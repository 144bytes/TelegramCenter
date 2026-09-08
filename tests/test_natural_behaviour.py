"""Things that made the app's traffic recognisable as a machine's.

Each of these came out of a real evening's log: 34 join requests for 22
channels, a flat pause distribution, and 139 failures out of 396 attempts
that nobody was there to notice.
"""
from __future__ import annotations

import asyncio

from app.models import Target
from app.models.enums import AccountState, CampaignState
from app.services.pacing import human_pause
from app.util import now_iso


def _campaign(services, storage, seeded, targets=1, **extra):
    ids = [seeded["target"].id]
    for i in range(targets - 1):
        extra_target = Target(title=f"T{i}", username=f"chan_{i}")
        storage.targets.add(extra_target)
        ids.append(extra_target.id)
    payload = {"name": "blast", "account_id": seeded["account"].id,
               "target_ids": ids, "messages": [{"text": "hi"}],
               "interval_min_sec": 0, "interval_max_sec": 0}
    payload.update(extra)
    return services.campaigns.create(payload)


def _auto_join(storage):
    storage.settings.set("campaign.auto_join", True)
    storage.settings.set("campaign.auto_join_delay_sec", 0)


# ── joining once, not every time ────────────────────────────────────────
def test_a_chat_the_account_is_in_is_not_joined_again(services, storage,
                                                      telegram, seeded):
    """Joining a chat you are already in is not an error - Telegram hands the
    chat back without a word - so nothing noticed. In the field that was 34
    join requests in an evening for 22 channels."""
    _auto_join(storage)
    telegram.members.add(seeded["target"].username)
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    assert telegram.joined == [], "already a member, nothing to ask for"
    assert len(telegram.sent) == 1, "and the message still went"


def test_a_chat_the_account_left_is_joined_again(services, storage, telegram,
                                                 seeded):
    """The reason this is asked rather than remembered. A remembered
    "already joined" would never let the account back in after the user left
    the chat by hand, and every send would fail with no visible cause."""
    _auto_join(storage)
    campaign = _campaign(services, storage, seeded)

    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.joined) == 1


def test_repeated_cycles_join_only_the_first_time(services, storage, telegram,
                                                  seeded):
    _auto_join(storage)
    # recurring, so each pass starts its targets over again
    campaign = _campaign(services, storage, seeded,
                         schedule={"mode": "LOOP"})

    asyncio.run(services.scheduler._run(campaign))
    telegram.members.add(seeded["target"].username)     # now we are in
    campaign.raw_state = CampaignState.SCHEDULED
    asyncio.run(services.scheduler._run(campaign))
    campaign.raw_state = CampaignState.SCHEDULED
    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.joined) == 1, "three cycles, one join"
    assert len(telegram.sent) == 3


def test_a_person_is_written_to_without_any_joining(services, storage,
                                                    telegram, seeded):
    _auto_join(storage)
    target = Target(title="Человек", username="someone")
    storage.targets.add(target)
    telegram.kinds["someone"] = "USER"
    campaign = services.campaigns.create({
        "name": "dm", "account_id": seeded["account"].id,
        "target_ids": [target.id], "messages": [{"text": "hi"}],
        "interval_min_sec": 0, "interval_max_sec": 0})

    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 1


# ── pauses shaped like a person's ───────────────────────────────────────
def test_a_pause_never_leaves_the_range_the_user_set():
    draws = [human_pause(15, 300) for _ in range(500)]
    assert all(15 <= d <= 300 for d in draws)


def test_short_pauses_are_the_common_case():
    """A flat draw has no rare values at all: every length is as likely as
    every other. A person is quick, quick, quick, then distracted."""
    draws = [human_pause(0, 100) for _ in range(4000)]
    below_half = sum(1 for d in draws if d < 50)

    assert below_half / len(draws) > 0.6, "most pauses are short"
    assert max(draws) > 80, "and the long ones still happen"


def test_a_degenerate_range_is_simply_that_number():
    assert human_pause(30, 30) == 30
    assert human_pause(40, 20) >= 20, "given the wrong way round, still sane"


