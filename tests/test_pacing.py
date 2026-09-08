"""What paces a campaign beyond its own «Интервал».

The start chain spreads campaigns started together; one account sends one
message at a time; a wait Telegram names is obeyed. Nothing else holds a
campaign back - there are no ceilings per hour or per day.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.models import Target
from app.models.enums import CampaignState, TargetResultStatus
from app.services.pacing import Pacer
from app.util import parse_iso


class FloodWaitError(Exception):
    """Shaped like Telethon's: the number of seconds is the whole message."""

    def __init__(self, seconds):
        super().__init__(f"wait {seconds}s")
        self.seconds = seconds


class SlowModeWaitError(FloodWaitError):
    pass


def _campaign(services, storage, seeded, targets=1, **extra):
    ids = [seeded["target"].id]
    for i in range(targets - 1):
        extra_target = Target(title=f"T{i}", username=f"chan_{i}")
        storage.targets.add(extra_target)
        ids.append(extra_target.id)
    payload = {"name": f"blast {len(storage.campaigns.all())}",
               "account_id": seeded["account"].id, "target_ids": ids,
               "messages": [{"text": "hi"}],
               "interval_min_sec": 0, "interval_max_sec": 0,
               "schedule": {"mode": "ONCE", "at": "2020-01-01T00:00:00"}}
    payload.update(extra)
    return services.campaigns.create(payload)


# ── the start chain ─────────────────────────────────────────────────────
def test_each_started_campaign_starts_after_the_one_before(
        services, storage, seeded):
    """Start pressed on three at 22:00: 22:00:33, then 22:00:53, then 22:01:33
    - each one a random 0..60 s after the previous start."""
    storage.settings.set("campaign.start_delay_sec", 60)
    before = datetime.now().replace(microsecond=0)
    started = [services.campaigns.start(
        _campaign(services, storage, seeded, schedule={"mode": "LOOP"}))
        for _ in range(3)]

    times = [parse_iso(c.next_run_at) for c in started]
    assert times == sorted(times)
    assert before <= times[0] <= before + timedelta(seconds=61)
    for earlier, later in zip(times, times[1:]):
        assert later - earlier <= timedelta(seconds=61)


def test_a_zero_start_delay_starts_at_once(services, storage, seeded):
    campaign = services.campaigns.start(
        _campaign(services, storage, seeded, schedule={"mode": "LOOP"}))
    assert parse_iso(campaign.next_run_at) <= datetime.now()


def test_a_dated_or_daily_campaign_keeps_its_exact_moment(
        services, storage, seeded):
    storage.settings.set("campaign.start_delay_sec", 3600)
    at = (datetime.now() + timedelta(days=1)).replace(microsecond=0)
    once = services.campaigns.start(_campaign(
        services, storage, seeded,
        schedule={"mode": "ONCE", "at": at.strftime("%Y-%m-%dT%H:%M:%S")}))
    daily = services.campaigns.start(_campaign(
        services, storage, seeded, schedule={"mode": "DAILY", "times": ["09:00"]}))

    assert parse_iso(once.next_run_at) == at
    assert parse_iso(daily.next_run_at).strftime("%H:%M:%S") == "09:00:00"


def test_start_on_a_running_campaign_changes_nothing(services, storage, seeded):
    storage.settings.set("campaign.start_delay_sec", 60)
    campaign = services.campaigns.start(
        _campaign(services, storage, seeded, schedule={"mode": "LOOP"}))
    planned = campaign.next_run_at

    services.campaigns.start(campaign)

    assert campaign.next_run_at == planned


