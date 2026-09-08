"""Telegram layer: one background asyncio loop hosting many persistent

Telethon sessions.
The auth internals (`_send_fresh_code_request`, the per-key login lock,
the phone/QR/bot logins) are ported verbatim from Telesender v1.5.2 -
they encode already-fixed bugs and must not be cleaned up.
"""
from __future__ import annotations

import asyncio
import random
import threading
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events, types
from telethon.errors import AuthRestartError
from telethon.tl.functions.auth import SendCodeRequest

from .. import config
from ..logging import LOG
from .errors import (NoCredentialsError, NotAuthorizedError,
                     SessionReplaceError, classify, tg_error)
from ..models.enums import TargetType


def entity_kind(entity) -> str:
    """Channel, group or person - the distinction everything downstream needs.

    `isinstance(entity, types.Channel)` is not a test for "channel": that
    class covers supergroups too, and only `broadcast` / `megagroup` tell
    them apart. Every supergroup was filed as a channel and asked for a
    discussion chat it does not have - two thirds of the delivery failures.
    """
    if isinstance(entity, types.Channel):
        return (TargetType.CHANNEL if getattr(entity, "broadcast", False)
                else TargetType.GROUP)
    if isinstance(entity, (types.Chat, types.ChatForbidden,
                           types.ChannelForbidden)):
        # a small group, or one we have been thrown out of
        return TargetType.GROUP
    return TargetType.USER


def is_member(entity) -> bool | None:
    """Is this account in that chat? None when the question does not apply.

    Telegram reports it on the chat object we already hold (`left`), so the
    answer is free. Asked every time rather than remembered: a remembered
    "joined" goes stale the moment the user leaves by hand. A person has no
    `left` field - that is the None case.
    """
    left = getattr(entity, "left", None)
    return None if left is None else not left


# What a chat allows when Telegram says "all standard reactions"
# instead of listing them. The first two are the ones used.
COMMON_REACTIONS = ("👍", "❤", "🔥", "👏")


def _full_request(entity):
    """The right "tell me everything about this peer" request for its kind."""
    from telethon import types as tl
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetFullChatRequest
    if isinstance(entity, tl.Channel):
        return GetFullChannelRequest(channel=entity)
    return GetFullChatRequest(chat_id=entity.id)


def _is_operator_key(key: str) -> bool:
    return key.startswith("op_")


def session_dir_for(key: str) -> Path:
    return (config.OPERATOR_SESSIONS_DIR if _is_operator_key(key)
            else config.CAMPAIGN_SESSIONS_DIR)

MOD = "telegram"

# How long the whole fleet takes to disconnect when the app closes.
# Small on purpose: closing happens because the user asked for it.
CLOSE_SPREAD_SEC = 3.0



def media_info(message) -> dict | None:
    """Describe an attachment in the few words the chat needs to show.

    Nothing is downloaded. Order matters: a sticker is also a document, a
    voice note is also audio, so the most specific test comes first.
    """
    if message is None:
        return None
    kinds = (
        ("sticker", getattr(message, "sticker", None)),
        ("voice", getattr(message, "voice", None)),
        ("video_note", getattr(message, "video_note", None)),
        ("gif", getattr(message, "gif", None)),
        ("video", getattr(message, "video", None)),
        ("audio", getattr(message, "audio", None)),
        ("photo", getattr(message, "photo", None)),
        ("document", getattr(message, "document", None)),
    )
    kind = next((name for name, value in kinds if value), None)
    if kind is None:
        return None
    file = getattr(message, "file", None)
    return {
        "kind": kind,
        "name": (getattr(file, "name", None) or "") if file else "",
        "size": int(getattr(file, "size", 0) or 0) if file else 0,
    }



