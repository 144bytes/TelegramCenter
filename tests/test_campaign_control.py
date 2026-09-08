"""Duplicating a campaign, changing its schedule, and stopping it mid-run."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.models import Schedule, Target
from app.models.enums import CampaignState, ScheduleMode, TargetResultStatus
from app.services.campaigns import CampaignError, reschedule
from app.util import parse_iso
from tests.conftest import ONE_PASS


def _campaign(services, storage, seeded, count=3, gap=0, **extra):
    ids = [seeded["target"].id]
    for i in range(count - 1):
        extra_target = Target(title=f"T{i}", username=f"chan_{i}")
        storage.targets.add(extra_target)
        ids.append(extra_target.id)
    payload = {"name": "blast", "account_id": seeded["account"].id,
               "target_ids": ids, "messages": [{"text": "hello"}],
               "interval_min_sec": gap, "interval_max_sec": gap,
               "schedule": ONE_PASS}
    payload.update(extra)
    return services.campaigns.create(payload)


# ── duplication ─────────────────────────────────────────────────────────
def test_duplicate_copies_what_to_send_and_how(services, storage, seeded):
    original = _campaign(services, storage, seeded, 2, gap=7)
    original.schedule = Schedule(mode=ScheduleMode.DAILY, times=["09:00"])
    storage.campaigns.upsert(original)

    copy = services.campaigns.duplicate(original, "копия")

    assert copy.id != original.id
    assert copy.account_id == original.account_id
    assert copy.target_ids == original.target_ids
    assert copy.target_ids is not original.target_ids, "not a shared list"
    assert [m.text for m in copy.messages] == \
        [m.text for m in original.messages]
    assert {m.id for m in copy.messages}.isdisjoint(
        {m.id for m in original.messages}), "the copy rotates on its own"
    assert copy.schedule.to_dict() == original.schedule.to_dict()
    assert copy.interval_min_sec == 7 and copy.interval_max_sec == 7


def test_duplicate_starts_switched_off_and_empty(services, storage, telegram,
                                                 seeded):
    original = _campaign(services, storage, seeded, 2)
    asyncio.run(services.scheduler._run(original))
    original.last_error = "something went wrong"
    storage.campaigns.upsert(original)
    assert original.sent_total == 2

    copy = services.campaigns.duplicate(original, "копия")

    assert copy.raw_state == CampaignState.DRAFT
    assert copy.next_run_at is None
    assert copy.last_run_at is None
    assert copy.last_error is None
    assert copy.sent_total == 0
    assert [r.status for r in copy.results] == [TargetResultStatus.PENDING] * 2
    assert all(r.attempts == 0 and r.sent_at is None for r in copy.results)


def test_duplicate_names_do_not_collide(services, storage, seeded):
    original = _campaign(services, storage, seeded, 1)
    original.name = "Рассылка"
    storage.campaigns.upsert(original)

    first = services.campaigns.duplicate(original, "копия")
    second = services.campaigns.duplicate(original, "копия")
    third = services.campaigns.duplicate(first, "копия")

    assert first.name == "Рассылка (копия)"
    assert second.name == "Рассылка (копия 2)"
    assert third.name == "Рассылка (копия) (копия)"
    names = [c.name for c in storage.campaigns.all()]
    assert len(names) == len(set(names))


def test_duplicate_is_stored(services, storage, seeded):
    original = _campaign(services, storage, seeded, 1)
    copy = services.campaigns.duplicate(original, "копия")
    assert storage.campaigns.get(copy.id) is not None


def test_duplicate_goes_through_the_same_gate_as_create(services, storage,
                                                        seeded):
    """A campaign whose account has since been deleted must not be copyable
    into a second broken campaign."""
    original = _campaign(services, storage, seeded, 1)
    storage.accounts.delete(seeded["account"].id)

    with pytest.raises(CampaignError):
        services.campaigns.duplicate(original, "копия")


# ── the schedule actually changing ──────────────────────────────────────
def test_switching_from_once_to_a_loop_reschedules_at_once(
        services, storage, seeded):
    """The bug: a campaign moved off a one-shot kept waiting for the moment
    the one-shot had picked."""
    campaign = _campaign(services, storage, seeded, 1)
    campaign.schedule = Schedule(
        mode=ScheduleMode.ONCE,
        at=(datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S"))
    services.campaigns.start(campaign)
    far_away = campaign.next_run_at

    services.campaigns.update(campaign, {"schedule": {"mode": ScheduleMode.LOOP}})

    assert campaign.next_run_at != far_away
    due = parse_iso(campaign.next_run_at)
    assert due is not None
    # a loop goes through the start chain, not a fixed moment
    assert due <= datetime.now() + timedelta(seconds=1)
    assert campaign.raw_state == CampaignState.SCHEDULED


def test_a_date_already_past_runs_at_once(services, storage, seeded):
    """The same as pressing Start on it: a date that has gone means now."""
    campaign = _campaign(services, storage, seeded, 1,
                         schedule={"mode": ScheduleMode.DAILY, "times": ["09:00"]})
    services.campaigns.start(campaign)

    services.campaigns.update(campaign, {
        "schedule": {"mode": ScheduleMode.ONCE, "times": [],
                     "at": (datetime.now() - timedelta(days=1))
                     .strftime("%Y-%m-%dT%H:%M:%S")}})

    assert campaign.raw_state == CampaignState.SCHEDULED
    assert parse_iso(campaign.next_run_at) <= datetime.now()


def test_an_inactive_campaign_gains_no_next_run(services, storage, seeded):
    campaign = _campaign(services, storage, seeded, 1)
    assert campaign.raw_state == CampaignState.DRAFT

    services.campaigns.update(campaign, {"schedule": {"mode": ScheduleMode.LOOP}})

    assert campaign.next_run_at is None
    assert campaign.raw_state == CampaignState.DRAFT


def test_editing_something_else_leaves_the_schedule_alone(services, storage,
                                                          seeded):
    campaign = _campaign(services, storage, seeded, 1)
    campaign.schedule = Schedule(mode=ScheduleMode.DAILY, times=["09:00"])
    services.campaigns.start(campaign)
    before = campaign.next_run_at

    services.campaigns.update(campaign, {"name": "renamed"})

    assert campaign.next_run_at == before


def test_after_a_pass_a_loop_goes_on_and_a_one_shot_is_done():
    from app.models import Campaign

    c = Campaign(name="c", schedule=Schedule(mode=ScheduleMode.LOOP))
    c.raw_state = CampaignState.DRAFT
    reschedule(c)
    assert c.next_run_at is None, "nothing waits for an inactive campaign"

    c.raw_state = CampaignState.RUNNING
    reschedule(c)
    assert c.raw_state == CampaignState.SCHEDULED
    assert c.next_run_at is not None

    c.schedule = Schedule(mode=ScheduleMode.ONCE, at="2020-01-01T00:00:00")
    reschedule(c)
    assert c.raw_state == CampaignState.DONE


# ── stopping a run that is already going ────────────────────────────────
def test_stop_ends_the_run_at_the_next_target(services, storage, telegram,
                                              seeded, monkeypatch):
    """The bug: pause() wrote PAUSED but the loop never read it back, so a
    campaign with a 15-40 second gap kept sending for another half hour and
    then overwrote the stop."""
    campaign = _campaign(services, storage, seeded, 4)
    sent = {"n": 0}
    real_send = telegram.send_message

    async def counting_send(key, entity, text, file=None, reply_to=None):
        sent["n"] += 1
        if sent["n"] == 2:
            services.campaigns.pause(storage.campaigns.get(campaign.id))
        return await real_send(key, entity, text, file, reply_to)

    monkeypatch.setattr(telegram, "send_message", counting_send)
    asyncio.run(services.scheduler._run(campaign))

    assert sent["n"] == 2, "nothing is sent after the stop"
    assert campaign.raw_state == CampaignState.PAUSED, "the stop is not undone"
    statuses = [r.status for r in campaign.results]
    assert statuses.count(TargetResultStatus.SENT) == 2
    assert statuses.count(TargetResultStatus.PENDING) == 2, \
        "untouched targets were never attempted, so they are not failures"
    assert TargetResultStatus.FAILED not in statuses


def test_a_stop_during_the_pause_between_targets_is_noticed(
        services, storage, telegram, seeded, monkeypatch):
    campaign = _campaign(services, storage, seeded, 4, gap=30)

    async def stop_while_sleeping(seconds):  # noqa: ARG001
        services.campaigns.pause(storage.campaigns.get(campaign.id))

    monkeypatch.setattr("app.services.campaigns.asyncio.sleep",
                        stop_while_sleeping)
    asyncio.run(services.scheduler._run(campaign))

    assert len(telegram.sent) == 1
    assert campaign.raw_state == CampaignState.PAUSED


def test_a_stopped_recurring_campaign_is_not_rearmed(services, storage,
                                                     telegram, seeded,
                                                     monkeypatch):
    campaign = _campaign(services, storage, seeded, 3)
    campaign.schedule = Schedule(mode=ScheduleMode.LOOP)
    storage.campaigns.upsert(campaign)
    real_send = telegram.send_message

    async def stop_after_one(key, entity, text, file=None, reply_to=None):
        services.campaigns.pause(storage.campaigns.get(campaign.id))
        return await real_send(key, entity, text, file, reply_to)

    monkeypatch.setattr(telegram, "send_message", stop_after_one)
    asyncio.run(services.scheduler._run(campaign))

    assert campaign.raw_state == CampaignState.PAUSED
    assert campaign.next_run_at is None, "a stopped campaign waits for nobody"


# ── one campaign no longer holds up the others ──────────────────────────
def test_the_tick_starts_campaigns_without_waiting_for_them(
        services, storage, telegram, seeded):
    slow = _campaign(services, storage, seeded, 2, gap=0)
    slow.name = "slow"
    services.campaigns.start(slow)
    other = _campaign(services, storage, seeded, 1, gap=0)
    other.name = "other"
    services.campaigns.start(other)

    started: list[str] = []

    async def slow_run(c):
        started.append(c.id)
        await asyncio.sleep(0.05)

    services.scheduler._run = slow_run

    async def one_tick():
        services.scheduler._tick()
        # the tick itself returned at once; let the tasks it made finish
        await asyncio.sleep(0.15)

    asyncio.run(one_tick())

    assert set(started) == {slow.id, other.id}, \
        "a campaign already sending must not hold up one whose time has come"


def test_a_campaign_is_never_started_twice_at_once(services, storage, seeded):
    campaign = _campaign(services, storage, seeded, 1)
    services.campaigns.start(campaign)
    runs = {"n": 0}

    async def slow_run(c):  # noqa: ARG001
        runs["n"] += 1
        await asyncio.sleep(0.05)

    services.scheduler._run = slow_run

    async def two_ticks():
        services.scheduler._tick()
        services.scheduler._tick()
        await asyncio.sleep(0.15)

    asyncio.run(two_ticks())

    assert runs["n"] == 1


def test_stopping_the_scheduler_cancels_the_runs_it_started(services, storage,
                                                            seeded):
    campaign = _campaign(services, storage, seeded, 1)
    services.campaigns.start(campaign)
    finished = {"yes": False}

    async def never_ending(c):  # noqa: ARG001
        try:
            await asyncio.sleep(30)
            finished["yes"] = True
        except asyncio.CancelledError:
            raise

    services.scheduler._run = never_ending

    async def start_then_stop():
        services.scheduler._tick()
        await asyncio.sleep(0)
        await services.scheduler.stop()

    asyncio.run(start_then_stop())

    assert finished["yes"] is False
    assert services.scheduler._runs == {}
