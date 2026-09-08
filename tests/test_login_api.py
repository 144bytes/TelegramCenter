"""Creating the API profile straight from the login form.

Adding the first account should not require a detour through Settings, but a
wrong api_id must not leave a junk profile behind either - so the profile is
created only once Telegram has accepted the keys.
"""
from __future__ import annotations

import asyncio

from app.services.login import LoginSession


class _Me:
    def __init__(self, tid=777, username="newbie"):
        self.id = tid
        self.username = username
        self.first_name = "New"
        self.last_name = "Account"
        self.phone = "+100"
        self.restricted = False


def _session(as_operator=False):
    s = LoginSession(kind="phone", phone="+100", as_operator=as_operator)
    s.key = "op_pending_x" if as_operator else "pending_x"
    return s


# ── resolving credentials ───────────────────────────────────────────────
def test_typed_keys_are_accepted_without_any_saved_profile(services, storage):
    assert storage.api_profiles.all() == []

    creds = services.login._creds(None, api_id=555, api_hash="secret")

    assert creds is not None, "keys typed by hand must be enough to start"
    assert creds["api_id"] == 555
    assert creds["explicit"] is True


def test_nothing_is_saved_before_the_login_succeeds(services, storage):
    services.login._creds(None, api_id=555, api_hash="secret")
    assert storage.api_profiles.all() == [], "a failed login must leave no junk"


def test_typed_keys_reuse_a_matching_profile(services, storage, seeded):
    creds = services.login._creds(None, api_id=123, api_hash="hash")
    assert creds["profile_id"] == seeded["profile"].id


def test_a_saved_profile_is_still_usable(services, seeded):
    creds = services.login._creds(seeded["profile"].id)
    assert creds["profile_id"] == seeded["profile"].id
    assert creds["explicit"] is False


def test_keys_without_a_hash_are_refused(services, storage):
    assert services.login._creds(None, api_id=555, api_hash="") is None
    assert services.login._creds(None, api_id=555, api_hash="   ") is None


def test_a_non_numeric_api_id_is_refused(services, storage):
    assert services.login._creds(None, api_id="not-a-number",
                                 api_hash="secret") is None


def test_no_profile_and_no_keys_means_no_login(services, storage):
    assert services.login._creds(None) is None


# ── what happens once the login works ───────────────────────────────────
def test_profile_is_created_and_bound_to_the_new_account(services, storage):
    creds = services.login._creds(None, api_id=555, api_hash="secret",
                                  api_name="Мой API")

    account = asyncio.run(services.login._persist(_session(), _Me(), creds))

    profile = storage.api_profiles.find(lambda p: p.api_id == 555)
    assert profile is not None, "the keys must be kept once they are known good"
    assert profile.name == "Мой API"
    assert profile.api_hash == "secret"
    # Telegram just accepted them, which is better evidence than a probe
    assert profile.raw_state == "ONLINE"
    assert account.api_profile_id == profile.id


def test_the_profile_is_named_after_the_phone(services, storage):
    """So the keys can be found again in the API list."""
    creds = services.login._creds(None, api_id=555, api_hash="secret")
    asyncio.run(services.login._persist(_session(), _Me(), creds))
    profile = storage.api_profiles.find(lambda p: p.api_id == 555)
    assert profile.name == "+100"


def test_a_phone_without_a_plus_still_reads_as_a_phone(services, storage):
    me = _Me()
    me.phone = "573189330969"
    creds = services.login._creds(None, api_id=555, api_hash="secret")
    asyncio.run(services.login._persist(_session(), me, creds))
    profile = storage.api_profiles.find(lambda p: p.api_id == 555)
    assert profile.name == "+573189330969"


def test_an_explicit_name_beats_the_phone(services, storage):
    creds = services.login._creds(None, api_id=555, api_hash="secret",
                                  api_name="Рабочий")
    asyncio.run(services.login._persist(_session(), _Me(), creds))
    assert storage.api_profiles.find(lambda p: p.api_id == 555).name == "Рабочий"


def test_a_nameless_phoneless_login_still_gets_a_label(services, storage):
    me = _Me()
    me.phone = ""
    session = _session()
    session.phone = ""
    creds = services.login._creds(None, api_id=555, api_hash="secret")
    asyncio.run(services.login._persist(session, me, creds))
    assert storage.api_profiles.find(lambda p: p.api_id == 555).name == "API 555"


def test_the_saved_profile_keeps_the_keys_visible(services, storage):
    """The point of saving it: the user can open it and read both values."""
    creds = services.login._creds(None, api_id=555, api_hash="secret")
    asyncio.run(services.login._persist(_session(), _Me(), creds))

    view = services.state.snapshot()["api_profiles"][0]
    assert view["api_id"] == 555
    assert view["api_hash"] == "secret"
    assert view["enabled"] is True


