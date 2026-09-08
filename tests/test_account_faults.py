"""Verdicts that end an account's usefulness, and the ones that do not.

Three things had to be told apart and were not:

  * a temporary limit, which names a date and runs out;
  * a permanent block, which never does;
  * a freeze, which a health check cannot see at all because `get_me` keeps
    answering on a frozen account.

The account the user had in the field was blocked for good and reported as
"непонятный ответ антиспам-бота" in yellow, still sending. Another was frozen
and reported READY, with campaigns being started on it all day.
"""
from __future__ import annotations

import asyncio

import pytest

from app.messages import msg
from app.models import Account
from app.models.enums import AccountState, EffectiveState, SpamState
from app.services.probing import (apply_probe_result, clear_frozen,
                                  mark_frozen, probe)
from app.services.spamcheck import classify, mentions_frozen

FROZEN = msg("tg.FrozenMethodInvalidError")

BLOCKED_REPLY = ("Your account was blocked for violations of the Telegram "
                 "Terms of Service based on user reports confirmed by our "
                 "moderators.")
LIMITED_REPLY = ("I'm afraid some Telegram users found your messages "
                 "annoying and reported them. As a result, your account is "
                 "now limited until 25 Sep 2026, 15:22 UTC.")
CLEAN_REPLY = ("Good news, no limits are currently applied to your account. "
               "You're free as a bird!")


# ── reading the bot ─────────────────────────────────────────────────────
def test_blocked_is_not_an_unrecognised_reply():
    """The exact wording the user's accounts got back.

    It used to come out UNKNOWN, which paints the dot yellow and leaves the
    account sending - the opposite of what a permanent block calls for.
    """
    assert classify(BLOCKED_REPLY) == SpamState.BLOCKED


def test_a_limit_is_still_a_limit_and_not_a_block():
    assert classify(LIMITED_REPLY) == SpamState.LIMITED


def test_a_clean_account_stays_clean():
    assert classify(CLEAN_REPLY) == SpamState.CLEAN


def test_blocked_beats_limited_when_both_could_match():
    """Worst first. Wording that reads like both must not come out as the
    milder of the two - that is the direction it is dangerous to be wrong in."""
    both = BLOCKED_REPLY + " " + LIMITED_REPLY
    assert classify(both) == SpamState.BLOCKED


def test_frozen_is_recognised_in_prose_too():
    assert mentions_frozen("Your account is frozen.") is True
    assert mentions_frozen(CLEAN_REPLY) is False


# ── what the verdict does ───────────────────────────────────────────────
def test_a_block_switches_the_account_off_and_paints_it_red(
        services, storage, telegram, seeded):
    account = seeded["account"]
    telegram.bot_reply = BLOCKED_REPLY

    verdict = asyncio.run(services.spamcheck.check(account))

    assert verdict == SpamState.BLOCKED
    assert account.disabled is True, "a blocked account must stop sending"
    assert services.state.account_effective(account) == EffectiveState.BANNED


def test_a_banned_account_reads_as_banned_rather_than_merely_switched_off(
        services, storage, telegram, seeded):
    """Being switched off is what we did about it, not what happened.

    `disabled` is also what the user sets by hand, so an account that came
    back BANNED and DISABLED would be indistinguishable from one somebody
    simply turned off.
    """
    account = seeded["account"]
    account.spam_state = SpamState.BLOCKED
    account.disabled = True
    storage.accounts.upsert(account)

    assert services.state.account_effective(account) == EffectiveState.BANNED
    codes = {i.code for i in services.state.account_issues(account)}
    assert "account.banned" in codes
    assert "account.spam_limited" not in codes, "not worded as a mere limit"


def test_a_frozen_account_is_not_ready_however_green_the_check_is(
        services, storage, seeded):
    account = seeded["account"]
    mark_frozen(account, storage, services.bus, FROZEN)

    assert services.state.account_effective(account) == EffectiveState.FROZEN
    assert any(i.code == "account.frozen"
               for i in services.state.account_issues(account))


