"""The chosen proxy applies from the moment it is chosen.

A proxy that only starts being used after the account is saved is a proxy that
leaked the real address at the one moment it mattered: Telegram's "new login
from ..." notice names the city the sign-in came from, and it named the wrong
one. The stored record says how the account connected *before*; the dialog
says how the user wants it to connect now.
"""
from __future__ import annotations

from app.models import ApiProfile, NetworkProfile
from app.models.enums import ProbeState
from app.telegram.service import TelegramService


def _service_with_account(proxy=None):
    """A TelegramService whose resolver answers for one existing account."""
    service = TelegramService()

    def resolver(key):
        if key != "session_777":
            return None
        return {"api_id": 10, "api_hash": "stored_hash", "proxy": proxy}

    service.profile_resolver = resolver
    return service


def test_an_account_connects_through_its_own_proxy():
    service = _service_with_account(proxy=("socks5", "1.2.3.4", 1080))
    assert service._resolve_creds("session_777") == \
        (10, "stored_hash", ("socks5", "1.2.3.4", 1080))


def test_a_login_in_progress_wins_over_the_stored_record():
    """Signing an existing account in again, through a proxy picked in the
    dialog. The record still names the old connection, and following it would
    send the sign-in out over the old address."""
    service = _service_with_account(proxy=("socks5", "old", 1080))
    service._login_ctx["session_777"] = (99, "typed_hash", ("socks5", "new", 9050))

    assert service._resolve_creds("session_777") == \
        (99, "typed_hash", ("socks5", "new", 9050))


def test_a_login_only_speaks_for_its_own_session():
    """The context is keyed by session: one account signing in must not
    decide how another account's client connects."""
    service = _service_with_account(proxy=("socks5", "mine", 1080))
    service._login_ctx["pending_abc"] = (99, "typed_hash", ("socks5", "new", 9050))

    assert service._resolve_creds("session_777")[2] == ("socks5", "mine", 1080)


def test_a_brand_new_account_uses_what_the_dialog_chose():
    service = _service_with_account()
    service._login_ctx["pending_abc"] = (99, "typed_hash", ("socks5", "new", 9050))

    assert service._resolve_creds("pending_abc") == \
        (99, "typed_hash", ("socks5", "new", 9050))


def test_a_session_nobody_owns_has_nothing_to_connect_with():
    service = _service_with_account()
    assert service._resolve_creds("session_other") == (None, None, None)


# ── the record keeps up ─────────────────────────────────────────────────
def test_changing_the_proxy_drops_the_pooled_client(services, storage, telegram,
                                                    seeded):
    """A pooled client keeps the proxy it was built with for ever. Left alive
    it would keep talking from the old address while the new connection came
    up from the new one - one auth key on two addresses, which is what
    Telegram kills a session for."""
    proxy = NetworkProfile(name="P", host="1.2.3.4", port=1080,
                           raw_state=ProbeState.ONLINE)
    storage.network_profiles.add(proxy)

    services.accounts.update(seeded["account"], network_profile_id=proxy.id)

    assert seeded["account"].key in telegram.invalidated


def test_the_login_writes_the_chosen_proxy_onto_the_account(services, storage,
                                                            telegram):
    """Chosen before the account existed, so it has to be carried across."""
    proxy = NetworkProfile(name="P", host="1.2.3.4", port=1080,
                           raw_state=ProbeState.ONLINE)
    storage.network_profiles.add(proxy)
    profile = ApiProfile(name="Main", api_id=1, api_hash="h")
    storage.api_profiles.add(profile)

    session = services.login.start(kind="qr", api_profile_id=profile.id,
                                   network_profile_id=proxy.id)

    assert telegram.login_proxy == proxy.as_telethon_proxy(), \
        "the sign-in itself went through it"
    account = storage.accounts.find(lambda a: a.telegram_id == telegram.login_id)
    assert account is not None
    assert account.network_profile_id == proxy.id
    assert session.key
