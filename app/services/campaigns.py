"""Campaigns: editing, scheduling, and the send loop.

Pressing Start puts a campaign in the start chain: each one starts a
random 0..«Задержка старта» seconds after the one started before it.
A pass then goes channel by channel with a random «Интервал» pause after
every channel, whatever happened to it. «По кругу» repeats passes until
stopped, «Один раз» makes one pass at a date, «Ежедневно» one pass at
each listed time. Campaigns never wait for each other.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime, timedelta

from .. import config
from ..logging import LOG
from ..models import Campaign, CampaignMessage, Schedule
from ..models.enums import CampaignState, ScheduleMode, TargetResultStatus
from ..state.manager import OPERATOR_TOKEN, mentions_operator
from ..messages import AppError, msg, text_of
from ..telegram.errors import Fault, wait_seconds
from ..util import now_iso, parse_iso, until_text
from .delivery import DeliveryError
from .pacing import Pacer, human_pause
from .probing import hold_for_flood, mark_dead, mark_frozen, switch_off

MOD = "campaigns"
TICK_SEC = 5

# A picture is uploaded a moment before the campaign that names it is
# saved, so a sweep is only kept off the file for that moment.
MEDIA_GRACE_SEC = 60

# The most texts one campaign may hold: a floor against a paste that
# was never meant to be one, not a setting.
MESSAGE_CAP = 100

# Why a campaign stopped: what is missing and what to do.
NO_TARGETS_LEFT = msg("stop.no_targets")
NO_ACCOUNT_LEFT = msg("stop.no_account")
NO_OPERATOR_LEFT = msg("stop.no_operator")
NO_TEXT_LEFT = msg("stop.no_text")
# «Banned» from every public group, not from one chat: the account stops.
PUBLIC_GROUPS_BANNED = msg("note.public_groups")

STAMP = "%Y-%m-%dT%H:%M:%S"

# How a pass ended.
DONE, STOPPED, CANCELLED = "done", "stopped", "cancelled"
# how one send ended, for the channel loop
SENT, REFUSED, FLOODED = "sent", "refused", "flooded"


def tag(storage, c: Campaign) -> str:
    """A campaign as the log names it: its name and its account, since two
    accounts' campaigns are often called the same."""
    account = storage.accounts.get(c.account_id)
    return f"{c.name!r} [{account.handle if account else c.account_id or '-'}]"



class CampaignError(AppError):
    """Why a campaign cannot be saved or started."""


def clean_messages(raw) -> list[CampaignMessage]:
    """The texts a campaign will hold, from whatever the editor sent.

    Blank boxes are dropped - the editor always offers one spare. An id
    that came back is kept: the rotation remembers it.
    """
    out: list[CampaignMessage] = []
    for item in raw or []:
        message = (item if isinstance(item, CampaignMessage)
                   else CampaignMessage.from_dict(item))
        # A picture with no caption is a perfectly good message; an empty box
        # with nothing at all is the editor's spare row.
        if message.text.strip() or message.file:
            out.append(message)
    return out


# ── schedule maths ──────────────────────────────────────────────────────
DAILY_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def daily_times(schedule: Schedule) -> list[tuple[int, int]]:
    """The valid «ЧЧ:ММ» entries of a daily schedule, as (hour, minute)."""
    out = []
    for raw in schedule.times or []:
        m = DAILY_TIME.fullmatch(str(raw).strip())
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


def next_run_after(schedule: Schedule, after: datetime) -> datetime | None:
    """The next fixed moment of a dated or daily schedule. None for a loop:
    its next pass follows the previous one, not the clock."""
    if schedule.mode == ScheduleMode.ONCE:
        at = parse_iso(schedule.at)
        return at if at and at > after else None
    if schedule.mode == ScheduleMode.DAILY:
        candidates = []
        for hh, mm in daily_times(schedule):
            today = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
            candidates.append(today if today > after else today + timedelta(days=1))
        return min(candidates) if candidates else None
    return None


def skipped_slots(schedule: Schedule, since: datetime | None,
                  until: datetime) -> list[str]:
    """Daily times that came while a pass was still going."""
    if schedule.mode != ScheduleMode.DAILY or since is None:
        return []
    out = []
    for hh, mm in daily_times(schedule):
        slot = since.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= since:
            slot += timedelta(days=1)
        if slot <= until:
            out.append(f"{hh:02d}:{mm:02d}")
    return out


