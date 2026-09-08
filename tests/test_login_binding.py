"""What a finished login leaves behind.

The bug this covers: an account could end up working while its link to the API
profile was empty, and an operator got even less - no profile, and a raw state
still saying OFFLINE right after Telegram had accepted it. Both kinds have to
come out of a login in the same shape.
"""
from __future__ import annotations

import asyncio

from app.models import NetworkProfile
from app.models.enums import AccountState
from app.services.login import LoginSession


class _Me:
    def __init__(self, tid=777, username="newbie"):
        self.id = tid
        self.username = username
        self.first_name = "New"
        self.last_name = "Account"
        self.phone = "+100"
        self.restricted = False


def _session(as_operator=False, network_profile_id=None, target_id=None):
    s = LoginSession(kind="phone", phone="+100", as_operator=as_operator,
                     network_profile_id=network_profile_id, target_id=target_id)
    s.key = "op_pending_x" if as_operator else "pending_x"
    return s


def _proxy(storage, name="DE"):
    prof = NetworkProfile(name=name, host="10.0.0.1", port=1080)
    storage.network_profiles.add(prof)
    return prof


def _finish(services, s, creds):
    return asyncio.run(services.login._persist(s, _Me(), creds))


# ── the profile typed into the form gets bound ──────────────────────────
def test_an_account_keeps_the_profile_created_during_login(services, storage):
    creds = services.login._creds(None, api_id=555, api_hash="secret",
                                  api_name="From the form")

    account = _finish(services, _session(), creds)

    assert account.api_profile_id, "an account that works must know its keys"
    profile = storage.api_profiles.get(account.api_profile_id)
    assert profile is not None and profile.api_id == 555


def test_an_operator_keeps_the_profile_created_during_login(services, storage):
    """The operator branch used not to set this at all, so a logged-in
    operator had working chats and no idea which keys they ran on."""
    creds = services.login._creds(None, api_id=555, api_hash="secret",
                                  api_name="From the form")

    operator = _finish(services, _session(as_operator=True), creds)

    assert operator.api_profile_id
    assert storage.api_profiles.get(operator.api_profile_id).api_id == 555


def test_a_saved_profile_is_bound_to_an_operator_too(services, storage, seeded):
    creds = services.login._creds(seeded["profile"].id)

    operator = _finish(services, _session(as_operator=True), creds)

    assert operator.api_profile_id == seeded["profile"].id


# ── and the state it comes out in ───────────────────────────────────────
def test_an_operator_is_ready_right_after_a_successful_login(services, storage,
                                                              seeded):
    """Telegram just accepted it, so it is ready. Leaving the default OFFLINE
    in place is what showed a fresh operator as "not connected"."""
    creds = services.login._creds(seeded["profile"].id)

    operator = _finish(services, _session(as_operator=True), creds)

    assert operator.raw_state == AccountState.READY
    assert operator.last_error is None
    assert operator.last_check_at is not None
    assert services.state.operator_view(operator)["effective"] == "READY"


def test_the_two_kinds_come_out_in_the_same_state(services, storage, seeded):
    creds = services.login._creds(seeded["profile"].id)

    account = _finish(services, _session(), creds)
    operator = _finish(services, _session(as_operator=True), creds)

    for owner in (account, operator):
        assert owner.raw_state == AccountState.READY
        assert owner.api_profile_id == seeded["profile"].id
        assert owner.last_check_at is not None


# ── the proxy chosen in the form ────────────────────────────────────────
def test_the_chosen_proxy_is_bound_to_the_account(services, storage, seeded):
    prof = _proxy(storage)
    creds = services.login._creds(seeded["profile"].id)

    account = _finish(services, _session(network_profile_id=prof.id), creds)

    assert account.network_profile_id == prof.id


def test_the_chosen_proxy_is_bound_to_the_operator(services, storage, seeded):
    prof = _proxy(storage)
    creds = services.login._creds(seeded["profile"].id)

    operator = _finish(services, _session(as_operator=True,
                                          network_profile_id=prof.id), creds)

    assert operator.network_profile_id == prof.id


def test_the_authorisation_itself_goes_through_the_proxy(services, storage,
                                                          telegram, seeded):
    """Binding the proxy afterwards is not enough: if the sign-in went direct,
    Telegram already saw this machine's address for that account."""
    prof = _proxy(storage)

    services.login.start(kind="phone", phone="+100",
                         api_profile_id=seeded["profile"].id,
                         network_profile_id=prof.id)

    assert telegram.login_proxy is not None
    assert telegram.login_proxy[1] == "10.0.0.1"


def test_a_login_with_no_proxy_stays_direct(services, storage, telegram, seeded):
    services.login.start(kind="phone", phone="+100",
                         api_profile_id=seeded["profile"].id)

    assert telegram.login_proxy is None


def test_a_missing_proxy_stops_the_login(services, storage, seeded):
    """Quietly falling back to a direct connection would leak the address the
    proxy was picked to hide."""
    s = services.login.start(kind="phone", phone="+100",
                             api_profile_id=seeded["profile"].id,
                             network_profile_id="net_gone")

    assert s.state == "ERROR"
    assert "прокси" in (s.error or "").lower()