def describe_message(message, me_id=None) -> dict:
    """One message in the shape the chat renders.

    Defined once for reading history and for sending. `message` may be None:
    Telethon sometimes hands back nothing even though the send went through,
    and raising there made the campaign record a failure and send again.
    """
    if message is None:
        return {"id": 0, "date": "", "out": True, "sender": "operator",
                "text": "", "media": None, "service": False, "reply_to": None}
    out = bool(getattr(message, "out", False))
    sender_id = getattr(message, "sender_id", None)
    mine = out if me_id is None else sender_id == me_id
    date = getattr(message, "date", None)
    return {
        "id": getattr(message, "id", 0),
        "date": date.isoformat() if hasattr(date, "isoformat") else str(date or ""),
        "out": out,
        "sender": "operator" if mine else "client",
        # Telegram's line breaks are part of what the person wrote:
        # flattening them turned every multi-line message into one line.
        "text": getattr(message, "raw_text", "") or "",
        "media": media_info(message),
        "service": bool(getattr(message, "action", None)),
        # Which message this one answers, if any. The chat quotes it above the
        # bubble the way Telegram does.
        "reply_to": getattr(message, "reply_to_msg_id", None),
    }


class TelegramService:
    def __init__(self, profile_resolver=None):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.clients: dict[str, TelegramClient] = {}
        self.ready = threading.Event()
        self._login_locks: dict[str, asyncio.Lock] = {}
        # One connect at a time per session. Telethon's connect() is not safe
        # to call twice at once on the same client.
        self._connect_locks: dict[str, asyncio.Lock] = {}
        # session key -> (api_id, api_hash, proxy) while that key is signing
        # in. Keyed, not global: a login must not decide how another account
        # connects, and the account may have no record yet.
        self._login_ctx: dict[str, tuple] = {}
        # (owner, key) -> the handler registered for it. One slot per owner
        # per key, so the auto-responder detaching cannot silence the chat.
        self._incoming: dict[tuple[str, str], object] = {}
        # profile_resolver(key) -> {"api_id", "api_hash", "proxy"} | None
        self.profile_resolver = profile_resolver

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._thread_main, daemon=True, name="tg-loop")
        self.thread.start()
        self.ready.wait(timeout=10)

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        LOG.info("event loop started", module=MOD)
        self.loop.run_forever()

    def submit(self, coro):
        self.start()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro, timeout: float = 60):
        """Blocking helper for scripts/tests — never call from the Tk thread."""
        return self.submit(coro).result(timeout=timeout)

    def shutdown(self) -> None:
        if self.loop is None:
            return
        async def _close():
            self._incoming.clear()
            # In a random order with a small gap, for the same reason they are
            # spread out on the way in - but seconds, not minutes: the app has to
            # close when the user closes it.
            clients = list(self.clients.values())
            random.shuffle(clients)
            gap = CLOSE_SPREAD_SEC / len(clients) if clients else 0
            for n, client in enumerate(clients):
                if n and gap:
                    await asyncio.sleep(gap)
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            for task in asyncio.all_tasks():
                if task is not asyncio.current_task():
                    task.cancel()
        try:
            self.submit(_close()).result(timeout=8)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        LOG.info("event loop stopped", module=MOD)

    # ── client pool ──────────────────────────────────────────────────────
    def _session_path(self, key: str) -> Path:
        return session_dir_for(key) / key

    def _resolve_creds(self, key: str):
        # A login in progress wins: the user picked these credentials a moment
        # ago, while the stored record is what the account used before.
        # Preferring the record sent a re-login out over the old address, and
        # Telegram's "new login from …" notice named the wrong city.
        ctx = self._login_ctx.get(key)
        if ctx:
            return ctx
        if self.profile_resolver:
            try:
                r = self.profile_resolver(key)
            except Exception:  # noqa: BLE001
                r = None
            if r and r.get("api_id"):
                return int(r["api_id"]), r["api_hash"], r.get("proxy")
        return None, None, None

    def _connect_lock(self, key: str) -> asyncio.Lock:
        lock = self._connect_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._connect_locks[key] = lock
        return lock

    async def _client_for(self, key: str) -> TelegramClient:
        """The connected client for one session, created on first use.

        Serialised per key: two requests arriving together - the dialog list and
        the live subscription - both saw an unconnected client, both called
        connect(), and one waited forever.
        """
        api_id, api_hash, proxy = self._resolve_creds(key)
        if not (api_id and api_hash):
            raise NoCredentialsError(key)
        async with self._connect_lock(key):
            client = self.clients.get(key)
            if client is None:
                # Russian, pinned rather than Telethon's default English: the accounts
                # write in Russian. The antispam check reads either language.
                client = TelegramClient(str(self._session_path(key)),
                                        api_id, api_hash, proxy=proxy,
                                        lang_code="ru", system_lang_code="ru")
                self.clients[key] = client
            if not client.is_connected():
                await client.connect()
            return client

    def invalidate(self, key: str) -> None:
        """Throw this session's client away so the next use builds a fresh one.

        A pooled client keeps the api_id, api_hash and proxy it was built with,
        so changing a profile left one auth key live on two addresses - what
        Telegram kills a session for. Safe from any thread.
        """
        if not key:
            return
        client = self.clients.pop(key, None)
        for owner, attached in [k for k in self._incoming if k[1] == key]:
            self._incoming.pop((owner, attached), None)
        if client is None:
            return

        async def _close():
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                LOG.debug(f"invalidate {key}: {exc}", module=MOD)

        loop = self.loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(_close(), loop)
        LOG.debug(f"client for {key} dropped from the pool", module=MOD)

    def _login_lock(self, key: str) -> asyncio.Lock:
        lock = self._login_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._login_locks[key] = lock
        return lock

    # ── auth (ported verbatim from Telesender) ───────────────────────────
    async def _send_fresh_code_request(self, client, phone):
        for attempt in range(3):
            try:
                return await client(SendCodeRequest(
                    phone, client.api_id, client.api_hash, types.CodeSettings()))
            except AuthRestartError:
                if attempt == 2:
                    raise

    async def login_session(self, key, phone, code_provider, password_provider,
                            api_id=None, api_hash=None, proxy=None, force_relogin=False):
        if api_id:
            self._login_ctx[key] = (api_id, api_hash, proxy)
        try:
            async with self._login_lock(key):
                client = await self._client_for(key)
                if force_relogin and await client.is_user_authorized():
                    await client.log_out()
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    self.clients.pop(key, None)
                    client = await self._client_for(key)

                if not await client.is_user_authorized():
                    sent = await self._send_fresh_code_request(client, phone)
                    phone_code_hash = sent.phone_code_hash
                    needs_password = False
                    code_error = None
                    while True:
                        code = await asyncio.to_thread(code_provider, code_error)
                        try:
                            await client.sign_in(phone=phone, code=code,
                                                 phone_code_hash=phone_code_hash)
                            break
                        except Exception as exc:  # noqa: BLE001
                            if exc.__class__.__name__ == "SessionPasswordNeededError":
                                needs_password = True
                                break
                            if exc.__class__.__name__ != "PhoneCodeInvalidError":
                                raise
                            code_error = exc

                    if needs_password:
                        password_error = None
                        while True:
                            password = await asyncio.to_thread(password_provider, password_error)
                            try:
                                await client.sign_in(password=password)
                                break
                            except Exception as exc:  # noqa: BLE001
                                if exc.__class__.__name__ != "PasswordHashInvalidError":
                                    raise
                                password_error = exc

                me = await client.get_me()
                return me
        finally:
            self._login_ctx.pop(key, None)

    async def login_qr(self, key, qr_provider, password_provider, should_cancel,
                       api_id=None, api_hash=None, proxy=None, force_relogin=False):
        if api_id:
            self._login_ctx[key] = (api_id, api_hash, proxy)
        try:
            async with self._login_lock(key):
                client = await self._client_for(key)
                if force_relogin and await client.is_user_authorized():
                    await client.log_out()
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    self.clients.pop(key, None)
                    client = await self._client_for(key)

                if not await client.is_user_authorized():
                    qr = await client.qr_login()
                    qr_provider(qr.url)
                    needs_password = False
                    while True:
                        if should_cancel():
                            raise RuntimeError("Login cancelled")
                        remaining = (qr.expires - datetime.now(timezone.utc)).total_seconds()
                        if remaining <= 0:
                            await qr.recreate()
                            qr_provider(qr.url)
                            continue
                        try:
                            await qr.wait(timeout=min(1.5, remaining))
                            break
                        except asyncio.TimeoutError:
                            continue
                        except Exception as exc:  # noqa: BLE001
                            if exc.__class__.__name__ == "SessionPasswordNeededError":
                                needs_password = True
                                break
                            raise

                    if needs_password:
                        password_error = None
                        while True:
                            if should_cancel():
                                raise RuntimeError("Login cancelled")
                            password = await asyncio.to_thread(password_provider, password_error)
                            try:
                                await client.sign_in(password=password)
                                break
                            except Exception as exc:  # noqa: BLE001
                                if exc.__class__.__name__ != "PasswordHashInvalidError":
                                    raise
                                password_error = exc

                me = await client.get_me()
                return me
        finally:
            self._login_ctx.pop(key, None)

    async def login_bot(self, key, bot_token, api_id=None, api_hash=None, proxy=None,
                        force_relogin=False):
        if api_id:
            self._login_ctx[key] = (api_id, api_hash, proxy)
        try:
            async with self._login_lock(key):
                client = await self._client_for(key)
                if force_relogin and await client.is_user_authorized():
                    await client.log_out()
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    self.clients.pop(key, None)
                    client = await self._client_for(key)
                if not await client.is_user_authorized():
                    await client.sign_in(bot_token=bot_token)
                me = await client.get_me()
                return me
        finally:
            self._login_ctx.pop(key, None)

    # ── session management ───────────────────────────────────────────────
    async def remove_session(self, key):
        """Stop using this session. The file on disk is left alone."""
        await self.detach_all_incoming(key)
        client = self.clients.pop(key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def drop_session(self, key: str) -> int:
        """Stop using this session and delete its files. Returns how many.

        Both halves, in this order: on Windows a file open by a connected client
        does not delete. Three callers need it - deleting an account, deleting
        an operator, abandoning an unfinished login.
        """
        if not key:
            return 0
        await self.remove_session(key)
        removed = 0
        for path in session_dir_for(key).glob(f"{key}.session*"):
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                LOG.warning(f"could not delete {path.name}: {exc}", module=MOD)
        return removed

    async def log_out(self, key: str) -> None:
        """End this session's authorisation in Telegram, then delete its files.

        Deleting the file alone leaves the login listed under Devices.
        """
        if not key:
            return
        try:
            client = self.clients.get(key) or await self._client_for(key)
            await client.log_out()
        except Exception as exc:  # noqa: BLE001 - the files go either way
            LOG.warning(f"could not log {key} out: {type(exc).__name__}: {exc}",
                        module=MOD)
        await self.drop_session(key)

    async def rename_session(self, old_key, new_key):
        """Give a finished login its permanent name.

        Both clients let go of their files first. Signing in again writes to a
        name already in use, the rename failed with WinError 32, and the login
        reported success while keeping the old session.
        """
        if old_key == new_key:
            return
        client = self.clients.pop(old_key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        # The account being re-authorised is very likely connected right now.
        await self.remove_session(new_key)
        src_dir, dst_dir = session_dir_for(old_key), session_dir_for(new_key)
        # A journal left over from the old session describes a database that
        # is about to be replaced, so it goes before anything is moved in.
        for stale in dst_dir.glob(f"{new_key}.session-*"):
            try:
                stale.unlink()
            except OSError:
                pass
        for p in sorted(src_dir.glob(old_key + ".session*")):
            suffix = p.name[len(old_key):]
            target = dst_dir / f"{new_key}{suffix}"
            try:
                if target.exists():
                    target.unlink()
                p.rename(target)
            except OSError as exc:
                if suffix == ".session":
                    # Carrying on would report a successful login while the account kept
                    # the old authorisation - the one being replaced.
                    raise SessionReplaceError(f"{target.name}: {exc}") from exc
                LOG.warning(f"could not rename {p.name}: {exc}", module=MOD)

    # ── reads ────────────────────────────────────────────────────────────
    @staticmethod
    def _entity_name(entity):
        if entity is None:
            return "Unknown"
        title = getattr(entity, "title", None)
        if title:
            return title
        name = " ".join(x for x in (getattr(entity, "first_name", None),
                                    getattr(entity, "last_name", None)) if x).strip()
        if name:
            return name
        username = getattr(entity, "username", None)
        return f"@{username}" if username else "Unknown"

    async def dialogs(self, limit: int, key: str):
        client = await self._client_for(key)
        out = []
        async for d in client.iter_dialogs(limit=None if limit == 0 else limit):
            out.append({
                "id": d.id, "name": self._entity_name(d.entity),
                "username": getattr(d.entity, "username", None), "entity": d.entity,
            })
        return out

    async def messages(self, entity, limit: int, key: str, offset_id: int = 0):
        """One page of history, oldest first.

        `offset_id` pages backwards: pass the oldest message already on screen
        to get the ones before it.
        """
        client = await self._client_for(key)
        me = await client.get_me()
        out = []
        async for m in client.iter_messages(entity, limit=limit,
                                            offset_id=int(offset_id or 0)):
            out.append(describe_message(m, me.id))
        out.reverse()
        return out

    # ── writes ───────────────────────────────────────────────────────────
    async def join(self, key: str, entity):
        """Join a channel, supergroup or discussion chat. Transport only.

        Already being a member is not an error, so callers may join without
        paying for a membership lookup first.
        """
        from telethon.tl.functions.channels import JoinChannelRequest
        client = await self._client_for(key)
        return await client(JoinChannelRequest(entity))

    async def leave(self, key: str, entity):
        """Leave a channel, supergroup or chat. Transport only."""
        from telethon.tl.functions.channels import LeaveChannelRequest
        client = await self._client_for(key)
        return await client(LeaveChannelRequest(entity))

    async def discussion_anchor(self, key: str, channel):
        """Where a comment under the channel's latest post has to go.

        Returns `(linked_chat, anchor_message_id)`, or None when there is no
        post or no discussion group. Replying to the anchor is what makes a
        message a comment rather than a loose line in the chat.
        """
        from telethon.tl.functions.messages import GetDiscussionMessageRequest
        client = await self._client_for(key)
        entity = await client.get_entity(channel)
        posts = await client.get_messages(entity, limit=1)
        if not posts:
            return None
        result = await client(GetDiscussionMessageRequest(peer=entity,
                                                          msg_id=posts[0].id))
        messages = list(getattr(result, "messages", None) or [])
        if not messages:
            return None
        anchor = messages[0]
        peer = getattr(anchor, "peer_id", None)
        chat_id = getattr(peer, "channel_id", None)
        chat = next((c for c in getattr(result, "chats", None) or []
                     if getattr(c, "id", None) == chat_id), None)
        if chat is None:
            chat = await client.get_entity(peer)
        return chat, anchor.id

    # ── presence: the small signals a real client sends ──────────────────
    # Each does one thing and decides nothing. Who calls them in what
    # order is behaviour, and that lives in services/presence.py.
    async def set_online(self, key: str, online: bool = True) -> None:
        """Appear in Telegram as online, or stop appearing.

        Connected is not online: an official client sends this separately and
        Telethon never sends it, so an account held a connection for three days
        without ever being seen online.
        """
        from telethon.tl.functions.account import UpdateStatusRequest
        client = await self._client_for(key)
        await client(UpdateStatusRequest(offline=not online))

    async def mark_read(self, key: str, entity) -> None:
        """Mark what is in this chat as read. Best effort."""
        client = await self._client_for(key)
        await client.send_read_acknowledge(entity)

    async def set_typing(self, key: str, entity, action: str = "typing") -> None:
        """Show «печатает…» (or «отправляет фото…») once. Telegram keeps it
        on screen for a few seconds; whoever wants it longer sends it again.

        One request and nothing left running: Telethon's `client.action` runs
        it as a background task whose failure surfaces after the block, in
        place of whatever the block did.
        """
        from telethon import types as tl
        from telethon.tl.functions.messages import SetTypingRequest
        signal = (tl.SendMessageUploadPhotoAction(progress=0)
                  if action == "upload_photo" else tl.SendMessageTypingAction())
        client = await self._client_for(key)
        await client(SetTypingRequest(peer=entity, action=signal))

    async def peek_history(self, key: str, entity, limit: int = 15) -> int:
        """Read the last few messages, the way opening a chat does.

        Returns the id of the newest one, which is what a reaction needs.
        """
        client = await self._client_for(key)
        messages = await client.get_messages(entity, limit=limit)
        return getattr(messages[0], "id", 0) if messages else 0

    async def send_message(self, key, entity, text: str, file: str | None = None,
                           reply_to: int | None = None):
        """Send, and describe what was sent. The send alone: what raises here
        is the send's own failure.

        Returning the message lets the chat append one bubble instead of
        reloading history. `reply_to` is applied here and nowhere else.
        """
        client = await self._client_for(key)
        if not await client.is_user_authorized():
            raise NotAuthorizedError(key)
        reply = int(reply_to) if reply_to else None
        if file:
            sent = await client.send_file(entity, file, caption=text or None,
                                          reply_to=reply)
        else:
            sent = await client.send_message(entity, text, reply_to=reply)
        return describe_message(sent)

    async def own_restrictions(self, key: str, chats: list) -> list[dict]:
        """What each of these channels forbids this account, in one request.

        One row per channel or supergroup: `kicked` (thrown out, or may not
        even read), `send` (may not write) and `until` (when that ends; None
        or far away means never). Anything that is not a channel has nothing
        to report and is left out.
        """
        from telethon import types as tl
        from telethon.tl.functions.channels import GetChannelsRequest
        channels = [c for c in chats
                    if isinstance(c, (tl.Channel, tl.ChannelForbidden))]
        if not channels:
            return []
        client = await self._client_for(key)
        result = await client(GetChannelsRequest(id=channels))
        rows = []
        for chat in getattr(result, "chats", None) or []:
            if isinstance(chat, tl.ChannelForbidden):
                rows.append({"kicked": True, "send": True,
                             "until": getattr(chat, "until_date", None)})
                continue
            rights = getattr(chat, "banned_rights", None)
            rows.append({"kicked": bool(rights and rights.view_messages),
                         "send": bool(rights and rights.send_messages),
                         "until": rights.until_date if rights else None})
        return rows

    # ── reactions ────────────────────────────────────────────────────────
    async def reactions_available(self, key: str, entity) -> list[str]:
        """Which emoji this chat allows, in the order Telegram lists them.

        Paid reactions are left out everywhere: they cost real Stars and tend
        to be listed first, so "take the first one" would spend money.
        """
        from telethon import types as tl
        client = await self._client_for(key)
        full = await client(
            _full_request(await client.get_entity(entity)))
        allowed = getattr(getattr(full, "full_chat", None),
                          "available_reactions", None)
        if isinstance(allowed, tl.ChatReactionsAll):
            # every standard emoji is allowed; Telegram does not enumerate
            # them here, so fall back to the ones every chat has
            return list(COMMON_REACTIONS)
        emoticons = []
        for item in getattr(allowed, "reactions", None) or []:
            emoji = getattr(item, "emoticon", None)
            if emoji:                      # ReactionPaid carries no emoticon
                emoticons.append(emoji)
        return emoticons

    async def react(self, key: str, entity, message_id: int, emoji: str) -> None:
        from telethon import types as tl
        from telethon.tl.functions.messages import SendReactionRequest
        client = await self._client_for(key)
        await client(SendReactionRequest(
            peer=entity, msg_id=int(message_id),
            reaction=[tl.ReactionEmoji(emoticon=emoji)]))

    async def ask_bot(self, key: str, bot: str, text: str = "/start",
                      timeout: float = 25, ack_buttons: tuple = ()) -> str:
        """Send one command to a bot and return the text it answers with.

        `conversation` waits for the reply rather than polling history.
        `ack_buttons` are captions that merely close the dialog - only an exact
        match is pressed, never the button that opens an appeal.
        """
        client = await self._client_for(key)
        if not await client.is_user_authorized():
            raise NotAuthorizedError(key)

        entity = await client.get_entity(bot)
        # not exclusive: the account may have other handlers attached
        async with client.conversation(entity, timeout=timeout,
                                       exclusive=False) as conv:
            await conv.send_message(text)
            reply = await conv.get_response()
            answer = reply.raw_text or ""

            if ack_buttons:
                await self._press_ack(client, entity, reply, ack_buttons, key)
            return answer

    async def _press_ack(self, client, entity, reply, ack_buttons: tuple,
                         key: str) -> bool:
        """Close the bot's dialog by pressing its closing button.

        Left unpressed, the bot answers the next /start with "use the buttons"
        instead of the status. Best effort: the answer is already in hand.
        """
        wanted = {c.strip().lower() for c in ack_buttons}
        candidates = [reply]
        try:
            candidates += await client.get_messages(entity, limit=5)
        except Exception as exc:  # noqa: BLE001
            LOG.debug(f"{key}: could not read the bot's history: {exc}", module=MOD)

        for message in candidates:
            try:
                rows = getattr(message, "buttons", None) or []
            except Exception:  # noqa: BLE001 - Telethon builds these lazily
                continue
            for row in rows:
                for button in row:
                    caption = (getattr(button, "text", "") or "").strip().lower()
                    if caption in wanted:
                        try:
                            await button.click()
                            return True
                        except Exception as exc:  # noqa: BLE001
                            LOG.debug(f"{key}: could not press {caption!r}: {exc}",
                                      module=MOD)
                            return False
        return False

    async def resolve_entity(self, ref: str, key: str):
        client = await self._client_for(key)
        s = str(ref).strip()
        if s.lstrip("-").isdigit():
            return await client.get_entity(int(s))
        return await client.get_entity(s)

    async def peer_kind(self, key: str, ref) -> tuple:
        """Resolve a reference and say what kind of peer it turned out to be.

        Both together, because every caller needs both and asking twice means
        two lookups that can disagree. The kind comes from the entity Telegram
        just returned, never from what we stored.
        """
        entity = await self.resolve_entity(ref, key=key)
        return entity, entity_kind(entity)

    async def invite_chat(self, key: str, invite: str, join: bool = False):
        """The chat behind a t.me/+… link, joining it first if asked.

        Returns `(chat, joined)`: chat is None when the account is not in it
        and was not asked to join. A private chat reached by invitation has no
        username and no id, so `get_entity` can never find it.
        """
        from telethon.tl.functions.messages import (CheckChatInviteRequest,
                                                    ImportChatInviteRequest)
        client = await self._client_for(key)
        hash_ = invite.lstrip("+")
        result = await client(CheckChatInviteRequest(hash_))
        chat = getattr(result, "chat", None)
        if chat is not None:
            return chat, False               # already a member
        if not join:
            return None, False
        updates = await client(ImportChatInviteRequest(hash_))
        chats = list(getattr(updates, "chats", None) or [])
        return (chats[0], True) if chats else (None, False)

    async def check_target(self, ref: str, key: str) -> dict:
        try:
            entity = await self.resolve_entity(ref, key=key)
            return {
                "ok": True,
                "id": getattr(entity, "id", None),
                "title": self._entity_name(entity),
                "username": getattr(entity, "username", None),
                # CHANNEL / GROUP / USER by entity_kind. The old isinstance check said
                # True for supergroups, so their stored type was always wrong.
                "kind": entity_kind(entity),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": tg_error(exc), "fault": classify(exc)}

    # ── health ───────────────────────────────────────────────────────────
    async def health_check(self, key: str) -> dict:
        checks = {"session_valid": False, "authorized": False, "reachable": False,
                  "get_me": False}
        detail = ""
        try:
            client = await self._client_for(key)
            checks["session_valid"] = True
            checks["reachable"] = client.is_connected()
            authed = await client.is_user_authorized()
            checks["authorized"] = authed
            if authed:
                me = await client.get_me()
                checks["get_me"] = me is not None
                return {"checks": checks, "ok": True, "detail": "", "me": me}
            detail = "not authorized"
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
        return {"checks": checks, "ok": False, "detail": detail, "me": None}

    # ── incoming updates ─────────────────────────────────────────────────
    async def detach_incoming(self, key: str, owner: str = "autoreply") -> None:
        """Remove one owner's NewMessage handler for one account, if any."""
        handler = self._incoming.pop((owner, key), None)
        if handler is None:
            return
        client = self.clients.get(key)
        if client is None:
            return
        try:
            client.remove_event_handler(handler)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"detach_incoming {owner}/{key}: {exc}", module=MOD)

    async def detach_all_incoming(self, key: str) -> None:
        """Remove every owner's handler for one account - used when the
        session itself goes away."""
        for owner, attached in [k for k in self._incoming if k[1] == key]:
            await self.detach_incoming(attached, owner)

    async def attach_incoming(self, key: str, on_message,
                              owner: str = "autoreply") -> bool:
        """Register a NewMessage handler for one account, on behalf of `owner`.

        Idempotent per (owner, key): without that, every refresh stacked another
        handler and one message triggered N auto-replies. Other owners are left
        alone.
        """
        try:
            client = await self._client_for(key)
            if not await client.is_user_authorized():
                await self.detach_incoming(key, owner)
                return False

            await self.detach_incoming(key, owner)

            async def _handler(event):
                try:
                    sender = await event.get_sender()
                except Exception:  # noqa: BLE001
                    sender = None
                on_message({
                    "account_key": key,
                    # The chat, not the author: in a group these differ, and
                    # anything replying to `peer_id` writes into the group.
                    "peer_id": event.chat_id,
                    # Whether somebody is writing to the account itself. A group or a
                    # channel is not, and an auto-reply there is posted for everyone.
                    "private": bool(getattr(event, "is_private", False)),
                    "sender_id": getattr(event, "sender_id", None),
                    "sender_name": self._entity_name(sender),
                    "sender_username": getattr(sender, "username", None),
                    "sender_bot": bool(getattr(sender, "bot", False)),
                    "message_id": event.id,
                    "text": event.raw_text or "",
                    "media": media_info(getattr(event, "message", None)),
                    "reply_to": getattr(event, "reply_to_msg_id", None),
                    "date": event.date.isoformat() if event.date else "",
                    "out": bool(event.out),
                })

            client.add_event_handler(_handler, events.NewMessage(incoming=True))
            self._incoming[(owner, key)] = _handler
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"attach_incoming {owner}/{key} failed: {exc}", module=MOD)
            return False

    def attached_keys(self, owner: str = "autoreply") -> set[str]:
        """Which account keys this owner is currently listening on."""
        return {k for o, k in self._incoming if o == owner}

    async def first_contacts(self, key: str, since) -> list[dict]:
        """Private chats a person opened while nobody was listening.

        A chat counts when everything in it is their unread messages - they
        never wrote before and nobody answered - the newest no older than
        `since`, and they are a person, not a bot. One payload per chat,
        shaped like a live message, with all the unread text in it.
        """
        client = await self._client_for(key)
        out = []
        async for d in client.iter_dialogs():
            msg = d.message
            if msg is None or msg.date is None:
                continue
            if msg.date < since:
                if d.pinned:
                    continue
                break                   # the rest are older still
            entity = d.entity
            if (not d.is_user or not d.unread_count or msg.out
                    or getattr(entity, "bot", False)
                    or getattr(entity, "is_self", False)):
                continue
            history = await client.get_messages(entity, limit=d.unread_count + 1)
            if len(history) > d.unread_count or any(m.out for m in history):
                continue
            out.append({
                "account_key": key,
                "peer_id": d.id,
                "private": True,
                "sender_id": getattr(entity, "id", None),
                "sender_name": self._entity_name(entity),
                "sender_username": getattr(entity, "username", None),
                "sender_bot": False,
                "message_id": msg.id,
                "text": "\n".join(m.raw_text or "" for m in reversed(history)),
                "media": None,
                "reply_to": None,
                "date": msg.date.isoformat(),
                "out": False,
            })
        return out
