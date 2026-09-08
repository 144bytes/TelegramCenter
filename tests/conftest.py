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

DATA_FILES = (
    "SETTINGS_FILE", "ACCOUNTS_FILE", "OPERATORS_FILE", "TARGETS_FILE",
    "TEMPLATES_FILE", "CAMPAIGNS_FILE", "API_PROFILES_FILE",
    "NETWORK_PROFILES_FILE", "AUTO_REPLY_FILE", "CONVERSATIONS_FILE",
)

FILE_NAMES = {
    "SETTINGS_FILE": "settings.json",
    "ACCOUNTS_FILE": "accounts.json",
    "OPERATORS_FILE": "operators.json",
    "TARGETS_FILE": "targets.json",
    "TEMPLATES_FILE": "templates.json",
    "CAMPAIGNS_FILE": "campaigns.json",
    "API_PROFILES_FILE": "api_profiles.json",
    "NETWORK_PROFILES_FILE": "network_profiles.json",
    "AUTO_REPLY_FILE": "auto_reply.json",
    "CONVERSATIONS_FILE": "conversations.json",
}


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """Point every path constant at a throwaway directory."""
    monkeypatch.setenv("TC_APP_DIR", str(tmp_path))
    from app import config

    data = tmp_path / "data"
    campaign_sessions = tmp_path / "sessions" / "campaign"
    operator_sessions = tmp_path / "sessions" / "operators"
    for d in (data, campaign_sessions, operator_sessions):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "CAMPAIGN_SESSIONS_DIR", campaign_sessions)
    monkeypatch.setattr(config, "OPERATOR_SESSIONS_DIR", operator_sessions)
    monkeypatch.setattr(config, "LOCK_FILE", tmp_path / "instance.json")
    monkeypatch.setattr(config, "V1_BACKUP_DIR", tmp_path / "data.v1.bak")
    for name in DATA_FILES:
        monkeypatch.setattr(config, name, data / FILE_NAMES[name])
    return tmp_path


@pytest.fixture
def storage(app_dir):  # noqa: ARG001 - fixture ordering only
    from app.storage import Storage
    return Storage()


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
        self.default_api_id = None
        self.default_api_hash = None
        self.removed: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.authorized = True
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

    def configure_default_api(self, api_id, api_hash):
        self.default_api_id, self.default_api_hash = api_id, api_hash

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

    async def send_message(self, key, entity, text, file=None, reply_to=None):
        if entity in self.fail_on:
            raise RuntimeError("ChatWriteForbidden")
        self.sent.append((key, entity, text))
        self.replied.append(reply_to)
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

    async def rename_session(self, old, new):
        self.renamed.append((old, new))
        # The real service reconnects the client under its new name here, and
        # that reconnect resolves credentials through the stored profiles. Fail
        # the same way it does, so an ordering mistake cannot slip through.
        if self.profile_resolver and not self.profile_resolver(new):
            raise RuntimeError("Telegram API ID / API Hash are not configured")

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
                "is_channel": True}


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
    from app.models import Account, ApiProfile, Target
    from app.models.enums import AccountState, ProbeState

    profile = ApiProfile(name="Main", api_id=123, api_hash="hash",
                         raw_state=ProbeState.ONLINE)
    storage.api_profiles.add(profile)

    account = Account(key="session_777", session_file="session_777.session",
                      telegram_id=777, username="sender",
                      api_profile_id=profile.id, raw_state=AccountState.READY)
    storage.accounts.add(account)

    target = Target(title="Channel A", username="channel_a", telegram_id=-100)
    storage.targets.add(target)
    return {"profile": profile, "account": account, "target": target}