def test_a_green_health_check_does_not_lift_a_freeze(services, storage, seeded):
    """`get_me` answers on a frozen account, so "ok" proves nothing.

    This is the whole reason a freeze is a field of its own: kept in
    raw_state it would be overwritten by CHECKING on the way past and then
    by READY on the way back, every single check.
    """
    account = seeded["account"]
    mark_frozen(account, storage, services.bus, FROZEN)

    asyncio.run(probe(account, storage, services.service, services.bus))

    assert account.frozen_at, "the check came back fine and the freeze survived"
    assert services.state.account_effective(account) == EffectiveState.FROZEN


def test_a_failing_call_that_names_a_freeze_records_one():
    class Owner:
        raw_state = AccountState.READY
        last_error = None
        last_check_at = None
        frozen_at = None
        frozen_detail = ""

    owner = Owner()
    apply_probe_result(owner, {"ok": False,
                               "detail": "FrozenMethodInvalidError: nope"})
    assert owner.frozen_at


def test_a_call_that_goes_through_clears_the_freeze(services, storage, telegram,
                                                    seeded):
    """Only something that actually does something proves the freeze is over,
    and the antispam check is the cheapest such thing we have."""
    account = seeded["account"]
    mark_frozen(account, storage, services.bus, FROZEN)
    telegram.bot_reply = CLEAN_REPLY

    asyncio.run(services.spamcheck.check(account))

    assert not account.frozen_at
    assert services.state.account_effective(account) == EffectiveState.READY


def test_switching_an_account_back_on_clears_the_freeze(services, storage, seeded):
    """The user's only lever. Nothing we can ask Telegram says "no longer
    frozen", and a frozen account never gets as far as making a call that
    would prove it - so without this it would stay frozen for ever."""
    account = seeded["account"]
    mark_frozen(account, storage, services.bus, FROZEN)
    services.accounts.set_disabled(account, True)

    services.accounts.set_disabled(account, False)

    assert not account.frozen_at
    assert services.state.account_effective(account) == EffectiveState.READY


def test_clear_frozen_says_whether_anything_changed(services, storage, seeded):
    account = seeded["account"]
    assert clear_frozen(account, storage, services.bus) is False
    mark_frozen(account, storage, services.bus)
    assert clear_frozen(account, storage, services.bus) is True


# ── what it does to campaigns ───────────────────────────────────────────
def _campaign(services, seeded):
    return services.campaigns.create({
        "name": "blast", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id],
        "messages": [{"text": "hi"}], "interval_min_sec": 0, "interval_max_sec": 0})


@pytest.mark.parametrize("fault", ["frozen", "banned"])
def test_a_faulty_account_stops_its_campaigns(services, storage, seeded, fault):
    campaign = _campaign(services, seeded)
    account = seeded["account"]
    if fault == "frozen":
        mark_frozen(account, storage, services.bus)
    else:
        account.spam_state = SpamState.BLOCKED
        storage.accounts.upsert(account)

    block = services.state.run_block(campaign)

    assert block is not None
    assert block.transient is False, "waiting does not fix either of these"


def test_telegrams_own_frozen_error_is_not_said_twice(services, storage, seeded):
    """«Заморожен… Аккаунт заморожен Telegram.» read as a stutter: the error
    adds nothing to the verdict. What the antispam bot said does."""
    account = seeded["account"]
    mark_frozen(account, storage, services.bus, FROZEN)
    assert services.state.fault_issue(account).params["detail"] is None

    other = Account(key="session_8", api_profile_id=seeded["profile"].id,
                    raw_state=AccountState.READY)
    storage.accounts.add(other)
    said = msg("raw", text="Your account was frozen for spam.")
    mark_frozen(other, storage, services.bus, said)
    assert services.state.fault_issue(other).params["detail"] == said

