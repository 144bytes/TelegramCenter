"""Operators connect the way accounts do.

They used to borrow whichever API profile happened to be default, which made
a broken profile invisible on the operator side and impossible to change.
"""
from __future__ import annotations

import asyncio

from app.models import Operator
from app.models.enums import AccountState, EffectiveState, ProbeState


def _logged_in(storage, seeded, key="op_100", **fields):
    fields.setdefault("api_profile_id", seeded["profile"].id)
    operator = Operator(username="support", display_name="Support", key=key,
                        session_file=f"{key}.session", telegram_id=100,
                        raw_state=AccountState.READY, **fields)
    storage.operators.add(operator)
    return operator


# ── their own connection ────────────────────────────────────────────────
def test_an_operator_uses_its_own_profile(services, storage, seeded):
    from app.models import ApiProfile

    other = ApiProfile(name="Second", api_id=999, api_hash="other",
                       raw_state=ProbeState.ONLINE)
    storage.api_profiles.add(other)
    _logged_in(storage, seeded, api_profile_id=other.id)

    creds = services.accounts.creds_for("op_100")
    assert creds["api_id"] == 999, "not the default profile, its own"


def test_an_operator_can_use_a_proxy(services, storage, seeded):
    from app.models import NetworkProfile

    proxy = NetworkProfile(name="p", host="127.0.0.1", port=1080,
                           raw_state=ProbeState.ONLINE)
    storage.network_profiles.add(proxy)
    _logged_in(storage, seeded, network_profile_id=proxy.id)

    assert services.accounts.creds_for("op_100")["proxy"] is not None


def test_a_broken_profile_shows_on_the_operator(services, storage, seeded):
    operator = _logged_in(storage, seeded)
    profile = seeded["profile"]
    profile.raw_state = ProbeState.ERROR
    profile.last_error = "AUTH_KEY_UNREGISTERED"
    storage.api_profiles.upsert(profile)

    view = services.state.operator_view(operator)
    assert view["effective"] == EffectiveState.ERROR
    assert "api.error" in [i["code"] for i in view["issues"]]


def test_a_handle_only_operator_needs_no_connection(services, storage):
    """A username is a complete way to use an operator, so asking it for an
    API profile it will never use would be noise."""
    operator = Operator(username="helper")
    storage.operators.add(operator)

    view = services.state.operator_view(operator)
    codes = [i["code"] for i in view["issues"]]
    assert codes == ["operator.not_logged_in"]
    assert view["effective"] == EffectiveState.OFFLINE


def test_promoting_an_account_carries_its_connection_over(services, storage,
                                                          app_dir, seeded):
    from app.models import NetworkProfile

    proxy = NetworkProfile(name="p", host="127.0.0.1", port=1080)
    storage.network_profiles.add(proxy)
    account = seeded["account"]
    account.network_profile_id = proxy.id
    storage.accounts.upsert(account)
    (app_dir / "sessions" / "campaign" / f"{account.key}.session").write_text("x")

    operator = services.accounts.promote_to_operator(account)

    assert operator.api_profile_id == account.api_profile_id
    assert operator.network_profile_id == proxy.id


def test_the_bound_accounts_are_listed_on_the_operator(services, storage,
                                                       seeded):
    operator = _logged_in(storage, seeded)
    account = seeded["account"]
    services.accounts.set_operator(account, operator.id)

    assert services.state.operator_view(operator)["account_ids"] == [account.id]


# ── probing ─────────────────────────────────────────────────────────────
def test_an_operator_is_probed_like_an_account(services, storage, telegram,
                                               seeded):
    operator = _logged_in(storage, seeded)

    asyncio.run(services.checkup.check_one(operator, spam=False))

    assert operator.raw_state == AccountState.READY
    assert operator.last_check_at is not None
    assert operator.last_error is None


def test_a_dead_operator_session_is_reported(services, storage, telegram,
                                             seeded):
    operator = _logged_in(storage, seeded)
    telegram.authorized = False

    asyncio.run(services.checkup.check_one(operator, spam=False))

    assert operator.raw_state == AccountState.OFFLINE
    assert operator.last_error


def test_a_handle_only_operator_is_not_probed(services, storage, telegram):
    operator = Operator(username="helper")
    storage.operators.add(operator)

    asyncio.run(services.checkup.check_one(operator, spam=False))

    assert operator.last_check_at is not None, "it was still looked at"
    assert operator.last_error is None, "but having no session is not a fault"


# ── in the check run ────────────────────────────────────────────────────
def test_check_all_covers_operators_too(services, storage, telegram, seeded):
    operator = _logged_in(storage, seeded)
    storage.settings.set("spamcheck.delay_sec", 0)

    progress = services.checkup.start(spam=False)

    assert progress["total"] == 2, "one account and one operator"
    assert storage.operators.get(operator.id).last_check_at is not None


def test_operators_are_never_asked_about_spam(services, storage, telegram,
                                              seeded):
    """They do not send campaigns, so the limit does not apply to them - and
    messaging the bot from an operator would be traffic for nothing."""
    _logged_in(storage, seeded)
    storage.settings.update({"spamcheck.enabled": True, "spamcheck.delay_sec": 0})

    services.checkup.start()

    asked = [key for key, _bot, _text in telegram.asked]
    assert asked == ["session_777"], "the account only"


def test_a_handle_only_operator_is_left_out_of_the_run(services, storage,
                                                       seeded):
    storage.operators.add(Operator(username="helper"))
    progress = services.checkup.start(spam=False)
    assert progress["total"] == 1, "nothing to probe without a session"


def test_an_operator_is_greyed_while_it_waits(services, storage, seeded):
    operator = _logged_in(storage, seeded)
    services.checkup._queue(operator, probe=True)

    assert operator.raw_state == AccountState.QUEUED
    assert services.state.operator_effective(operator) == EffectiveState.QUEUED
