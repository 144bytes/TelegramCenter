"""Campaign scheduling and the send loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.models import Schedule, Target
from app.models.enums import (
    AccountState, CampaignState, EffectiveState, ScheduleMode,
    TargetResultStatus,
)
from app.services.campaigns import (CampaignError, next_run_after,
                                    reschedule, skipped_slots)
from tests.conftest import ONE_PASS


# ── schedule maths ──────────────────────────────────────────────────────
def test_once_returns_the_moment_then_nothing():
    at = datetime(2026, 5, 1, 12, 0, 0)
    before = at - timedelta(hours=1)
    assert next_run_after(
        Schedule(mode=ScheduleMode.ONCE, at=at.strftime("%Y-%m-%dT%H:%M:%S")),
        before) == at
    # once it has passed there is no next run: the campaign is done
    assert next_run_after(
        Schedule(mode=ScheduleMode.ONCE, at=at.strftime("%Y-%m-%dT%H:%M:%S")),
        at + timedelta(seconds=1)) is None


def test_daily_picks_the_next_listed_time_today():
    now = datetime(2026, 5, 1, 10, 0, 0)
    nxt = next_run_after(Schedule(mode=ScheduleMode.DAILY,
                                  times=["09:00", "18:30"]), now)
    assert nxt == datetime(2026, 5, 1, 18, 30)


def test_daily_rolls_over_to_tomorrow():
    now = datetime(2026, 5, 1, 20, 0, 0)
    nxt = next_run_after(Schedule(mode=ScheduleMode.DAILY,
                                  times=["09:00", "18:30"]), now)
    assert nxt == datetime(2026, 5, 2, 9, 0)


def test_daily_ignores_malformed_times():
    now = datetime(2026, 5, 1, 10, 0, 0)
    nxt = next_run_after(Schedule(mode=ScheduleMode.DAILY,
                                  times=["nonsense", "18:30"]), now)
    assert nxt == datetime(2026, 5, 1, 18, 30)


def test_a_loop_follows_its_last_pass_not_the_clock():
    assert next_run_after(Schedule(mode=ScheduleMode.LOOP), datetime.now()) is None


def test_a_loop_waits_one_interval_after_its_last_channel(services, seeded):
    campaign = services.campaigns.create({
        "name": "c", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}],
        "interval_min_sec": 600, "interval_max_sec": 900})
    campaign.raw_state = CampaignState.RUNNING
    now = datetime(2026, 5, 1, 10, 0, 0)

    reschedule(campaign, now)

    nxt = datetime.fromisoformat(campaign.next_run_at)
    assert campaign.raw_state == CampaignState.SCHEDULED
    assert now + timedelta(seconds=600) <= nxt <= now + timedelta(seconds=900)


def test_a_daily_slot_that_came_during_a_pass_is_reported():
    schedule = Schedule(mode=ScheduleMode.DAILY, times=["09:00", "10:00"])
    started = datetime(2026, 5, 1, 9, 0, 3)

    late = datetime(2026, 5, 1, 10, 30)
    assert skipped_slots(schedule, started, late) == ["10:00"]
    assert skipped_slots(schedule, started, datetime(2026, 5, 1, 9, 40)) == []


@pytest.mark.parametrize("bad", ["25:99", "9-30", "", "24:00", "12:60"])
def test_a_daily_time_must_be_hh_mm(services, seeded, bad):
    with pytest.raises(CampaignError, match="err.schedule.bad_time"):
        services.campaigns.create({
            "name": "c", "account_id": seeded["account"].id,
            "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}],
            "schedule": {"mode": "DAILY", "times": ["09:00", bad]}})


def test_once_needs_a_date(services, seeded):
    with pytest.raises(CampaignError):
        services.campaigns.create({
            "name": "c", "account_id": seeded["account"].id,
            "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}],
            "schedule": {"mode": "ONCE", "at": None}})


# ── editing ─────────────────────────────────────────────────────────────
def test_campaign_owns_its_text(services, storage, seeded):
    """The text is the campaign's own copy. Nothing outside it can change
    what an existing campaign says."""
    campaign = services.campaigns.create({
        "name": "c", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id],
        "messages": [{"text": "Original text"}],
    })
    stored = storage.campaigns.get(campaign.id)
    assert [m.text for m in stored.messages] == ["Original text"]


def test_a_campaign_with_no_text_is_saved_but_cannot_run(services, seeded):
    """An empty campaign is a campaign being written, not an error. It just
    says in red that it has nothing to send."""
    campaign = services.campaigns.create({
        "name": "c", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id], "messages": [{"text": "  "}],
    })

    assert campaign.messages == []
    assert services.state.campaign_effective(campaign) == EffectiveState.ERROR
    with pytest.raises(CampaignError) as err:
        services.campaigns.start(campaign)
    assert err.value.msg["params"]["reason"]["code"] == "issue.campaign.no_text"


def test_campaign_rejects_inverted_gap(services, seeded):
    with pytest.raises(CampaignError):
        services.campaigns.create({
            "name": "c", "account_id": seeded["account"].id,
            "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}],
            "interval_min_sec": 90, "interval_max_sec": 10,
        })


def test_results_follow_the_target_list(services, storage, seeded):
    second = Target(title="B", username="channel_b")
    storage.targets.add(second)
    campaign = services.campaigns.create({
        "name": "c", "account_id": seeded["account"].id,
        "target_ids": [seeded["target"].id], "messages": [{"text": "hi"}],
    })
    assert len(campaign.results) == 1

    services.campaigns.update(campaign, {
        "target_ids": [seeded["target"].id, second.id]})
    assert {r.target_id for r in campaign.results} == {seeded["target"].id, second.id}

    services.campaigns.update(campaign, {"target_ids": [second.id]})
    assert [r.target_id for r in campaign.results] == [second.id]


# ── the send loop ───────────────────────────────────────────────────────
def _campaign_with_targets(services, storage, seeded, count=3, gap=0):
    ids = [seeded["target"].id]
    for i in range(count - 1):
        extra = Target(title=f"T{i}", username=f"chan_{i}")
        storage.targets.add(extra)
        ids.append(extra.id)
    return services.campaigns.create({
        "name": "blast", "account_id": seeded["account"].id,
        "target_ids": ids, "messages": [{"text": "hello"}],
        "interval_min_sec": gap, "interval_max_sec": gap,
        "schedule": ONE_PASS,
    })


def test_run_sends_to_every_target(services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 3)
    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 3
    assert campaign.sent_count == 3
    assert campaign.raw_state == CampaignState.DONE


def test_failed_target_is_marked_and_retried_next_run(
        services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 3)
    failing = storage.targets.get(campaign.target_ids[1])
    telegram.fail_on.add(failing.username)

    asyncio.run(services.scheduler._run(campaign))

    result = campaign.result_for(failing.id)
    assert result.status == TargetResultStatus.FAILED
    assert result.attempts == 1
    assert campaign.sent_count == 2

    # a second run retries only the failure
    telegram.fail_on.clear()
    telegram.sent.clear()
    campaign.raw_state = CampaignState.SCHEDULED
    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 1
    assert campaign.result_for(failing.id).status == TargetResultStatus.SENT
    assert campaign.result_for(failing.id).attempts == 2


def test_dependency_failure_stops_the_run_immediately(
        services, storage, telegram, seeded, monkeypatch):
    """Agreed behaviour: stop at once, and leave untouched targets PENDING —
    they were never attempted, so they are not failures."""
    campaign = _campaign_with_targets(services, storage, seeded, 4)
    profile = seeded["profile"]

    real_send = telegram.send_message
    calls = {"n": 0}

    async def breaking_send(key, entity, text, file=None, reply_to=None):
        calls["n"] += 1
        if calls["n"] == 2:
            profile.enabled = False
            storage.api_profiles.upsert(profile)
        return await real_send(key, entity, text, file, reply_to)

    monkeypatch.setattr(telegram, "send_message", breaking_send)
    asyncio.run(services.scheduler._run(campaign))

    assert calls["n"] == 2, "sending must stop as soon as the dependency dies"
    assert campaign.raw_state == CampaignState.PAUSED
    statuses = [r.status for r in campaign.results]
    assert statuses.count(TargetResultStatus.SENT) == 2
    assert statuses.count(TargetResultStatus.PENDING) == 2
    assert TargetResultStatus.FAILED not in statuses


def test_run_refuses_when_account_not_ready(services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 2)
    account = seeded["account"]
    account.raw_state = AccountState.OFFLINE
    storage.accounts.upsert(account)

    asyncio.run(services.scheduler._run(campaign))

    assert telegram.sent == []
    assert campaign.raw_state == CampaignState.PAUSED
    assert campaign.last_error


def test_missing_target_is_skipped_not_failed(services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 2)
    gone = campaign.target_ids[1]
    storage.targets.delete(gone)

    asyncio.run(services.scheduler._run(campaign))

    assert campaign.result_for(gone).status == TargetResultStatus.SKIPPED
    assert len(telegram.sent) == 1


def test_recurring_campaign_rearms_after_a_full_pass(
        services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 2)
    campaign.schedule = Schedule(mode=ScheduleMode.LOOP)
    storage.campaigns.upsert(campaign)

    asyncio.run(services.scheduler._run(campaign))

    assert campaign.raw_state == CampaignState.SCHEDULED
    assert campaign.next_run_at is not None
    # the next cycle starts from a clean slate
    assert all(r.status == TargetResultStatus.PENDING for r in campaign.results)
    assert len(telegram.sent) == 2


def test_gap_between_targets_is_inside_the_range(services, storage, seeded,
                                                 monkeypatch):
    campaign = _campaign_with_targets(services, storage, seeded, 3)
    campaign.interval_min_sec, campaign.interval_max_sec = 5, 9
    storage.campaigns.upsert(campaign)
    slept = _record_pauses(services, monkeypatch)

    asyncio.run(services.scheduler._run(campaign))

    assert len(slept) == 2, "one pass: no pause after the last channel"
    assert all(5 <= s <= 9 for s in slept)


def _record_pauses(services, monkeypatch) -> list[float]:
    slept: list[float] = []

    async def fake_wait(c, seconds):
        slept.append(seconds)
        return True

    monkeypatch.setattr(services.scheduler, "_wait", fake_wait)
    return slept


def test_the_interval_follows_every_channel_whatever_happened_to_it(
        services, storage, telegram, seeded, monkeypatch):
    """Sent, failed, switched off, deleted or told to wait by Telegram - each
    one is followed by the pause, so a list that refuses is never a burst."""
    campaign = _campaign_with_targets(services, storage, seeded, 5)
    campaign.interval_min_sec, campaign.interval_max_sec = 5, 9
    ids = campaign.target_ids
    telegram.fail_on.add(storage.targets.get(ids[1]).username)
    campaign.result_for(ids[2]).excluded = True
    campaign.result_for(ids[3]).retry_at = (
        datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    storage.targets.delete(ids[4])
    storage.campaigns.upsert(campaign)
    slept = _record_pauses(services, monkeypatch)

    asyncio.run(services.scheduler._run(campaign))

    assert len(slept) == 4, "after each of the five but the last"
    assert len(telegram.sent) == 1


def test_stopping_during_the_pause_ends_the_pass(
        services, storage, telegram, seeded, monkeypatch):
    campaign = _campaign_with_targets(services, storage, seeded, 3)

    async def stop_now(c, seconds):
        services.campaigns.pause(c)
        return await original(c, seconds)

    original = services.scheduler._wait
    monkeypatch.setattr(services.scheduler, "_wait", stop_now)
    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 1
    assert campaign.raw_state == CampaignState.PAUSED


def test_resume_unfinished_moves_running_back_to_scheduled(
        services, storage, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 1)
    campaign.raw_state = CampaignState.RUNNING
    campaign.next_run_at = None
    storage.campaigns.upsert(campaign)

    services.scheduler.resume_unfinished()

    reloaded = storage.campaigns.get(campaign.id)
    assert reloaded.raw_state == CampaignState.SCHEDULED
    assert reloaded.next_run_at is not None


def test_start_refuses_a_campaign_with_no_targets(services, seeded):
    campaign = services.campaigns.create({
        "name": "empty", "account_id": seeded["account"].id,
        "target_ids": [], "messages": [{"text": "hi"}],
    })
    with pytest.raises(CampaignError):
        services.campaigns.start(campaign)


def test_reset_clears_results(services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 2)
    asyncio.run(services.scheduler._run(campaign))
    assert campaign.sent_count == 2

    services.campaigns.reset(campaign)
    assert campaign.sent_count == 0
    assert all(r.attempts == 0 for r in campaign.results)
    assert campaign.raw_state == CampaignState.DRAFT


# ── the counter the user actually reads ─────────────────────────────────
def test_sent_total_survives_recurring_cycles(services, storage, telegram, seeded):
    """A repeating campaign rearms its per-target results for the next pass, so
    the per-pass count drops back to zero. The cumulative total must not."""
    campaign = _campaign_with_targets(services, storage, seeded, 2)
    campaign.schedule = Schedule(mode=ScheduleMode.LOOP)
    storage.campaigns.upsert(campaign)

    asyncio.run(services.scheduler._run(campaign))
    assert campaign.sent_total == 2
    assert campaign.sent_count == 0, "results were rearmed for the next cycle"

    campaign.raw_state = CampaignState.SCHEDULED
    asyncio.run(services.scheduler._run(campaign))

    assert campaign.sent_total == 4, "the total must keep climbing across cycles"
    assert len(telegram.sent) == 4


def test_recurring_campaigns_are_flagged_as_such(services, storage, seeded):
    once = _campaign_with_targets(services, storage, seeded, 1)
    assert once.is_recurring is False

    once.schedule = Schedule(mode=ScheduleMode.DAILY, times=["09:00"])
    assert once.is_recurring is True

    once.schedule = Schedule(mode=ScheduleMode.LOOP)
    assert once.is_recurring is True


def test_one_shot_campaign_total_matches_its_targets(services, storage, telegram,
                                                     seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 3)
    asyncio.run(services.scheduler._run(campaign))
    assert campaign.sent_total == 3
    assert campaign.sent_count == 3


def test_reset_clears_the_cumulative_total(services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 2)
    asyncio.run(services.scheduler._run(campaign))
    assert campaign.sent_total == 2

    services.campaigns.reset(campaign)
    assert campaign.sent_total == 0


def test_sent_total_is_exposed_to_the_ui(services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 2)
    asyncio.run(services.scheduler._run(campaign))

    view = services.state.campaign_view(campaign)
    assert view["sent_total"] == 2
    assert view["is_recurring"] is False


def test_failed_sends_do_not_inflate_the_total(services, storage, telegram, seeded):
    campaign = _campaign_with_targets(services, storage, seeded, 3)
    telegram.fail_on.add(storage.targets.get(campaign.target_ids[1]).username)

    asyncio.run(services.scheduler._run(campaign))

    assert campaign.sent_total == 2
    assert campaign.failed_count == 1