# ── the account stops itself ────────────────────────────────────────────
def test_an_account_failing_most_of_what_it_tries_switches_itself_off(
        services, storage, telegram, seeded):
    """139 of 396 attempts failed in the field and it ran on for hours,
    because nothing was watching."""
    storage.settings.update({"campaign.error_stop_percent": 30,
                             "campaign.error_stop_min": 3})
    from app.services.pacing import Pacer
    services.scheduler.pacer = Pacer(storage)
    campaign = _campaign(services, storage, seeded, targets=6)

    async def refuse(key, entity, text, file=None, reply_to=None):
        raise RuntimeError("ChatWriteForbiddenError")

    telegram.send_message = refuse
    asyncio.run(services.scheduler._run(campaign))

    account = storage.accounts.get(seeded["account"].id)
    assert account.disabled, "the account, not the channel, is the problem"
    fresh = storage.campaigns.get(campaign.id)
    assert fresh.raw_state == CampaignState.PAUSED
    assert fresh.last_error, "and the card says what happened"
    assert account.stop_note, "the switch alone would look like the user did it"


def test_a_few_failures_are_not_enough_to_stop_anything(services, storage,
                                                        telegram, seeded):
    """Below the minimum a failure rate means nothing: one bad channel out of
    two is 50 %, and that is an ordinary Tuesday."""
    storage.settings.update({"campaign.error_stop_percent": 30,
                             "campaign.error_stop_min": 20})
    from app.services.pacing import Pacer
    services.scheduler.pacer = Pacer(storage)
    campaign = _campaign(services, storage, seeded, targets=4)

    async def refuse(key, entity, text, file=None, reply_to=None):
        raise RuntimeError("ChatWriteForbiddenError")

    telegram.send_message = refuse
    asyncio.run(services.scheduler._run(campaign))

    assert not storage.accounts.get(seeded["account"].id).disabled


def test_a_healthy_run_never_trips_the_guard(services, storage, telegram,
                                             seeded):
    storage.settings.update({"campaign.error_stop_percent": 30,
                             "campaign.error_stop_min": 2})
    from app.services.pacing import Pacer
    services.scheduler.pacer = Pacer(storage)
    campaign = _campaign(services, storage, seeded, targets=5)

    asyncio.run(services.scheduler._run(campaign))

    assert not storage.accounts.get(seeded["account"].id).disabled
    assert len(telegram.sent) == 5


def test_zero_switches_the_guard_off(services, storage, telegram, seeded):
    storage.settings.update({"campaign.error_stop_percent": 0,
                             "campaign.error_stop_min": 2})
    from app.services.pacing import Pacer
    services.scheduler.pacer = Pacer(storage)
    campaign = _campaign(services, storage, seeded, targets=4)

    async def refuse(key, entity, text, file=None, reply_to=None):
        raise RuntimeError("ChatWriteForbiddenError")

    telegram.send_message = refuse
    asyncio.run(services.scheduler._run(campaign))

    assert not storage.accounts.get(seeded["account"].id).disabled


# ── a young account is worth a word ─────────────────────────────────────
def test_an_account_added_days_ago_that_is_already_sending_is_flagged(
        services, storage, seeded):
    storage.settings.set("accounts.young_days", 7)
    _campaign(services, storage, seeded)
    account = seeded["account"]
    account.created_at = now_iso()
    storage.accounts.upsert(account)

    codes = {i.code for i in services.state.account_issues(account)}

    assert "account.young" in codes


def test_a_young_account_is_warned_about_right_after_adding(services,
                                                            storage, seeded):
    """Before it has any campaign: the warning is about the account."""
    storage.settings.set("accounts.young_days", 7)
    account = seeded["account"]
    account.created_at = now_iso()
    storage.accounts.upsert(account)

    codes = {i.code for i in services.state.account_issues(account)}

    assert "account.young" in codes


def test_the_warning_can_be_switched_off(services, storage, seeded):
    storage.settings.set("accounts.young_days", 7)
    _campaign(services, storage, seeded)
    account = seeded["account"]
    account.created_at = now_iso()
    storage.accounts.upsert(account)
    storage.settings.set("accounts.young_days", 0)

    codes = {i.code for i in services.state.account_issues(account)}

    assert "account.young" not in codes


def test_a_young_account_is_still_allowed_to_send(services, storage, seeded):
    """A warning, never a refusal: only the user knows what the account is
    for, and this one is deliberately fresh."""
    storage.settings.set("accounts.young_days", 7)
    campaign = _campaign(services, storage, seeded)
    account = seeded["account"]
    account.created_at = now_iso()
    storage.accounts.upsert(account)

    assert services.state.run_block(campaign) is None
    assert account.raw_state == AccountState.READY
