"""Test fixtures.

Every test runs against a temporary TC_APP_DIR. Nothing here may ever touch
the real %APPDATA%\\TelegramCenter — that folder holds the user's live
sessions and settings.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    """Point every path constant at a throwaway directory.

    Derived from config rather than listed here, and applied to every test
    whether it asks for it or not. The listed version missed MEDIA_DIR, so
    the picture tests wrote their stubs into the media folder of the user's
    real %APPDATA% and left them there.
    """
    monkeypatch.setenv("TC_APP_DIR", str(tmp_path))
    from app import config

    real = config.APP_DIR
    for name in dir(config):
        if name == "APP_DIR" or name.startswith("_"):
            continue
        value = getattr(config, name)
        if not isinstance(value, Path):
            continue
        try:
            inside = value.relative_to(real)
        except ValueError:
            continue                 # WEB_DIR: not the user's data
        monkeypatch.setattr(config, name, tmp_path / inside)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)

    for folder in (config.DATA_DIR, config.CAMPAIGN_SESSIONS_DIR,
                   config.OPERATOR_SESSIONS_DIR, config.MEDIA_DIR,
                   config.LOGS_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    return tmp_path


# Guards that are right in production and in the way here.
#
# The start delay and the spread between connects are seconds to minutes by
# design, so a suite that ran with the shipped values would spend its time
# asleep. The young-account warning is the same kind of nuisance: every
# account a test creates was created just now. All of them are switched off
# here and turned back on, explicitly, by the tests that are about them.
# «Один раз» with a date already past: Start runs it at once, one pass.
ONE_PASS = {"mode": "ONCE", "at": "2020-01-01T00:00:00"}

NO_PACING = {
    "campaign.start_delay_sec": 0,
    "campaign.error_stop_percent": 0,
    "accounts.young_days": 0,
    "accounts.connect_spread_sec": 0,
}


@pytest.fixture(autouse=True)
def instant_presence(monkeypatch):
    """No real waiting around a send while the suite runs.

    Coming online, typing for as long as the text would take and lingering
    before going quiet are seconds each, by design - that is the entire point
    of them. Left alone, a suite that sends a few hundred messages would
    spend half an hour asleep. The tests that are about presence put the
    durations back themselves.
    """
    from app.services import presence
    monkeypatch.setattr(presence, "LINGER_SEC", (0.0, 0.0))
    monkeypatch.setattr(presence, "TYPING_MIN_SEC", 0.0)
    monkeypatch.setattr(presence, "TYPING_MAX_SEC", 0.0)


@pytest.fixture
def storage(app_dir):  # noqa: ARG001 - fixture ordering only
    from app.storage import Storage
    store = Storage()
    store.settings.update(dict(NO_PACING))
    return store


class FakeTelegram:
    """Stand-in for TelegramService with no network and no event loop thread.

    Coroutines are awaited by the caller, so tests stay deterministic.
    """

    def __init__(self):
        self.sent: list[tuple[str, object, str]] = []
        self.files: list[tuple[str, object, str, str]] = []
        # what each send was told to answer, in order
        self.replied: list[int | None] = []
        self.fail_on: set[object] = set()
        self.attached: dict[tuple[str, str], object] = {}
        self.profile_resolver = None
        self.removed: list[str] = []
        self.dropped: list[str] = []
        self.logged_out: list[str] = []
        # key -> first messages that came while nothing was listening
        self.waiting: dict[str, list[dict]] = {}
        self.renamed: list[tuple[str, str]] = []
        self.invalidated: list[str] = []
        self.authorized = True
        # sending to a channel: ref -> (discussion chat, anchor message id).
        # The default keeps the chat addressed the same way the channel is, so
        # a test that cares about which target was written to does not have to
        # care about the discussion chat as well.
        self.discussion: dict = {}
        self.no_comments: set = set()
        # ref -> "CHANNEL" | "GROUP" | "USER". Default is CHANNEL, which is
        # what the suite was written against; a test about supergroups says so.
        self.kinds: dict = {}
        # refs Telegram refuses to resolve, for the fallback path
        self.unresolvable: set = set()
        # invite hash -> the chat behind it, when the account is already in
        self.invites: dict = {}
        # refs the account is already a member of. Telegram reports this on
        # the chat object as `left`, so nothing needs to be remembered.
        self.members: set = set()
        self.joined: list[tuple[str, object]] = []
        self.left: list[tuple[str, object]] = []
        # what the account signalled around each send, in order
        self.presence: list[tuple] = []
        # «печатает…» signals, one per request: (key, entity, action)
        self.typing: list[tuple] = []
        # what the indicator, a reaction or a send raises instead of working
        self.typing_error: Exception | None = None
        self.react_error: Exception | None = None
        self.send_errors: dict = {}
        # what each channel forbids this account: ref -> row, as
        # own_restrictions returns it; a channel not listed forbids nothing
        self.restrictions: dict = {}
        self.restriction_asks: list[tuple] = []
        # reactions a chat offers, and the ones actually put
        self.reactions: list[str] = ["👍", "❤", "🔥"]
        self.reacted: list[tuple] = []
        self.join_error: Exception | None = None
        # antispam check
        self.asked: list[tuple[str, str, str]] = []
        self.acked: list[tuple] = []
        self.bot_reply = ("Good news, no limits are currently applied to your "
                          "account. You're free as a bird!")
        self.bot_fails: set[str] = set()
        self.bot_replies: list[str] = []
        # login behaviour: set login_error to make Telegram reject the attempt
        self.login_error: str | None = None
        self.login_creds: tuple | None = None
        # what the login was actually told to connect through
        self.login_proxy: tuple | None = None
        self.login_id = 999

    # lifecycle -----------------------------------------------------------
    def start(self):
        return None

    def submit(self, coro):
        """Run the coroutine to completion right away."""
        try:
            return _CompletedFuture(asyncio.get_event_loop().run_until_complete(coro))
        except RuntimeError:
            return _CompletedFuture(asyncio.run(coro))

    def run(self, coro, timeout=60):  # noqa: ARG002
        return asyncio.run(coro)

    def shutdown(self):
        return None

    # login ---------------------------------------------------------------
    async def _login(self, api_id, api_hash):
        if self.login_error:
            raise RuntimeError(self.login_error)
        # what the credential plumbing actually handed to Telethon
        self.login_creds = (api_id, api_hash)
        return _Me(self.login_id)

    async def login_session(self, key, phone, code_provider, password_provider,
                            api_id=None, api_hash=None, proxy=None,
                            force_relogin=False):
        self.login_proxy = proxy
        return await self._login(api_id, api_hash)

    async def login_qr(self, key, qr_provider, password_provider, should_cancel,
                       api_id=None, api_hash=None, proxy=None,
                       force_relogin=False):
        self.login_proxy = proxy
        return await self._login(api_id, api_hash)

    async def login_bot(self, key, bot_token, api_id=None, api_hash=None,
                        proxy=None, force_relogin=False):
        self.login_proxy = proxy
        return await self._login(api_id, api_hash)

    # surface used by the services ---------------------------------------
    async def health_check(self, key):
        if not self.authorized:
            return {"ok": False, "detail": "not authorized", "me": None, "checks": {}}
        return {"ok": True, "detail": "", "checks": {},
                "me": _Me(int(key.split("_")[-1]) if key.split("_")[-1].isdigit() else 1)}

    # presence: the small signals a real client sends ---------------------
    async def set_online(self, key, online=True):
        self.presence.append((key, "online" if online else "offline"))

    async def mark_read(self, key, entity):
        self.presence.append((key, "read", entity))

    async def set_typing(self, key, entity, action="typing"):
        self.presence.append((key, "typing", entity))
        if self.typing_error is not None:
            raise self.typing_error
        self.typing.append((key, entity, action))

    async def peek_history(self, key, entity, limit=15):  # noqa: ARG002
        self.presence.append((key, "peek", entity))
        return 4242

    async def reactions_available(self, key, entity):  # noqa: ARG002
        return list(self.reactions)

    async def react(self, key, entity, message_id, emoji):
        if self.react_error is not None:
            raise self.react_error
        self.reacted.append((key, entity, message_id, emoji))
        self.presence.append((key, "react", entity))

    async def send_message(self, key, entity, text, file=None, reply_to=None):
        if entity in self.fail_on:
            raise RuntimeError("ChatWriteForbidden")
        if entity in self.send_errors:
            raise self.send_errors[entity]
        self.sent.append((key, entity, text))
        self.replied.append(reply_to)
        self.presence.append((key, "send", entity))
        if file is not None:
            self.files.append((key, entity, str(file), text))
        # the real service describes what it sent, so the fake must too
        return {"id": 1000 + len(self.sent), "out": True, "sender": "operator",
                "text": text or "", "date": "2026-01-01T00:00:00",
                "media": {"kind": "document", "name": "f", "size": 1}
                if file is not None else None,
                "service": False, "reply_to": reply_to}

    # Keyed by (owner, key) exactly like the real service: a fake that kept a
    # single slot per key would hide the very bug the owners were added for.
    async def attach_incoming(self, key, on_message, owner="autoreply"):
        self.attached[(owner, key)] = on_message
        return True

    async def detach_incoming(self, key, owner="autoreply"):
        self.attached.pop((owner, key), None)

    async def detach_all_incoming(self, key):
        for owner, attached in [k for k in self.attached if k[1] == key]:
            self.attached.pop((owner, attached), None)

    def attached_keys(self, owner="autoreply"):
        return {k for o, k in self.attached if o == owner}

    async def remove_session(self, key):
        self.removed.append(key)
        await self.detach_all_incoming(key)

    async def drop_session(self, key):
        """Stop using the session and delete its files, as the real one does."""
        from app.telegram.service import session_dir_for
        await self.remove_session(key)
        removed = 0
        for path in session_dir_for(key).glob(f"{key}.session*"):
            path.unlink()
            removed += 1
        self.dropped.append(key)
        return removed

    async def first_contacts(self, key, since):  # noqa: ARG002
        """Chats opened while nobody listened: set `waiting` to a list of
        payloads per key."""
        return list(self.waiting.get(key, []))

    async def log_out(self, key):
        self.logged_out.append(key)
        await self.drop_session(key)

    async def rename_session(self, old, new):
        self.renamed.append((old, new))

    async def dialogs(self, limit, key=None):  # noqa: ARG002
        return [{"id": 5, "name": "Client", "username": "client", "entity": 5}]

    async def messages(self, entity, limit, key=None, offset_id=0):  # noqa: ARG002
        if offset_id:            # one page back, then nothing older
            return [] if offset_id <= 1 else [
                {"id": 1, "date": "2025-12-31T00:00:00", "out": False,
                 "sender": "client", "text": "older", "media": None,
                 "service": False}]
        return [{"id": 2, "date": "2026-01-01T00:00:00", "out": False,
                 "sender": "client", "text": "hi", "media": None,
                 "service": False}]

    async def ask_bot(self, key, bot, text="/start", timeout=25,  # noqa: ARG002
                      ack_buttons=()):
        if key in self.bot_fails:
            raise RuntimeError("USER_BLOCKED")
        self.asked.append((key, bot, text))
        self.acked.append(tuple(ack_buttons))
        if self.bot_replies:          # a scripted sequence, one per call
            return self.bot_replies.pop(0)
        return self.bot_reply

    async def check_target(self, ref, key=None):  # noqa: ARG002
        return {"ok": True, "id": 42, "title": "Channel", "username": str(ref),
                "kind": self.kinds.get(ref, "CHANNEL")}

    async def peer_kind(self, key, ref):  # noqa: ARG002
        """The entity and what kind of peer it is.

        The entity compares equal to the reference itself, so a test that
        checks what was written to keeps reading the name it used - but it
        also carries `left`, the way a real Chat or Channel does, so the
        membership check has something to read. `kinds` is what makes a
        target behave as a group rather than a channel; `members` is what
        makes the account already be in it.
        """
        if ref in self.unresolvable:
            raise ValueError(f'No user has "{ref}" as username')
        return _peer(ref, left=ref not in self.members),             self.kinds.get(ref, "CHANNEL")

    async def invite_chat(self, key, invite, join=False):  # noqa: ARG002
        if invite in self.invites:
            return self.invites[invite], False
        return (f"chat:{invite}", True) if join else (None, False)

    async def own_restrictions(self, key, chats):
        self.restriction_asks.append((key, tuple(chats)))
        return [self.restrictions.get(chat, {"kicked": False, "send": False,
                                             "until": None})
                for chat in chats]

    # commenting under a channel post ------------------------------------
    async def discussion_anchor(self, key, channel):  # noqa: ARG002
        """The discussion chat carries `left` too, the way a real one does."""
        if channel in self.no_comments:
            return None
        chat, anchor = self.discussion.get(channel, (channel, 500))
        return _peer(chat, left=chat not in self.members), anchor

    async def leave(self, key, entity):
        self.left.append((key, entity))

    async def join(self, key, entity):
        if self.join_error is not None:
            raise self.join_error
        self.joined.append((key, entity))

    def invalidate(self, key):
        self.invalidated.append(key)


def _peer(ref, *, left: bool):
    """A stand-in chat that compares equal to its reference.

    The real thing is a Chat or a Channel carrying a `left` flag. Tests want
    to assert on the plain name they passed in, so the flag is bolted onto a
    str or int subclass rather than onto an object of its own.
    """
    base = _StrPeer if isinstance(ref, str) else _IntPeer
    peer = base(ref)
    peer.left = left
    return peer


class _StrPeer(str):
    left = True


class _IntPeer(int):
    left = True


class _Me:
    def __init__(self, tid):
        self.id = tid
        self.username = "tester"
        self.first_name = "Test"
        self.last_name = ""
        self.phone = "+100"
        self.restricted = False


class _CompletedFuture:
    """The fake runs coroutines straight away, so the future is already done."""

    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):  # noqa: ARG002
        return self._value

    def done(self) -> bool:
        return True

    def cancel(self) -> bool:
        return False


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture
def services(storage, telegram):
    from app.core.events import EventBus
    from app.services import build_services
    return build_services(storage, telegram, EventBus())


@pytest.fixture
def seeded(storage):
    """A working API profile, one ready account, one channel."""
    from app import config
    from app.models import Account, ApiProfile, Target
    from app.models.enums import AccountState

    profile = ApiProfile(name="Main", api_id=123, api_hash="hash")
    storage.api_profiles.add(profile)

    account = Account(key="session_777", session_file="session_777.session",
                      telegram_id=777, username="sender",
                      api_profile_id=profile.id, raw_state=AccountState.READY)
    storage.accounts.add(account)

    target = Target(title="Channel A", username="channel_a", telegram_id=-100)
    storage.targets.add(target)
    (config.CAMPAIGN_SESSIONS_DIR / "session_777.session").write_text("s")
    return {"profile": profile, "account": account, "target": target}
