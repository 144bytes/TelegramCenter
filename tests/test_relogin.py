"""Signing an account in again, and what Telegram tells us while we do.

All four of these come from one evening's log against real accounts.
"""
from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

from app import config
from app.models import ApiProfile, NetworkProfile
from app.models.enums import (AccountState, CampaignState, EffectiveState,
                              ProbeState)
from app.services.probing import mark_dead
from app.telegram.service import TelegramService


# ── a session file that is already in use ───────────────────────────────
class _Client:
    """Just enough of a Telethon client to hold a file open."""

    def __init__(self, path):
        self.handle = path.open("r+b")
        self.disconnected = False

    def is_connected(self):
        return not self.disconnected

    async def disconnect(self):
        self.handle.close()
        self.disconnected = True


def _service():
    service = TelegramService()

    async def fake_client_for(key):
        return object()

    service._client_for = fake_client_for
    return service


def test_re_login_replaces_the_session_of_a_connected_account(app_dir):  # noqa: ARG001
    """The exact failure from the log: WinError 32, "файл занят другим
    процессом", because the account being re-authorised was connected and
    nothing let go of its file."""
    folder = config.CAMPAIGN_SESSIONS_DIR
    (folder / "pending_abc.session").write_text("the new authorisation")
    (folder / "session_777.session").write_text("the old one")

    service = _service()
    live = _Client(folder / "session_777.session")
    service.clients["session_777"] = live

    asyncio.run(service.rename_session("pending_abc", "session_777"))

    assert live.disconnected, "the account let go of its file first"
    assert (folder / "session_777.session").read_text() == "the new authorisation"
    assert not (folder / "pending_abc.session").exists()


def test_a_session_that_cannot_be_replaced_is_not_reported_as_success(
        app_dir, monkeypatch):  # noqa: ARG001
    """Carrying on would mean the account kept the authorisation the user was
    replacing, while the dialog said "готово"."""
    folder = config.CAMPAIGN_SESSIONS_DIR
    (folder / "pending_abc.session").write_text("new")
    (folder / "session_777.session").write_text("old")
    service = _service()

    original = pathlib.Path.rename

    def refuse(self, target):
        if self.name.endswith(".session"):
            raise OSError(32, "занят другим процессом")
        return original(self, target)

    monkeypatch.setattr(pathlib.Path, "rename", refuse)

    with pytest.raises(RuntimeError) as err:
        asyncio.run(service.rename_session("pending_abc", "session_777"))

    assert type(err.value).__name__ == "SessionReplaceError"
    assert "session_777.session" in str(err.value)


def test_a_stale_journal_at_the_destination_is_cleared(app_dir):  # noqa: ARG001
    folder = config.CAMPAIGN_SESSIONS_DIR
    (folder / "pending_abc.session").write_text("new")
    (folder / "session_777.session").write_text("old")
    (folder / "session_777.session-journal").write_text("describes the old one")

    asyncio.run(_service().rename_session("pending_abc", "session_777"))

    assert not (folder / "session_777.session-journal").exists()


# ── a key Telegram has thrown away ──────────────────────────────────────
def _campaign(services, seeded):
    return services.campaigns.create({
        "name": "blast", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}],
        "interval_min_sec": 0, "interval_max_sec": 0})


def test_a_dead_key_found_while_sending_is_written_down(services, storage,
                                                        telegram, seeded):
    """Seen in the field coming back from a plain ResolveUsernameRequest.

    Nothing recorded it, so the account went on reporting READY and the
    campaign went on failing the same way against every channel for hours.
    """
    campaign = _campaign(services, seeded)

    class AuthKeyUnregisteredError(Exception):
        pass

    async def refuse(key, entity, text, file=None, reply_to=None):
        raise AuthKeyUnregisteredError("The key is not registered in the system")

    telegram.send_message = refuse
    asyncio.run(services.scheduler._run(campaign))

    account = storage.accounts.get(seeded["account"].id)
    assert account.raw_state == AccountState.AUTH_DEAD
    assert services.state.account_effective(account) == EffectiveState.AUTH_DEAD
    fresh = storage.campaigns.get(campaign.id)
    assert fresh.raw_state == CampaignState.PAUSED, \
        "the campaign stops instead of hammering every channel with it"


def test_a_dead_key_found_by_the_antispam_check_is_written_down(
        services, storage, telegram, seeded):
    account = seeded["account"]

    async def refuse(key, bot, text="/start", timeout=25, ack_buttons=()):
        raise RuntimeError("AuthKeyUnregisteredError: The key is not registered")

    telegram.ask_bot = refuse
    asyncio.run(services.spamcheck.check(account))

    assert account.raw_state == AccountState.AUTH_DEAD


def test_marking_a_dead_key_twice_changes_nothing(services, storage, seeded):
    account = seeded["account"]
    assert mark_dead(account, storage, services.bus, "x") is True
    assert mark_dead(account, storage, services.bus, "x") is False


# ── the login says how it is connecting ─────────────────────────────────
def _profiles(storage):
    api = ApiProfile(name="Main", api_id=1, api_hash="h")
    storage.api_profiles.add(api)
    proxy = NetworkProfile(name="Proxy_1", protocol="SOCKS5",
                           host="203.0.113.10", port=1080,
                           raw_state=ProbeState.ONLINE)
    storage.network_profiles.add(proxy)
    return api, proxy


def test_the_login_names_the_proxy_it_is_using(services, storage, telegram):
    """Otherwise the question "did the sign-in go through the proxy?" can
    only be answered by reading Telegram's own notice, hours later."""
    api, proxy = _profiles(storage)

    session = services.login.start(kind="qr", api_profile_id=api.id,
                                   network_profile_id=proxy.id)

    assert session.proxy_name == {"code": "login.via_proxy", "params": {
        "name": "Proxy_1", "address": "203.0.113.10:1080"}}
    assert session.public()["proxy_name"] == session.proxy_name


def test_a_login_with_no_proxy_says_that_too(services, storage, telegram):
    api, _proxy = _profiles(storage)
    session = services.login.start(kind="qr", api_profile_id=api.id)
    assert session.proxy_name["code"] == "login.direct"


def test_a_proxy_that_will_not_connect_is_named_in_the_error(services, storage,
                                                             telegram):
    """The bare wording says nothing about the proxy in front of it, and the
    proxy is what failed."""
    api, proxy = _profiles(storage)

    async def refuse(*args, **kwargs):
        raise ConnectionError("Connection to Telegram failed 5 time(s)")

    telegram.login_qr = refuse
    session = services.login.start(kind="qr", api_profile_id=api.id,
                                   network_profile_id=proxy.id)

    assert session.error["code"] == "err.login.proxy_failed"
    assert session.error["params"]["proxy"]["params"]["name"] == "Proxy_1"


def test_an_abandoned_login_is_given_up_on(services, storage, telegram):
    """A dialog closed without a word left the machine waiting for a code
    that was never coming, and its file in the folder for as long as the app
    ran - the sweep that removes those only gets its chance at startup."""
    api, _proxy = _profiles(storage)
    session = services.login.start(kind="qr", api_profile_id=api.id)
    session.state = "QR_WAIT"
    session.created_at = time.time() - 10_000
    path = config.CAMPAIGN_SESSIONS_DIR / f"{session.key}.session"
    path.write_text("half a login")

    services.login._sweep()

    assert session.state == "CANCELLED"
    assert not path.exists()
