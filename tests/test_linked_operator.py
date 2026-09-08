"""An operator made from an account: a link, not a second session.

Everything but the notes is the account's - name, session, API, proxy,
state and switch. Deleting the operator leaves the account alone; the
account going takes the operator with it unless the user keeps it.
"""
from __future__ import annotations

import asyncio

import pytest

from app.messages import AppError

from app.models import NetworkProfile, Operator
from app.models.enums import EffectiveState
from app.services.checkup import CheckError
from app.services.operators import OperatorError


def _linked(services, seeded):
    return services.accounts.promote_to_operator(seeded["account"])


def test_it_shows_the_accounts_name_and_state(services, seeded):
    operator = _linked(services, seeded)

    view = services.state.operator_view(operator)

    assert view["handle"] == "@sender"
    assert view["logged_in"] is True
    assert view["effective"] == EffectiveState.READY


def test_a_second_link_to_the_same_account_is_refused(services, seeded):
    _linked(services, seeded)

    with pytest.raises(AppError, match="err.operator.exists"):
        services.accounts.promote_to_operator(seeded["account"])


def test_the_account_says_it_has_one(services, seeded):
    operator = _linked(services, seeded)

    view = services.state.account_view(seeded["account"])

    assert view["linked_operator_id"] == operator.id


def test_its_proxy_is_the_accounts_proxy(services, storage, telegram, seeded):
    """One setting, changed from either window."""
    operator = _linked(services, seeded)
    proxy = NetworkProfile(name="P", host="1.2.3.4", port=1080)
    storage.network_profiles.add(proxy)
    telegram.invalidated.clear()

    services.operators.update(operator, network_profile_id=proxy.id,
                              notes="night shift")

    account = storage.accounts.get(seeded["account"].id)
    assert account.network_profile_id == proxy.id
    assert operator.network_profile_id is None, "nothing of its own"
    assert operator.notes == "night shift"
    assert account.key in telegram.invalidated, "the one client reconnects"


def test_deleting_it_leaves_the_account_and_its_session(services, storage,
                                                        telegram, seeded):
    operator = _linked(services, seeded)

    services.operators.delete(operator.id)

    assert storage.accounts.get(seeded["account"].id) is not None
    assert telegram.dropped == []


def test_switching_the_account_off_switches_it_off(services, storage, seeded):
    operator = _linked(services, seeded)

    services.accounts.set_disabled(seeded["account"], True)

    assert services.state.operator_effective(operator) == EffectiveState.DISABLED
    with pytest.raises(OperatorError):
        asyncio.run(services.operators.dialogs(operator))


def test_its_chat_uses_the_accounts_session(services, seeded):
    operator = _linked(services, seeded)

    rows = asyncio.run(services.operators.dialogs(operator))

    assert rows, "the account's dialogs"


def test_a_live_message_reaches_its_open_chat_with_the_quote(services, seeded):
    operator = _linked(services, seeded)
    sub = services.bus.subscribe()

    services.operators._on_incoming({
        "account_key": seeded["account"].key, "peer_id": 5, "message_id": 9,
        "text": "hi", "reply_to": 4})

    events = []
    while not sub._q.empty():
        events.append(sub._q.get_nowait())
    incoming = [e.payload for e in events if e.type == "operator.message_in"]
    assert incoming and incoming[0]["operator_id"] == operator.id
    assert incoming[0]["message"]["reply_to"] == 4


def test_at_operator_turns_into_the_accounts_username(services, storage, seeded):
    operator = _linked(services, seeded)
    other = services.accounts.create(key="session_888", username="spare")
    services.accounts.set_operator(other, operator.id)

    assert storage.operator_uname(operator) == "sender"


def test_it_is_not_checked_on_its_own(services, storage, seeded):
    """The account's check is its check; the bot never hears from it twice."""
    _linked(services, seeded)

    with pytest.raises(CheckError) as refused:
        services.checkup.start("operators")

    assert refused.value.msg["code"] == "err.check.no_operators"


def test_an_independent_operator_keeps_everything_its_own(services, storage):
    op = Operator(username="support", key="op_100", telegram_id=100)
    storage.operators.add(op)

    assert storage.session_holder(op) is op
    assert storage.operator_handle(op) == "@support"
