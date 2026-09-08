"""One check for the buttons and the background.

The background run is the same run with one difference: it never messages
the antispam bot. At most one run goes at a time; a button pressed during
the background run stops it and starts its own, and whatever the
background had not reached gets its previous status back.
"""
from __future__ import annotations

import asyncio
import concurrent.futures

import pytest

from app.models import Account, Operator
from app.services.checkup import CheckError
from app.models.enums import AccountState, SpamState

EN_LIMITED = "Your account is now limited until 3 October 2026."


def _operator(storage, seeded, key="op_100"):
    op = Operator(username="support", key=key, session_file=f"{key}.session",
                  telegram_id=100, api_profile_id=seeded["profile"].id,
                  raw_state=AccountState.READY)
    storage.operators.add(op)
    return op


def _spam_on(storage):
    storage.settings.update({"spamcheck.enabled": True,
                             "spamcheck.include_operators": True,
                             "spamcheck.delay_sec": 0})


def test_the_background_never_messages_the_bot(services, storage, telegram,
                                               seeded):
    _operator(storage, seeded)
    _spam_on(storage)

    services.checkup.start("background")

    assert telegram.asked == []
    assert storage.accounts.get(seeded["account"].id).last_check_at is not None


def test_the_background_covers_independent_operators_and_dead_sessions(
        services, storage, telegram, seeded):
    op = _operator(storage, seeded)
    dead = Account(key="session_9", api_profile_id=seeded["profile"].id,
                   raw_state=AccountState.AUTH_DEAD)
    storage.accounts.add(dead)
    off = Account(key="session_10", api_profile_id=seeded["profile"].id,
                  disabled=True)
    storage.accounts.add(off)
    services.accounts.promote_to_operator(seeded["account"])

    progress = services.checkup.start("background")

    assert progress["total"] == 3, "two accounts and one operator"
    assert storage.operators.get(op.id).last_check_at is not None
    assert off.last_check_at is None, "off means no traffic"


def test_a_probe_leaves_the_spam_verdict_it_does_not_redo(services, storage,
                                                          seeded):
    account = seeded["account"]
    account.spam_state = SpamState.LIMITED
    account.spam_detail = {"code": "raw", "params": {"text": "until Friday"}}
    storage.accounts.upsert(account)

    services.checkup.start("accounts", spam=False)

    assert account.spam_state == SpamState.LIMITED
    assert account.spam_detail["params"]["text"] == "until Friday"


def test_a_button_stops_the_background_and_restores_what_it_had_not_reached(
        services, storage, telegram, seeded):
    runner = services.checkup
    op = _operator(storage, seeded)
    op.raw_state = AccountState.QUEUED      # the background had queued it
    storage.operators.upsert(op)
    runner._future = concurrent.futures.Future()
    runner._progress = {**runner._idle(), "running": True, "kind": "background"}
    runner._before = {op.id: (op, {"raw_state": AccountState.READY,
                                   "last_error": None})}

    progress = runner.start("accounts")

    assert op.raw_state == AccountState.READY, "back to what it was"
    assert progress["kind"] == "accounts"
    assert storage.accounts.get(seeded["account"].id).last_check_at is not None


def test_a_button_does_not_stop_another_buttons_run(services, storage,
                                                    telegram, seeded):
    runner = services.checkup
    runner._future = concurrent.futures.Future()
    runner._progress = {**runner._idle(), "running": True, "kind": "targets"}

    with pytest.raises(CheckError) as refused:
        runner.start("accounts")

    assert refused.value.msg["code"] == "err.check.busy", "said, not swallowed"
    assert runner.progress()["kind"] == "targets"
    assert storage.accounts.get(seeded["account"].id).last_check_at is None


def test_a_button_with_nothing_to_check_leaves_the_background_going(
        services, storage, telegram, seeded):
    runner = services.checkup
    seeded["account"].disabled = True
    storage.accounts.upsert(seeded["account"])
    background = concurrent.futures.Future()
    runner._future = background
    runner._progress = {**runner._idle(), "running": True, "kind": "background"}

    with pytest.raises(CheckError) as refused:
        runner.start("accounts", account_ids=[seeded["account"].id])

    assert refused.value.msg["code"] == "err.check.no_accounts"
    assert not background.cancelled()
    assert runner.progress()["kind"] == "background"


def test_the_background_does_not_stop_a_buttons_run(services, storage, seeded):
    runner = services.checkup
    runner._future = concurrent.futures.Future()
    runner._progress = {**runner._idle(), "running": True, "kind": "accounts"}

    assert runner.start("background")["kind"] == "accounts"


def test_each_page_sees_only_its_own_progress(services, storage, seeded):
    assert services.checkup.start("accounts")["kind"] == "accounts"
    assert services.checkup.start("targets")["kind"] == "targets"


def test_the_spam_button_asks_without_probing(services, storage, telegram,
                                              seeded):
    storage.settings.update({"spamcheck.delay_sec": 0})
    telegram.bot_reply = EN_LIMITED
    before = seeded["account"].last_check_at

    services.checkup.start("spam")

    account = storage.accounts.get(seeded["account"].id)
    assert account.spam_state == SpamState.LIMITED
    assert account.last_check_at == before, "no probe"


def test_a_switched_off_account_is_never_asked(services, storage, telegram,
                                               seeded):
    account = seeded["account"]
    account.disabled = True
    storage.accounts.upsert(account)

    asyncio.run(services.spamcheck.check(account))
    with pytest.raises(CheckError) as refused:
        services.checkup.start("accounts", account_ids=[account.id])

    assert refused.value.msg["code"] == "err.check.no_accounts"
    assert telegram.asked == []
    assert account.last_check_at is None


def test_connecting_is_spread_for_a_button_too(services, storage, seeded,
                                               monkeypatch):
    from app.services import checkup
    waits = []

    async def fake_spread(storage):  # noqa: ARG001
        waits.append(1)

    monkeypatch.setattr(checkup, "spread_connect", fake_spread)
    storage.accounts.add(Account(key="session_2",
                                 api_profile_id=seeded["profile"].id))

    services.checkup.start("accounts", spam=False)

    assert len(waits) == 2, "before each account, as in the background"