def test_after_a_restart_what_was_going_is_chained_again(
        services, storage, seeded):
    storage.settings.set("campaign.start_delay_sec", 60)
    running = _campaign(services, storage, seeded, schedule={"mode": "LOOP"})
    waiting = _campaign(services, storage, seeded, schedule={"mode": "LOOP"})
    running.raw_state = CampaignState.RUNNING
    waiting.raw_state = CampaignState.SCHEDULED
    waiting.next_run_at = (datetime.now() + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    stopped = _campaign(services, storage, seeded, schedule={"mode": "LOOP"})
    now = datetime.now().replace(microsecond=0)

    services.scheduler.resume_unfinished()

    first, second = parse_iso(running.next_run_at), parse_iso(waiting.next_run_at)
    assert running.raw_state == waiting.raw_state == CampaignState.SCHEDULED
    assert now <= first <= second <= now + timedelta(seconds=122)
    assert stopped.next_run_at is None, "a stopped campaign stays stopped"


# ── the pacer on its own ────────────────────────────────────────────────
def _guard(storage, percent=30, minimum=3):
    storage.settings.update({"campaign.error_stop_percent": percent,
                             "campaign.error_stop_min": minimum})
    return Pacer(storage)


def test_one_chat_failing_twice_is_one_failure(storage):
    """Two campaigns writing to the same chat must not count its failure
    twice."""
    pacer = _guard(storage, minimum=2)
    pacer.note_outcome("acc", "chat", False)
    pacer.note_outcome("acc", "chat", False)

    assert pacer.error_rate_exceeded("acc") is None, "one row, below the minimum"


def test_a_success_in_between_makes_the_next_failure_count(storage):
    pacer = _guard(storage, minimum=3)
    pacer.note_outcome("acc", "chat", False)
    pacer.note_outcome("acc", "chat", True)
    pacer.note_outcome("acc", "chat", False)

    exceeded = pacer.error_rate_exceeded("acc")
    assert exceeded is not None and exceeded["params"]["failed"] == 2


def test_forgetting_starts_the_count_afresh(storage):
    pacer = _guard(storage, minimum=2)
    for chat in ("a", "b", "c"):
        pacer.note_outcome("acc", chat, False)
    assert pacer.error_rate_exceeded("acc") is not None

    pacer.forget("acc")
    pacer.note_outcome("acc", "d", False)

    assert pacer.error_rate_exceeded("acc") is None, "one failure after a fresh start"


def test_nonsense_in_settings_falls_back_rather_than_breaking(storage):
    storage.settings.set("campaign.error_stop_percent", "не число")
    assert Pacer(storage).setting("error_stop_percent") == 30


# ── inside a real run ───────────────────────────────────────────────────
def test_two_campaigns_on_one_account_do_not_send_at_once(
        services, storage, telegram, seeded):
    """One send at a time per account, while the campaigns stay independent."""
    first = _campaign(services, storage, seeded)
    second = _campaign(services, storage, seeded)
    busy = {"now": 0, "most": 0}
    real_send = telegram.send_message

    async def slow_send(key, entity, text, file=None, reply_to=None):
        busy["now"] += 1
        busy["most"] = max(busy["most"], busy["now"])
        await asyncio.sleep(0.01)
        busy["now"] -= 1
        return await real_send(key, entity, text, file, reply_to)

    telegram.send_message = slow_send

    async def both():
        await asyncio.gather(services.scheduler._run(first),
                             services.scheduler._run(second))

    asyncio.run(both())

    assert len(telegram.sent) == 2
    assert busy["most"] == 1


def test_a_slow_mode_answer_is_remembered_on_the_result(
        services, storage, telegram, seeded):
    campaign = _campaign(services, storage, seeded)

    async def refuse(key, entity, text, file=None, reply_to=None):
        raise SlowModeWaitError(3333)

    telegram.send_message = refuse
    asyncio.run(services.scheduler._run(campaign))

    result = storage.campaigns.get(campaign.id).results[0]
    assert result.status == TargetResultStatus.SKIPPED, "skipped, not an error"
    assert result.retry_at, "the wait outlives a restart"
    assert result.error["code"] == "result.slow_mode"
    assert not result.excluded


def test_a_target_inside_its_wait_is_left_alone(services, storage, telegram,
                                                seeded):
    from datetime import datetime, timedelta
    campaign = _campaign(services, storage, seeded)
    campaign.results[0].retry_at = (
        datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    storage.campaigns.upsert(campaign)

    asyncio.run(services.scheduler._run(campaign))

    assert telegram.sent == [], "not knocked on before Telegram said we may"


# -- connecting, not sending --------------------------------------------
def test_accounts_do_not_all_connect_in_the_same_instant(storage, monkeypatch):
    """The background pass and start-up both touch every account in a row.

    With no gap they connected from one address inside the same second,
    which is the one thing a dozen unrelated people never do.
    """
    from app.services import pacing
    storage.settings.set("accounts.connect_spread_sec", 60)
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(pacing.asyncio, "sleep", fake_sleep)
    for _ in range(20):
        asyncio.run(pacing.spread_connect(storage))

    assert len(waits) == 20
    assert all(0 <= w <= 60 for w in waits)
    assert len(set(waits)) > 1, "a fixed gap is a pulse of its own"


def test_the_spread_can_be_switched_off(storage, monkeypatch):
    from app.services import pacing
    storage.settings.set("accounts.connect_spread_sec", 0)
    called = []
    monkeypatch.setattr(pacing.asyncio, "sleep",
                        lambda s: called.append(s))

    asyncio.run(pacing.spread_connect(storage))

    assert called == [], "zero means they all go at once, as asked"


def test_a_hand_edited_spread_falls_back_to_the_default(storage, monkeypatch):
    from app.services import pacing
    storage.settings.set("accounts.connect_spread_sec", "полминуты")
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(pacing.asyncio, "sleep", fake_sleep)
    asyncio.run(pacing.spread_connect(storage))

    assert waits and 0 <= waits[0] <= 60
