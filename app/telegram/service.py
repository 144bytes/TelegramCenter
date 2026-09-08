"""Telegram layer — a single background asyncio loop hosting many persistent
Telethon sessions.

The auth internals (`_send_fresh_code_request`, per-key login lock, the
phone/QR/bot login coroutines) are ported verbatim from Telesender v1.5.2 —
they encode real, already-fixed bugs and must not be "cleaned up".
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events, types
from telethon.errors import AuthRestartError
from telethon.tl.functions.auth import SendCodeRequest

from .. import config
from ..logging import LOG


def _is_operator_key(key: str) -> bool:
    return key.startswith("op_")


def session_dir_for(key: str) -> Path:
    return (config.OPERATOR_SESSIONS_DIR if _is_operator_key(key)
            else config.CAMPAIGN_SESSIONS_DIR)

MOD = "telegram"



def media_info(message) -> dict | None:
    """Describe an attachment in the few words the chat needs to show.

    Nothing is downloaded: the UI shows a typed chip ("Photo", "Document:
    report.pdf"), so a message that carries only a picture stops rendering as
    an empty bubble. Order matters - a sticker is also a document, a voice note
    is also audio - so the most specific test comes first.
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

    Defined once and used by both reading history and sending, so a field
    added for one path cannot go missing on the other.
    """
    sender_id = getattr(message, "sender_id", None)
    mine = bool(message.out) if me_id is None else sender_id == me_id
    date = getattr(message, "date", None)
    return {
        "id": message.id,
        "date": date.isoformat() if hasattr(date, "isoformat") else str(date or ""),
        "out": bool(message.out),
        "sender": "operator" if mine else "client",
        # Telegram's own line breaks and spacing are part of what the person
        # wrote. Flattening them turned every multi-line message into one
        # run-on line, and nothing downstream could put them back.
        "text": message.raw_text or "",
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
        self.active_key: str | None = None
        self.ready = threading.Event()
        self._login_locks: dict[str, asyncio.Lock] = {}
        # One connect at a time per session. Telethon's connect() is not safe
        # to call twice at once on the same client.
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._login_ctx: tuple | None = None   # (api_id, api_hash, proxy) during a login
        # (owner, key) -> the NewMessage handler registered for it. One slot
        # per owner per key: re-attaching replaces that owner's handler and
        # never stacks. Owners are independent - the auto-responder detaching
        # its own listeners must not silence the operator chat, which is
        # exactly what a single shared slot used to do.
        self._incoming: dict[tuple[str, str], object] = {}
        self.default_api_id: int | None = None
        self.default_api_hash: str | None = None
        # profile_resolver(key) -> {"api_id", "api_hash", "proxy"} | None
        self.profile_resolver = profile_resolver

    # ── lifecycle ────────────────────────────────────────────────────────
    def configure_default_api(self, api_id, api_hash) -> None:
        try:
            self.default_api_id = int(api_id) if api_id not in (None, "") else None
        except (TypeError, ValueError):
            self.default_api_id = None
        self.default_api_hash = str(api_hash).strip() if api_hash not in (None, "") else None

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
            for client in list(self.clients.values()):
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
        if self.profile_resolver:
            try:
                r = self.profile_resolver(key)
            except Exception:  # noqa: BLE001
                r = None
            if r and r.get("api_id"):
                return int(r["api_id"]), r["api_hash"], r.get("proxy")
        if self._login_ctx:
            return self._login_ctx
        return self.default_api_id, self.default_api_hash, None

    def _connect_lock(self, key: str) -> asyncio.Lock:
        lock = self._connect_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._connect_locks[key] = lock
        return lock

    async def _client_for(self, key: str) -> TelegramClient:
        """The connected client for one session, created on first use.

        Serialised per key. Two requests arriving together for the same
        account - which is exactly what opening a chat does, asking for the
        dialog list and the live subscription at the same moment - both saw an
        unconnected client and both called connect() on it. One of them then
        waited forever.
        """
        api_id, api_hash, proxy = self._resolve_creds(key)
        if not (api_id and api_hash):
            raise RuntimeError("Telegram API ID / API Hash are not configured")
        async with self._connect_lock(key):
            client = self.clients.get(key)
            if client is None:
                # English is pinned rather than left to Telethon's default:
                # bots answer in the language the client reports, and the
                # antispam check reads those answers. One language to match,
                # deliberately.
                client = TelegramClient(str(self._session_path(key)),
                                        api_id, api_hash, proxy=proxy,
                                        lang_code="en", system_lang_code="en")
                self.clients[key] = client
            if not client.is_connected():
                await client.connect()
            return client

    def _login_lock(self, key: str) -> asyncio.Lock:
        lock = self._login_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._login_locks[key] = lock
        return lock

    async def _active_client(self) -> TelegramClient:
        if not self.active_key:
            raise RuntimeError("No account selected")
        client = await self._client_for(self.active_key)
        if not await client.is_user_authorized():
            raise RuntimeError("Selected account is not authorized")
        return client

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
        self._login_ctx = (api_id, api_hash, proxy) if api_id else self._login_ctx
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
                self.active_key = key
                return me
        finally:
            self._login_ctx = None

    async def login_qr(self, key, qr_provider, password_provider, should_cancel,
                       api_id=None, api_hash=None, proxy=None, force_relogin=False):
        self._login_ctx = (api_id, api_hash, proxy) if api_id else self._login_ctx
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
                self.active_key = key
                return me
        finally:
            self._login_ctx = None

    async def login_bot(self, key, bot_token, api_id=None, api_hash=None, proxy=None,
                        force_relogin=False):
        self._login_ctx = (api_id, api_hash, proxy) if api_id else self._login_ctx
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
                self.active_key = key
                return me
        finally:
            self._login_ctx = None

    # ── session management ───────────────────────────────────────────────
    async def set_active(self, key):
        client = await self._client_for(key)
        if not await client.is_user_authorized():
            raise RuntimeError("Selected session is not authorized")
        self.active_key = key
        return await client.get_me()

    async def get_me(self, key=None):
        try:
            client = await self._client_for(key) if key else await self._active_client()
            return await client.get_me()
        except Exception:  # noqa: BLE001
            return None

    async def remove_session(self, key):
        await self.detach_all_incoming(key)
        client = self.clients.pop(key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self.active_key == key:
            self.active_key = None

    async def rename_session(self, old_key, new_key):
        if old_key == new_key:
            return
        client = self.clients.pop(old_key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        src_dir, dst_dir = session_dir_for(old_key), session_dir_for(new_key)
        for p in src_dir.glob(old_key + ".session*"):
            suffix = p.name[len(old_key):]
            target = dst_dir / f"{new_key}{suffix}"
            try:
                if target.exists():
                    target.unlink()
                p.rename(target)
            except OSError as exc:
                LOG.warning(f"could not rename {p.name}: {exc}", module=MOD)
        if self.active_key == old_key:
            self.active_key = new_key
        await self._client_for(new_key)

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

    async def dialogs(self, limit: int, key=None):
        client = await self._client_for(key) if key else await self._active_client()
        out = []
        async for d in client.iter_dialogs(limit=None if limit == 0 else limit):
            out.append({
                "id": d.id, "name": self._entity_name(d.entity),
                "username": getattr(d.entity, "username", None), "entity": d.entity,
            })
        return out

    async def messages(self, entity, limit: int, key=None, offset_id: int = 0):
        """One page of history, oldest first.

        `offset_id` pages backwards: pass the id of the oldest message already
        on screen and this returns the ones before it, which is what loading
        older history on scroll-up needs.
        """
        client = await self._client_for(key) if key else await self._active_client()
        me = await client.get_me()
        out = []
        async for m in client.iter_messages(entity, limit=limit,
                                            offset_id=int(offset_id or 0)):
            out.append(describe_message(m, me.id))
        out.reverse()
        return out

    # ── writes ───────────────────────────────────────────────────────────
    async def send_message(self, key, entity, text: str, file: str | None = None,
                           reply_to: int | None = None):
        """Send, and describe what was sent.

        Returning the message itself is what lets the chat append one bubble
        instead of reloading the whole history after every send.

        `reply_to` is applied here and nowhere else, so a reply means the same
        thing whatever is being sent - text today, a picture or a voice note
        tomorrow - without each kind having to remember to pass it on.
        """
        client = await self._client_for(key)
        if not await client.is_user_authorized():
            raise RuntimeError("account not authorized")
        reply = int(reply_to) if reply_to else None
        if file:
            sent = await client.send_file(entity, file, caption=text or None,
                                          reply_to=reply)
        else:
            sent = await client.send_message(entity, text, reply_to=reply)
        return describe_message(sent)

    async def ask_bot(self, key: str, bot: str, text: str = "/start",
                      timeout: float = 25, ack_buttons: tuple = ()) -> str:
        """Send one command to a bot and return the text it answers with.

        Used for the antispam check. `conversation` waits for the bot's reply
        rather than polling history, so it cannot pick up an older message by
        mistake.

        `ack_buttons` lists button captions that simply close the dialog. Only
        an exact match is pressed - the bot also offers buttons that open an
        appeal, and those must never be clicked on the user's behalf.
        """
        client = await self._client_for(key)
        if not await client.is_user_authorized():
            raise RuntimeError("account not authorized")

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

        Left unpressed, the bot keeps waiting for a button and answers the next
        /start with "Please use buttons to communicate with me" instead of the
        status. The keyboard may belong to an earlier message than the one we
        just read, so recent history is searched too.

        Best effort: the answer is already in hand, so failing here must not
        fail the check.
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

    async def resolve_entity(self, ref: str, key=None):
        client = await self._client_for(key) if key else await self._active_client()
        s = ref.strip()
        if s.lstrip("-").isdigit():
            return await client.get_entity(int(s))
        return await client.get_entity(s)

    async def check_target(self, ref: str, key=None) -> dict:
        try:
            entity = await self.resolve_entity(ref, key=key)
            return {
                "ok": True,
                "id": getattr(entity, "id", None),
                "title": self._entity_name(entity),
                "username": getattr(entity, "username", None),
                "is_channel": isinstance(entity, types.Channel),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

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

    async def ensure_connected(self, key: str) -> bool:
        try:
            client = await self._client_for(key)
            return await client.is_user_authorized()
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"connect {key} failed: {exc}", module=MOD)
            return False

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

        Idempotent per (owner, key): re-attaching replaces that owner's own
        handler. Without that, every reconnect/refresh would stack another
        handler and one incoming message would trigger N auto-replies. Other
        owners' handlers are left strictly alone.
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
                    "peer_id": event.chat_id,
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