def reschedule(c: Campaign, now: datetime | None = None) -> None:
    """When the next pass is due, after a pass has ended.

    A loop waits one «Интервал» after its last channel, like after any
    other. A dated or daily campaign waits for its next moment, or is done.
    """
    now = now or datetime.now()
    if c.raw_state not in CampaignState.ACTIVE:
        c.next_run_at = None
        return
    if c.schedule.mode == ScheduleMode.LOOP:
        nxt = now + timedelta(seconds=human_pause(c.interval_min_sec,
                                                  c.interval_max_sec))
    else:
        nxt = next_run_after(c.schedule, now)
    if nxt is None:
        c.raw_state = CampaignState.DONE
        c.next_run_at = None
    else:
        c.raw_state = CampaignState.SCHEDULED
        c.next_run_at = nxt.strftime(STAMP)


class CampaignService:
    def __init__(self, storage, service, bus, state, delivery=None):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        self.delivery = delivery
        # When the last campaign in the start chain is due to start.
        self._chain_end: datetime | None = None

    def _changed(self, id_: str) -> None:
        self.bus.publish("entity.changed", entity="campaign", id=id_)

    # ── editing ─────────────────────────────────────────────────────────
    def _validate(self, c: Campaign, was: int = 0) -> None:
        """`was` is how many messages the campaign already had.

        Lowering the limit must not make an existing campaign unsavable, so a
        count over the limit is refused only when it also grew.
        """
        if not c.name.strip():
            raise CampaignError("err.campaign.no_name")
        if not c.account_id or self.storage.accounts.get(c.account_id) is None:
            raise CampaignError("err.campaign.no_account")
        if len(c.messages) > MESSAGE_CAP and len(c.messages) > was:
            raise CampaignError("err.campaign.too_many_messages",
                                n=len(c.messages), limit=MESSAGE_CAP)
        if c.interval_min_sec < 0 or c.interval_max_sec < 0:
            raise CampaignError("err.interval.negative")
        if c.interval_min_sec > c.interval_max_sec:
            raise CampaignError("err.interval.inverted")
        self._validate_schedule(c.schedule)
        # Every text is a text this campaign may send, so every one of them
        # has to be answerable for.
        if any(mentions_operator(m.text) for m in c.messages):
            account = self.storage.accounts.get(c.account_id)
            if self.storage.operator_for(account) is None:
                raise CampaignError("err.operator.required")

    @staticmethod
    def _validate_schedule(schedule: Schedule) -> None:
        if schedule.mode not in ScheduleMode.ALL:
            raise CampaignError("err.schedule.unknown_mode")
        if schedule.mode == ScheduleMode.ONCE and parse_iso(schedule.at) is None:
            raise CampaignError("err.schedule.no_date")
        if schedule.mode == ScheduleMode.DAILY:
            if not schedule.times:
                raise CampaignError("err.schedule.no_times")
            for raw in schedule.times:
                if not DAILY_TIME.fullmatch(str(raw).strip()):
                    raise CampaignError("err.schedule.bad_time", time=str(raw))

    def create(self, payload: dict) -> Campaign:
        c = Campaign.from_dict(payload)
        c.messages = clean_messages(c.messages)
        self._apply_defaults(c, payload)
        self._validate(c)
        c.sync_results()
        self.storage.campaigns.add(c)
        self._changed(c.id)
        return c

    def update(self, c: Campaign, payload: dict) -> Campaign:
        for field in ("name", "account_id", "target_ids", "interval_min_sec",
                      "interval_max_sec"):
            if field in payload:
                setattr(c, field, payload[field])
        if "schedule" in payload:
            before = c.schedule.to_dict()
            c.schedule = (payload["schedule"] if isinstance(payload["schedule"], Schedule)
                          else Schedule.from_dict(payload["schedule"]))
            if (c.schedule.to_dict() != before
                    and c.raw_state in CampaignState.ACTIVE):
                # a new schedule takes effect now, not after the old one fires
                self._arm(c)
        was = len(c.messages)
        if "messages" in payload:
            # Emptied means emptied. Keeping the old texts when the new list came
            # in empty is how a campaign went on sending deleted messages.
            c.messages = clean_messages(
                Campaign.from_dict({**payload, "id": c.id}).messages)
            # A message that is gone can no longer be "the previous one".
            if c.message_for(c.last_message_id) is None:
                c.last_message_id = None
        self._validate(c, was)
        c.sync_results()
        # An edit that leaves the campaign unable to send anything stops it,
        # rather than leaving it scheduled to fail at the next tick.
        self._save(c, None if c.messages and c.target_ids else
                   NO_TEXT_LEFT if not c.messages else NO_TARGETS_LEFT)
        # A picture taken off a message is a picture the user deleted.
        self.sweep_media()
        return c

    def _apply_defaults(self, c: Campaign, payload: dict) -> None:
        s = self.storage.settings
        if "interval_min_sec" not in payload:
            c.interval_min_sec = s.number("campaign.default_interval_min_sec")
        if "interval_max_sec" not in payload:
            c.interval_max_sec = s.number("campaign.default_interval_max_sec")

    def copy_name(self, name: str, word: str) -> str:
        """«Рассылка» -> «Рассылка (копия)» -> «Рассылка (копия 2)».

        `word` comes from the interface, in its language.
        """
        taken = {c.name for c in self.storage.campaigns.all()}
        candidate = f"{name.strip()} ({word})"
        n = 2
        while candidate in taken:
            candidate = f"{name.strip()} ({word} {n})"
            n += 1
        return candidate

    def duplicate(self, c: Campaign, word: str) -> Campaign:
        """A second campaign with the same settings, switched off.

        What to send is copied, what already happened is not: sending is
        something the user asks for, never something a copy inherits.
        """
        copy = Campaign(
            name=self.copy_name(c.name, word),
            account_id=c.account_id,
            target_ids=list(c.target_ids),
            # new ids: the copy rotates on its own, and sharing ids with the
            # original would tie the two rotations together
            messages=[CampaignMessage(text=m.text, file=m.file)
                      for m in c.messages],
            schedule=Schedule.from_dict(c.schedule.to_dict()),
            interval_min_sec=c.interval_min_sec,
            interval_max_sec=c.interval_max_sec,
        )
        # The same gate a new campaign goes through. The copy may keep however
        # many texts the original had, even if the limit has been lowered.
        self._validate(copy, was=len(c.messages))
        copy.sync_results()
        self.storage.campaigns.add(copy)
        self._changed(copy.id)
        LOG.info(f"campaign {tag(self.storage, c)} duplicated as {copy.name!r}", module=MOD)
        return copy

    def delete(self, campaign_id: str) -> bool:
        ok = self.storage.campaigns.delete(campaign_id)
        if ok:
            self.sweep_media()
            self._changed(campaign_id)
        return ok

    def forget_target(self, target_id: str) -> int:
        """Take a deleted channel out of every campaign that used it.

        A campaign left holding the id showed «Канал не найден» for ever, with
        no way to clear it. One that loses its last channel stops and says so;
        starting it again is the user's move.
        """
        touched = 0
        for c in self.storage.campaigns.all():
            if target_id not in c.target_ids:
                continue
            c.target_ids = [t for t in c.target_ids if t != target_id]
            c.sync_results()          # its row and its history go with it
            self._save(c, NO_TARGETS_LEFT if not c.target_ids else None)
            touched += 1
        if touched:
            LOG.info(f"channel {target_id} removed from {touched} campaign(s)",
                     module=MOD)
        return touched

    def forget_account(self, account_id: str) -> int:
        """Take a deleted account out of every campaign that sent through it.

        The campaigns stay - they can be pointed at another account - but they
        stop and let go of the id.
        """
        touched = 0
        for c in self.storage.campaigns_for_account(account_id):
            c.account_id = ""
            self._save(c, NO_ACCOUNT_LEFT)
            touched += 1
        return touched

    def _save(self, c: Campaign, stop_reason: dict | None) -> None:
        """Save a campaign the app has just edited, stopping it if it must.

        Every cascade ends here: drop the reference, then save or stop with the
        reason on the card.
        """
        if stop_reason and c.raw_state in CampaignState.ACTIVE:
            self.pause(c, stop_reason)
            return
        self.storage.campaigns.upsert(c)
        self._changed(c.id)

    def sweep_media(self) -> int:
        """Delete pictures no campaign refers to any more.

        Run when a campaign is saved or deleted, and once at start-up: the
        media folder holds the pictures of existing campaigns and nothing
        else. Files touched in the last minute are left alone - that is the
        gap between uploading one and saving the campaign that names it.
        """
        folder = config.MEDIA_DIR
        if not folder.is_dir():
            return 0
        wanted = {m.file for c in self.storage.campaigns.all()
                  for m in c.messages if m.file}
        cutoff = time.time() - MEDIA_GRACE_SEC
        removed = 0
        for path in folder.iterdir():
            if not path.is_file() or str(path) in wanted:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue          # may belong to a form still open
                path.unlink()
                removed += 1
            except OSError as exc:
                LOG.warning(f"could not delete {path.name}: {exc}", module=MOD)
        if removed:
            LOG.info(f"removed {removed} picture(s) no campaign uses any more",
                     module=MOD)
        return removed

    # ── control ─────────────────────────────────────────────────────────
    def _refuse_if_blocked(self, c: Campaign) -> None:
        """Let the user start a campaign unless something is actually wrong.

        A queued check clears by itself within seconds, so it is not a reason
        to refuse.
        """
        block = self.state.run_block(c)
        if block is not None and not block.transient:
            raise CampaignError("err.campaign.blocked", reason=block.reason)

    def start_slot(self) -> datetime:
        """The next place in the start chain.

        Each campaign started - by hand, several at once, or again after a
        restart - starts a random 0..«Задержка старта» seconds after the one
        started before it.
        """
        now = datetime.now()
        base = max(now, self._chain_end or now)
        span = self.storage.settings.number("campaign.start_delay_sec")
        self._chain_end = base + timedelta(seconds=random.uniform(0, span))
        return self._chain_end

    def _arm(self, c: Campaign) -> None:
        """Schedule the first pass: a loop through the start chain, a dated
        or daily campaign exactly at its moment (now, if the date has passed)."""
        now = datetime.now()
        if c.schedule.mode == ScheduleMode.LOOP:
            at = self.start_slot()
        else:
            at = next_run_after(c.schedule, now) or now
        c.raw_state = CampaignState.SCHEDULED
        c.next_run_at = at.strftime(STAMP)

    def start(self, c: Campaign) -> Campaign:
        if c.raw_state in CampaignState.ACTIVE:
            return c                  # already going; its place stays
        self._refuse_if_blocked(c)
        if not c.target_ids:
            raise CampaignError("err.campaign.no_targets")
        c.sync_results()
        c.last_error = None
        self._arm(c)
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        LOG.info(f"campaign {tag(self.storage, c)} scheduled for {c.next_run_at}", module=MOD)
        return c

    def pause(self, c: Campaign, reason: dict | None = None) -> Campaign:
        """Stop a campaign. The only way one ever stops.

        `reason` is what the card shows. Nothing starts a campaign again except
        the user.
        """
        c.raw_state = CampaignState.PAUSED
        c.next_run_at = None
        c.last_error = reason
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        if reason:
            LOG.warning(f"campaign {tag(self.storage, c)} stopped: {text_of(reason)}",
                        module=MOD)
        return c

    def stop_account(self, account, reason: dict) -> int:
        """Switch an account off and stop everything it was sending.

        For when the account is the problem: failing most of what it tries, its
        API profile deleted, its proxy gone. The on/off switch is the lever.
        """
        if switch_off(account, self.storage, self.service, self.bus, reason):
            LOG.warning(f"{account.handle}: switched off - {text_of(reason)}",
                        module=MOD)
        return self.stop_running(
            self.storage.campaigns_for_account(account.id), reason)

    def stop_running(self, campaigns, reason: dict) -> int:
        """Stop each of these that is still running or scheduled.

        One method for every caller that takes away something a campaign was
        built on, so all of them stop the same way.
        """
        stopped = 0
        for c in campaigns:
            if c.raw_state in CampaignState.ACTIVE:
                self.pause(c, reason)
                stopped += 1
        return stopped

    def set_target_excluded(self, c: Campaign, target_id: str,
                            excluded: bool) -> Campaign:
        """Tick a channel off, or back on, inside this campaign.

        The channel keeps its row and its history either way. Ticking it back
        on resets the refusal count: the user says they have dealt with it.
        """
        result = c.result_for(target_id)
        if result is None:
            raise CampaignError("err.campaign.not_its_channel")
        result.excluded = bool(excluded)
        if not result.excluded:
            result.excluded_reason = None
            result.refusals = 0
            result.retry_at = None
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        return c

    def reset(self, c: Campaign) -> Campaign:
        for r in c.results:
            r.status = TargetResultStatus.PENDING
            r.error = None
            r.sent_at = None
            r.attempts = 0
            r.retry_at = None
            r.refusals = 0
            r.excluded = False
            r.excluded_reason = None
        c.sent_total = 0
        c.raw_state = CampaignState.DRAFT
        c.next_run_at = None
        c.last_error = None
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        return c