def test_an_unusable_proxy_stops_the_login(services, storage, seeded):
    prof = NetworkProfile(name="broken", host="", port=0)
    storage.network_profiles.add(prof)

    s = services.login.start(kind="phone", phone="+100",
                             api_profile_id=seeded["profile"].id,
                             network_profile_id=prof.id)

    assert s.state == "ERROR"


# ── signing in *into* a handle-only operator ────────────────────────────
def _handle_only(storage, username="ebyte2"):
    from app.models import Operator

    op = Operator(username=username)
    storage.operators.add(op)
    return op


def test_signing_in_fills_the_record_in_rather_than_adding_one(services, storage,
                                                                seeded):
    """The whole point: one record, upgraded - not a second one beside it."""
    op = _handle_only(storage)
    creds = services.login._creds(seeded["profile"].id)

    result = _finish(services, _session(as_operator=True, target_id=op.id), creds)

    assert result.id == op.id, "the same record, filled in"
    assert len(storage.operators.all()) == 1
    assert result.logged_in and result.raw_state == AccountState.READY
    assert result.api_profile_id == seeded["profile"].id


def test_the_record_takes_the_real_handle(services, storage, seeded):
    op = _handle_only(storage, "ebyte2")
    creds = services.login._creds(seeded["profile"].id)

    result = _finish(services, _session(as_operator=True, target_id=op.id), creds)

    assert result.username == "newbie", "it is that account now"


def test_a_typed_handle_survives_when_telegram_reports_none(services, storage,
                                                             seeded):
    op = _handle_only(storage, "ebyte2")
    creds = services.login._creds(seeded["profile"].id)
    s = _session(as_operator=True, target_id=op.id)

    me = _Me()
    me.username = None
    result = asyncio.run(services.login._persist(s, me, creds))

    assert result.username == "ebyte2", "nothing better to put there"


def test_signing_in_as_an_account_already_saved_is_refused(services, storage,
                                                            telegram, seeded):
    """Two records for one Telegram account is the confusion this avoids."""
    from app.models import Operator
    from app.services.login import LoginConflict

    storage.operators.add(Operator(username="already", key="op_777",
                                   telegram_id=777))
    op = _handle_only(storage)
    creds = services.login._creds(seeded["profile"].id)

    try:
        _finish(services, _session(as_operator=True, target_id=op.id), creds)
    except LoginConflict as exc:
        assert "@already" in str(exc)
    else:                                    # pragma: no cover - failure path
        raise AssertionError("a duplicate operator must be refused")

    assert len(storage.operators.all()) == 2, "nothing was created or merged"
    assert storage.operators.get(op.id).logged_in is False


def test_a_refused_login_leaves_no_session_behind(services, storage, telegram,
                                                   seeded):
    """Refusing after the rename would drop an orphan file that the next
    rescan would adopt as yet another operator."""
    from app.models import Operator
    from app.services.login import LoginConflict

    storage.operators.add(Operator(username="already", key="op_777",
                                   telegram_id=777))
    op = _handle_only(storage)
    creds = services.login._creds(seeded["profile"].id)

    try:
        _finish(services, _session(as_operator=True, target_id=op.id), creds)
    except LoginConflict:
        pass

    assert telegram.renamed == [], "the temporary session was never promoted"


def test_signing_in_again_as_the_same_account_is_allowed(services, storage,
                                                          seeded):
    """Re-authorising the operator you already have is not a clash."""
    from app.models import Operator

    op = Operator(username="newbie", key="op_777", telegram_id=777)
    storage.operators.add(op)
    creds = services.login._creds(seeded["profile"].id)

    result = _finish(services, _session(as_operator=True, target_id=op.id), creds)

    assert result.id == op.id
    assert len(storage.operators.all()) == 1


def test_a_plain_login_adopts_the_handle_written_down_earlier(services, storage,
                                                               seeded):
    """Someone noted @newbie down to write to, then signed in as it. That is
    one operator, not two near-identical records."""
    op = _handle_only(storage, "newbie")
    creds = services.login._creds(seeded["profile"].id)

    result = _finish(services, _session(as_operator=True), creds)

    assert result.id == op.id
    assert len(storage.operators.all()) == 1
    assert result.logged_in


def test_a_plain_login_keeps_an_unrelated_handle(services, storage, seeded):
    """A different handle is a different person and stays where it is."""
    _handle_only(storage, "someone_else")
    creds = services.login._creds(seeded["profile"].id)

    result = _finish(services, _session(as_operator=True), creds)

    assert result.username == "newbie"
    assert len(storage.operators.all()) == 2


def test_a_signed_in_handle_is_never_taken_over(services, storage, seeded):
    """Only records still waiting for a session are adopted."""
    from app.models import Operator

    storage.operators.add(Operator(username="newbie", key="op_999",
                                   telegram_id=999))
    creds = services.login._creds(seeded["profile"].id)

    result = _finish(services, _session(as_operator=True), creds)

    assert result.telegram_id == 777
    assert len(storage.operators.all()) == 2, "the other session is left alone"