def test_the_same_keys_twice_do_not_make_two_profiles(services, storage):
    for tid in (777, 888):
        creds = services.login._creds(None, api_id=555, api_hash="secret")
        asyncio.run(services.login._persist(_session(), _Me(tid), creds))

    assert len(storage.api_profiles.all()) == 1
    assert len({a.api_profile_id for a in storage.accounts.all()}) == 1


def test_operator_login_keeps_the_keys_too(services, storage):
    """An operator has no profile of its own, but its session still needs
    credentials to reconnect later."""
    creds = services.login._creds(None, api_id=555, api_hash="secret")

    operator = asyncio.run(services.login._persist(_session(True), _Me(), creds))

    assert operator.key == "op_777"
    assert storage.default_api_profile() is not None
    assert services.accounts.creds_for(operator.key)["api_id"] == 555


def test_typed_keys_win_over_the_accounts_existing_profile(services, storage,
                                                           seeded):
    """Re-logging in with different keys should move the account onto them."""
    account = seeded["account"]
    assert account.api_profile_id == seeded["profile"].id

    creds = services.login._creds(None, api_id=999, api_hash="other")
    session = _session()
    updated = asyncio.run(services.login._persist(session, _Me(777), creds))

    new_profile = storage.api_profiles.find(lambda p: p.api_id == 999)
    assert updated.api_profile_id == new_profile.id


def test_picking_a_saved_profile_leaves_an_existing_binding_alone(
        services, storage, seeded):
    account = seeded["account"]
    other = services.profiles.create_api(name="Other", api_id=999, api_hash="x")

    creds = services.login._creds(other.id)
    session = _session()
    updated = asyncio.run(services.login._persist(session, _Me(account.telegram_id),
                                                  creds))

    assert updated.api_profile_id == seeded["profile"].id, \
        "an unchanged pick must not silently re-bind the account"


# ── ensure_api on its own ───────────────────────────────────────────────
def test_ensure_api_reuses_an_identical_profile(services, storage, seeded):
    same = services.profiles.ensure_api(123, "hash")
    assert same.id == seeded["profile"].id
    assert len(storage.api_profiles.all()) == 1


def test_ensure_api_creates_a_new_one_for_different_keys(services, storage,
                                                          seeded):
    fresh = services.profiles.ensure_api(321, "other", name="Second")
    assert fresh.id != seeded["profile"].id
    assert fresh.name == "Second"
    assert len(storage.api_profiles.all()) == 2


def test_an_unverified_profile_is_not_claimed_to_be_online(services, storage):
    profile = services.profiles.ensure_api(555, "secret")
    assert profile.raw_state == "UNKNOWN"
    assert profile.last_check is None


# ── ordering: the keys must be saved before the session is touched ──────
def test_the_profile_exists_before_the_session_is_renamed(services, storage,
                                                          telegram):
    """Regression: renaming reconnects the client, and that reconnect resolves
    credentials from the stored profiles - the login's own credentials are out
    of scope by then. On a fresh install with nothing saved yet, writing the
    profile after the rename failed the first account with
    "Telegram API ID / API Hash are not configured"."""
    seen = {}
    original = telegram.rename_session

    async def watching_rename(old, new):
        seen["creds"] = services.accounts.creds_for(new)
        await original(old, new)

    telegram.rename_session = watching_rename
    creds = services.login._creds(None, api_id=555, api_hash="secret")

    asyncio.run(services.login._persist(_session(), _Me(), creds))

    assert seen["creds"] is not None, "the rename had no credentials to use"
    assert seen["creds"]["api_id"] == 555


def test_a_first_login_with_typed_keys_leaves_a_usable_client(services, storage):
    """End state: the real Telegram layer can now resolve credentials for the
    session it just created."""
    from app.telegram.service import TelegramService

    real = TelegramService()
    real.profile_resolver = services.accounts.creds_for
    # nothing saved yet - exactly the state that used to raise
    assert real._resolve_creds("session_777")[0] is None

    creds = services.login._creds(None, api_id=555, api_hash="secret")
    asyncio.run(services.login._persist(_session(), _Me(), creds))

    api_id, api_hash, _proxy = real._resolve_creds("session_777")
    assert (api_id, api_hash) == (555, "secret")


def test_the_same_holds_for_an_operator_login(services, storage):
    creds = services.login._creds(None, api_id=555, api_hash="secret")
    operator = asyncio.run(services.login._persist(_session(True), _Me(), creds))
    assert operator.key == "op_777"
    assert services.accounts.creds_for("op_777")["api_id"] == 555