class Scheduler:
    """One asyncio task that wakes every few seconds and starts what is due.

    The tick only starts campaigns, each in its own task: a run sleeps
    between targets, and awaiting it inline blocked everything else.
    """

    def __init__(self, storage, service, bus, state, campaigns: CampaignService,
                 pacer: Pacer):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        self.campaigns = campaigns
        # One send at a time per account, and the safety net's count.
        self.pacer = pacer
        self._task: asyncio.Task | None = None
        # campaign id -> its running task. Also the guard against a second
        # task for a campaign that is already sending.
        self._runs: dict[str, asyncio.Task] = {}
        self._stop = False

    @property
    def delivery(self):
        return self.campaigns.delivery

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.ensure_future(self._loop())
        LOG.info("scheduler started", module=MOD)

    async def stop(self) -> None:
        self._stop = True
        tasks = [t for t in self._runs.values() if not t.done()]
        self._runs.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        LOG.info("scheduler stopped", module=MOD)

    def resume_unfinished(self) -> None:
        """After a restart nothing is mid-flight.

        What was going gets a new place in the start chain: every loop, a
        pass cut short, and a dated or daily pass whose moment came while the
        app was closed. The rest keep their time.
        """
        now = datetime.now()
        for c in self.storage.campaigns.all():
            if c.raw_state not in CampaignState.ACTIVE:
                continue
            due = parse_iso(c.next_run_at)
            if (c.raw_state == CampaignState.RUNNING
                    or c.schedule.mode == ScheduleMode.LOOP
                    or due is None or due <= now):
                c.raw_state = CampaignState.SCHEDULED
                c.next_run_at = self.campaigns.start_slot().strftime(STAMP)
                self.storage.campaigns.upsert(c)
                LOG.info(f"campaign {tag(self.storage, c)} resumes at {c.next_run_at}",
                         module=MOD)

    # -- loop -------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stop:
            try:
                self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"scheduler tick failed: {exc}", module=MOD)
            await asyncio.sleep(TICK_SEC)

    def _tick(self) -> None:
        """Find what is due and start it. Never waits for a run."""
        now = datetime.now()
        for done_id in [i for i, t in self._runs.items() if t.done()]:
            self._runs.pop(done_id, None)
        for c in self.storage.campaigns.all():
            if self._stop:
                return
            if c.id in self._runs:
                continue                       # already sending
            if c.raw_state != CampaignState.SCHEDULED or not c.next_run_at:
                continue
            due = parse_iso(c.next_run_at)
            if due is None or due > now:
                continue
            self._runs[c.id] = asyncio.ensure_future(self._guarded(c))

    async def _guarded(self, c: Campaign) -> None:
        try:
            await self._run(c)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"campaign {tag(self.storage, c)} crashed: {exc}", module=MOD)
        finally:
            self._runs.pop(c.id, None)

    def _still_active(self, c: Campaign) -> bool:
        """Is this pass still the campaign's to carry on?

        Stop writes PAUSED; Start again, or a new schedule, writes SCHEDULED
        with a new time. Either way this run is over.
        """
        fresh = self.storage.campaigns.get(c.id)
        return fresh is not None and fresh.raw_state == CampaignState.RUNNING

    async def _wait(self, c: Campaign, seconds: float) -> bool:
        """Sleep, noticing a stop within a tick. False if stopped."""
        end = time.monotonic() + seconds
        while not self._stop and self._still_active(c):
            left = end - time.monotonic()
            if left <= 0:
                return True
            await asyncio.sleep(min(left, TICK_SEC))
        return False

    def _defer(self, c: Campaign, reason: dict) -> None:
        """Try again on one of the next ticks, keeping the campaign armed.

        For blocks that clear on their own - a check working through the
        accounts, a flood wait running out. Stopping over one of those paused
        every campaign every pass.
        """
        c.next_run_at = (datetime.now() + timedelta(seconds=TICK_SEC)) \
            .strftime(STAMP)
        c.raw_state = CampaignState.SCHEDULED
        self.storage.campaigns.upsert(c)
        LOG.debug(f"campaign {tag(self.storage, c)} waiting: {text_of(reason)}",
                  module=MOD)

    async def _run(self, c: Campaign) -> None:
        block = self.state.run_block(c)
        if block is not None:
            if block.transient:
                self._defer(c, block.reason)
            else:
                self.campaigns.pause(c, block.reason)
            return

        account = self.storage.accounts.get(c.account_id)
        c.sync_results()
        c.raw_state = CampaignState.RUNNING
        c.last_run_at = now_iso()
        self.storage.campaigns.upsert(c)
        self.bus.publish("entity.changed", entity="campaign", id=c.id)
        LOG.info(f"campaign {tag(self.storage, c)} pass started", module=MOD)

        if not c.messages:
            c.last_error = NO_TEXT_LEFT
            self._finish(c, STOPPED)
            return

        # SKIPPED comes round again: a slow mode or a mute that has run out
        # is written to on the next pass.
        pending = [r for r in c.results
                   if r.status != TargetResultStatus.SENT]
        outcome = DONE
        for index, result in enumerate(pending):
            if not self._still_active(c):
                outcome = CANCELLED
                break
            # A proxy or API profile can die mid-run; a queued check clears.
            block = self.state.run_block(c)
            if block is not None and not block.transient:
                c.last_error = block.reason
                outcome = STOPPED
                break

            outcome = await self._visit(c, account, result)
            if outcome != DONE:
                break

            # The pause follows every channel, whatever happened to it: a list
            # of channels that all refuse must not become a burst of requests.
            # After the last one a loop waits in `reschedule`; a one-pass
            # campaign simply ends.
            if index < len(pending) - 1 and not await self._wait(
                    c, human_pause(c.interval_min_sec, c.interval_max_sec)):
                outcome = CANCELLED
                break

        if self._stop:
            return                    # shutting down: resumed after the restart
        if outcome == CANCELLED:
            why = text_of(c.last_error) if c.last_error else "by request"
            LOG.info(f"campaign {tag(self.storage, c)} stopped: {why}", module=MOD)
        self._finish(c, outcome)

    async def _visit(self, c: Campaign, account, result) -> str:
        """One channel of a pass. DONE to go on, otherwise why the pass ends.

        Everything that says «not now» is looked at here, before the account's
        lock is taken: a channel switched off, a chat's slow mode or mute, the
        account's flood wait. Nothing waits while holding the lock.
        """
        if result.excluded:
            return DONE               # switched off inside this campaign
        target = self.storage.targets.get(result.target_id)
        if target is None:
            result.status = TargetResultStatus.SKIPPED
            result.error = msg("result.channel_deleted")
            return DONE
        name = target.title or target.username
        if not target.active:
            LOG.debug(f"{tag(self.storage, c)} -> {name}: switched off on the "
                      f"channels page", module=MOD)
            return DONE
        until = parse_iso(result.retry_at)
        if until is not None and until > datetime.now():
            LOG.info(f"{tag(self.storage, c)} -> {name}: skipped until "
                     f"{result.retry_at}", module=MOD)
            return DONE

        # A fresh text for every channel; `last_message_id` is saved with
        # every target, so a restart cannot repeat the last text either.
        message = c.pick_message()
        text = self._resolve_text(c, account, message)
        if text is None:
            c.last_error = NO_OPERATOR_LEFT
            return STOPPED

        while True:
            # Telegram told the whole account to wait: every campaign on it
            # waits here, and Stop still works.
            left = account.flood_left()
            if left > 0:
                LOG.info(f"{tag(self.storage, c)}: waiting until "
                         f"{account.flood_until} as Telegram asked", module=MOD)
                if not await self._wait(c, left):
                    return CANCELLED
            sent = await self._send_one(c, account, target, result, text, message)
            if sent != FLOODED:
                break
        if not self._still_active(c):
            # stopped meanwhile - by the user, or with the whole account, in
            # which case the reason is already written on the campaign
            return CANCELLED
        # The account may be the problem now - a dead key, a freeze.
        block = self.state.run_block(c)
        if block is not None and not block.transient:
            c.last_error = block.reason
            return STOPPED
        if not self._save_progress(c):
            return CANCELLED          # deleted while it was sending
        too_many = self.pacer.error_rate_exceeded(account.id)
        if too_many:
            self.campaigns.stop_account(account, too_many)
            c.last_error = too_many
            return STOPPED
        return DONE

    def _may_send(self, c: Campaign) -> bool:
        """Still this pass's turn, and nothing about the account stops it -
        asked again once the lock is ours, since another campaign on the
        account may have switched it off while this one waited."""
        if not self._still_active(c):
            return False
        block = self.state.run_block(c)
        return block is None or block.transient

    async def _send_one(self, c: Campaign, account, target, result,
                        text: str, message: CampaignMessage) -> str:
        """One message to one chat: SENT, REFUSED, FLOODED or CANCELLED.

        The account's lock is held for the requests only. The pause after
        joining a chat happens between two holds, where Stop can reach it.
        """
        name = target.title or target.username
        destination = None
        async with self.pacer.lock(account.id):
            if not self._may_send(c):
                return CANCELLED
            result.attempts += 1
            try:
                destination = await self.delivery.resolve(account.key, target)
                await self.delivery.enter(account.key, destination, target)
            except DeliveryError as exc:
                return await self._refused(c, account, target, result, exc,
                                           destination)
        if destination.joined and self.delivery.join_delay:
            # Joining and posting in the same second is what a person never
            # does, and the first thing an anti-spam system looks for.
            if not await self._wait(c, self.delivery.join_delay):
                return CANCELLED
        async with self.pacer.lock(account.id):
            if not self._may_send(c):
                return CANCELLED
            try:
                await self.delivery.send(account.key, destination, text,
                                         file=message.file or None,
                                         react=self._wants_reaction())
            except DeliveryError as exc:
                return await self._refused(c, account, target, result, exc,
                                           destination)
        result.status = TargetResultStatus.SENT
        result.error = None
        result.retry_at = None
        result.refusals = 0
        result.sent_at = now_iso()
        c.sent_total += 1
        self.pacer.note_outcome(account.id, target.id, True)
        LOG.info(f"{tag(self.storage, c)} -> {name}: sent ({message.id})",
                 module=MOD)
        return SENT

    def _wants_reaction(self) -> bool:
        """Leave a reaction this time? Now and then, never always.

        An account that only ever posts and never touches anything is a shape
        worth not having. It goes on the newest message before ours exists,
        so never on our own; and never on an operator's behalf - campaigns are
        the only caller.
        """
        settings = self.storage.settings
        if not settings.get("campaign.reactions", False):
            return False
        percent = settings.number("campaign.reaction_percent", high=100)
        return percent > 0 and random.randint(1, 100) <= percent

    async def _refused(self, c: Campaign, account, target, result,
                       exc: DeliveryError, destination) -> str:
        """What one refusal means, by its fault - the one place that decides.

        FLOODED sends the caller back to wait and try again; everything else
        is REFUSED, with its consequences written down here.
        """
        name = target.title or target.username or target.id
        label = tag(self.storage, c)
        fault = exc.fault

        if fault == Fault.FLOOD:
            until = hold_for_flood(account, self.storage, self.bus,
                                   wait_seconds(exc.cause))
            LOG.warning(f"{label} -> {name}: Telegram asked the account to wait "
                        f"until {until}", module=MOD)
            return FLOODED

        if fault == Fault.SLOW_MODE:
            until = datetime.now() + timedelta(seconds=wait_seconds(exc.cause))
            self._skip_chat(account, target, until,
                            msg("result.slow_mode", time=until_text(until)))
            LOG.info(f"{label} -> {name}: slow mode, skipped until "
                     f"{until.strftime(STAMP)}", module=MOD)
            return REFUSED

        if fault == Fault.BANNED:
            fault = await self._banned_where(c, account, target, result,
                                             destination)
            if fault is None:
                return REFUSED        # decided and recorded there

        if fault == Fault.NOT_MEMBER:
            result.status = TargetResultStatus.SKIPPED
            result.error = exc.message
            self._exclude([(c, result)], exc.message)
            LOG.warning(f"{label} -> {name}: not a member and joining is off - "
                        f"switched off", module=MOD)
            return REFUSED

        result.status = TargetResultStatus.FAILED
        result.error = exc.message

        if fault == Fault.DEAD:
            mark_dead(account, self.storage, self.bus, exc.message)
            c.last_error = msg("issue.campaign.account_auth_dead",
                               account=account.handle)
            LOG.error(f"{label}: the session is revoked - the campaign stops",
                      module=MOD)
        elif fault == Fault.FROZEN:
            mark_frozen(account, self.storage, self.bus, exc.message)
            c.last_error = msg("issue.campaign.account_frozen",
                               account=account.handle)
            LOG.error(f"{label}: Telegram has frozen the account - the campaign "
                      f"stops", module=MOD)
        elif fault == Fault.NOT_FOUND:
            reason = msg("delivery.not_found")
            self._exclude(self._results_for(target.id), reason)
            self._mark_target(target, reason)
            LOG.warning(f"{label} -> {name}: no such chat - switched off in every "
                        f"campaign", module=MOD)
        elif fault == Fault.GUEST and destination is not None \
                and destination.comment and not self.delivery.auto_join:
            reason = msg("delivery.join_to_comment")
            self._exclude(self._results_for(target.id), reason)
            self._mark_target(target, reason)
            LOG.warning(f"{label} -> {name}: commenting needs the discussion "
                        f"joined - switched off in every campaign", module=MOD)
        elif fault in (Fault.CHAT, Fault.GUEST):
            # A chat that says "you may not write here" says it again every
            # cycle, for ever. Counted, and let go after enough of them.
            result.refusals += 1
            limit = self.storage.settings.number("campaign.refusals_before_off")
            LOG.warning(f"{label} -> {name}: {text_of(exc.message)}", module=MOD)
            if limit and result.refusals >= limit:
                self._exclude([(c, result)],
                              msg("excluded.refused", n=result.refusals))
        else:
            # The account or the network: the only failures the guard counts.
            self.pacer.note_outcome(account.id, target.id, False)
            LOG.warning(f"{label} -> {name}: {text_of(exc.message)}", module=MOD)
        return REFUSED

    async def _banned_where(self, c: Campaign, account, target, result,
                            destination) -> str | None:
        """«Banned»: in this chat, or everywhere? One more request says.

        Returns None when that is decided and recorded here, or the fault to
        treat it as when the chat could not be asked.
        """
        label = tag(self.storage, c)
        name = target.title or target.username or target.id
        if destination is None:
            return Fault.CHAT
        try:
            found = await self.delivery.restriction(account.key, destination)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"{label} -> {name}: could not ask the chat what it "
                        f"forbids: {type(exc).__name__}: {exc}", module=MOD)
            return Fault.CHAT

        if found is None:
            # The chat forbids nothing: Telegram barred the account from public
            # groups. Every campaign on it stops; no channel is to blame.
            result.status = TargetResultStatus.FAILED
            result.error = PUBLIC_GROUPS_BANNED
            self.campaigns.stop_account(account, PUBLIC_GROUPS_BANNED)
            return None
        if not found.banned:
            # Muted or thrown out for a week at most: waited out, like a slow
            # mode. Longer is a ban (delivery.LONGEST_WAIT).
            self._skip_chat(account, target, found.until,
                            msg("result.muted", time=until_text(found.until)))
            LOG.info(f"{label} -> {name}: restricted there for a while, skipped until "
                     f"{found.until.strftime(STAMP)}", module=MOD)
            return None
        # Banned in this chat: no campaign of this account writes there again,
        # and the account does not sit in a chat that threw it out.
        result.status = TargetResultStatus.FAILED
        result.error = msg("excluded.banned")
        self._exclude(self._results_for(target.id, account.id),
                      msg("excluded.banned"))
        LOG.warning(f"{label} -> {name}: banned in this chat - switched off in "
                    f"every campaign of {account.handle}", module=MOD)
        await self.delivery.leave(account.key, destination, target)
        return None

    # -- one chat, several campaigns --------------------------------------
    def _results_for(self, target_id: str, account_id: str | None = None):
        """Every campaign's row for this chat - of one account, or of all."""
        return [(c, r) for c in self.storage.campaigns.all()
                if account_id is None or c.account_id == account_id
                for r in c.results if r.target_id == target_id]

    def _exclude(self, rows, reason: dict) -> None:
        """Untick a chat inside these campaigns. Not a deletion: the channel
        keeps its place and history, and one tick brings it back."""
        touched = {}
        for c, r in rows:
            if r.excluded:
                continue
            r.excluded = True
            r.excluded_reason = reason
            touched[c.id] = c
        for c in touched.values():
            self._save_progress(c)

    def _skip_chat(self, account, target, until: datetime, reason: dict) -> None:
        """Not before this moment, for every campaign of this account writing
        here: they skip the chat without waiting and come back on a pass
        after it."""
        stamp = until.strftime(STAMP)
        touched = {}
        for c, r in self._results_for(target.id, account.id):
            r.retry_at = stamp
            if r.status != TargetResultStatus.SENT:
                r.status = TargetResultStatus.SKIPPED
                r.error = reason
            touched[c.id] = c
        for c in touched.values():
            self._save_progress(c)

    def _mark_target(self, target, reason: dict) -> None:
        """Say on the channel itself why nothing can be sent there."""
        target.last_check_error = reason
        target.last_check = now_iso()
        self.storage.targets.upsert(target)
        self.bus.publish("entity.changed", entity="target", id=target.id)

    def _save_progress(self, c: Campaign) -> bool:
        """Write the per-target result without clobbering a stop.

        The record in storage may already say PAUSED because the user pressed
        Остановить a moment ago; the object in hand still says RUNNING. Saving
        it whole would put RUNNING back and the stop would be lost. A campaign
        deleted while it was sending is not written back at all.
        """
        fresh = self.storage.campaigns.get(c.id)
        if fresh is None:
            return False
        if fresh.raw_state not in CampaignState.ACTIVE:
            c.raw_state = fresh.raw_state
            c.next_run_at = fresh.next_run_at
        self.storage.campaigns.upsert(c)
        self.bus.publish("campaign.progress", id=c.id,
                         sent=c.sent_count, sent_total=c.sent_total,
                         failed=c.failed_count, total=len(c.target_ids))
        return True

    def _resolve_text(self, c: Campaign, account,
                      message: CampaignMessage) -> str | None:
        """Final @operator check before a campaign message goes out."""
        text = message.text
        if not mentions_operator(text):
            return text
        operator = self.storage.operator_for(account)
        uname = self.storage.operator_uname(operator) if operator else ""
        if not uname:
            LOG.error(f"campaign {tag(self.storage, c)} BLOCKED - {OPERATOR_TOKEN} with no "
                      f"operator bound; nothing sent", module=MOD)
            return None
        return re.sub(r"@operator\b", f"@{uname}", text, flags=re.IGNORECASE)

    def _finish(self, c: Campaign, outcome: str) -> None:
        if self.storage.campaigns.get(c.id) is None:
            # deleted while it was sending: writing it back would restore it
            LOG.info(f"campaign {tag(self.storage, c)} was deleted mid-run", module=MOD)
            return
        if outcome == STOPPED:
            c.raw_state = CampaignState.PAUSED
            c.next_run_at = None
        elif outcome == DONE:
            now = datetime.now()
            for slot in skipped_slots(c.schedule, parse_iso(c.last_run_at), now):
                LOG.info(f"campaign {tag(self.storage, c)}: {slot} skipped, the previous "
                         f"pass was still going", module=MOD)
            reschedule(c, now)
            if c.raw_state == CampaignState.SCHEDULED:
                # the next pass starts from scratch
                for r in c.results:
                    if r.status == TargetResultStatus.SENT:
                        r.status = TargetResultStatus.PENDING
        # CANCELLED: the user already set the state; only the results are saved
        self.storage.campaigns.upsert(c)
        self.bus.publish("entity.changed", entity="campaign", id=c.id)
        LOG.info(f"campaign {tag(self.storage, c)} pass ended: state={c.raw_state} "
                 f"sent={c.sent_count} total={c.sent_total} "
                 f"failed={c.failed_count}", module=MOD)
