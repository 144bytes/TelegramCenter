"""Campaigns: editing, scheduling, and the send loop.

Behaviour agreed with the user:
  * one campaign -> many targets, each with its own result;
  * a target that fails is marked FAILED and retried on the next run;
  * if a dependency breaks mid-run the campaign stops immediately and the
    targets it never reached stay PENDING — they are not marked failed,
    because nothing was attempted;
  * the pause between targets is random inside [gap_min_sec, gap_max_sec].
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta

from ..logging import LOG
from ..models import Campaign, Schedule
from ..models.enums import CampaignState, ScheduleMode, TargetResultStatus
from ..state.manager import OPERATOR_TOKEN, mentions_operator
from ..util import now_iso, parse_iso

MOD = "campaigns"
TICK_SEC = 5


class CampaignError(ValueError):
    """Message meant to be shown to the user as-is."""


# ── schedule maths ──────────────────────────────────────────────────────
def next_run_after(schedule: Schedule, after: datetime) -> datetime | None:
    if schedule.mode == ScheduleMode.ONCE:
        at = parse_iso(schedule.at)
        return at if at and at > after else None

    if schedule.mode == ScheduleMode.DAILY:
        candidates: list[datetime] = []
        for raw in schedule.times or []:
            try:
                hh, mm = (int(x) for x in str(raw).split(":")[:2])
            except (ValueError, TypeError):
                continue
            today = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
            candidates.append(today if today > after else today + timedelta(days=1))
        return min(candidates) if candidates else None

    if schedule.mode == ScheduleMode.INTERVAL:
        every = int(schedule.every_sec or 0)
        return after + timedelta(seconds=every) if every > 0 else None
    return None


class CampaignService:
    def __init__(self, storage, service, bus, state):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state

    def _changed(self, id_: str) -> None:
        self.bus.publish("entity.changed", entity="campaign", id=id_)

    # ── editing ─────────────────────────────────────────────────────────
    def _validate(self, c: Campaign) -> None:
        if not c.name.strip():
            raise CampaignError("Не задано название кампании")
        if not c.account_id or self.storage.accounts.get(c.account_id) is None:
            raise CampaignError("Не выбран аккаунт")
        if not c.message_text.strip():
            raise CampaignError("Не задан текст сообщения")
        if c.gap_min_sec < 0 or c.gap_max_sec < 0:
            raise CampaignError("Пауза не может быть отрицательной")
        if c.gap_min_sec > c.gap_max_sec:
            raise CampaignError("Минимальная пауза больше максимальной")
        if mentions_operator(c.message_text):
            account = self.storage.accounts.get(c.account_id)
            if self.storage.operator_for(account) is None:
                raise CampaignError(
                    f"В тексте есть {OPERATOR_TOKEN}, но у аккаунта не привязан "
                    f"оператор. Привяжите оператора или уберите {OPERATOR_TOKEN}.")

    def create(self, payload: dict) -> Campaign:
        c = Campaign.from_dict(payload)
        c.message_text = self._snapshot_text(payload)
        self._apply_defaults(c, payload)
        self._validate(c)
        c.sync_results()
        self.storage.campaigns.add(c)
        self._changed(c.id)
        return c

    def update(self, c: Campaign, payload: dict) -> Campaign:
        for field in ("name", "account_id", "target_ids", "gap_min_sec",
                      "gap_max_sec"):
            if field in payload:
                setattr(c, field, payload[field])
        if "schedule" in payload:
            c.schedule = (payload["schedule"] if isinstance(payload["schedule"], Schedule)
                          else Schedule.from_dict(payload["schedule"]))
        if "message_text" in payload or "source_template_id" in payload:
            c.message_text = self._snapshot_text(payload, fallback=c.message_text)
            c.source_template_id = payload.get("source_template_id",
                                               c.source_template_id)
        self._validate(c)
        c.sync_results()
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        return c

    def _apply_defaults(self, c: Campaign, payload: dict) -> None:
        s = self.storage.settings
        if "gap_min_sec" not in payload:
            c.gap_min_sec = int(s.get("campaign.default_gap_min_sec", 15))
        if "gap_max_sec" not in payload:
            c.gap_max_sec = int(s.get("campaign.default_gap_max_sec", 40))

    def _snapshot_text(self, payload: dict, fallback: str = "") -> str:
        """Copy the template's text into the campaign once.

        From here on the campaign owns its text: editing the template later
        must not change campaigns that were already created from it.
        """
        text = payload.get("message_text")
        if text:
            return text
        tpl_id = payload.get("source_template_id") or payload.get("template_id")
        if tpl_id:
            tpl = self.storage.templates.get(tpl_id)
            if tpl is not None:
                return tpl.text
        return fallback

    def delete(self, campaign_id: str) -> bool:
        ok = self.storage.campaigns.delete(campaign_id)
        if ok:
            self._changed(campaign_id)
        return ok

    # ── control ─────────────────────────────────────────────────────────
    def start(self, c: Campaign) -> Campaign:
        ok, reason = self.state.can_run(c)
        if not ok:
            raise CampaignError(reason or "Кампанию нельзя запустить")
        if not c.target_ids:
            raise CampaignError("Не выбрано ни одного канала")
        c.sync_results()
        c.raw_state = CampaignState.SCHEDULED
        c.last_error = None
        nxt = next_run_after(c.schedule, datetime.now())
        # a one-shot with no date, or "start now", runs on the next tick
        c.next_run_at = (nxt or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        LOG.info(f"campaign {c.name!r} scheduled for {c.next_run_at}", module=MOD)
        return c

    def run_now(self, c: Campaign) -> Campaign:
        ok, reason = self.state.can_run(c)
        if not ok:
            raise CampaignError(reason or "Кампанию нельзя запустить")
        c.raw_state = CampaignState.SCHEDULED
        c.next_run_at = now_iso()
        c.last_error = None
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        return c

    def pause(self, c: Campaign) -> Campaign:
        c.raw_state = CampaignState.PAUSED
        c.next_run_at = None
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        return c

    def reset(self, c: Campaign) -> Campaign:
        for r in c.results:
            r.status = TargetResultStatus.PENDING
            r.error = None
            r.sent_at = None
            r.attempts = 0
        c.sent_total = 0
        c.raw_state = CampaignState.DRAFT
        c.next_run_at = None
        c.last_error = None
        self.storage.campaigns.upsert(c)
        self._changed(c.id)
        return c


class Scheduler:
    """One asyncio task that wakes every few seconds and runs what is due."""

    def __init__(self, storage, service, bus, state, campaigns: CampaignService):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        self.campaigns = campaigns
        self._task: asyncio.Task | None = None
        self._stop = False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.ensure_future(self._loop())
        LOG.info("scheduler started", module=MOD)

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        LOG.info("scheduler stopped", module=MOD)

    def resume_unfinished(self) -> None:
        """After a restart nothing is mid-flight. Campaigns that were RUNNING
        go back to SCHEDULED so the loop picks them up cleanly."""
        for c in self.storage.campaigns.all():
            if c.raw_state == CampaignState.RUNNING:
                c.raw_state = CampaignState.SCHEDULED
                if not c.next_run_at:
                    c.next_run_at = now_iso()
                self.storage.campaigns.upsert(c)

    # -- loop -------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stop:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"scheduler tick failed: {exc}", module=MOD)
            await asyncio.sleep(TICK_SEC)

    async def _tick(self) -> None:
        now = datetime.now()
        for c in self.storage.campaigns.all():
            if self._stop:
                return
            if c.raw_state != CampaignState.SCHEDULED or not c.next_run_at:
                continue
            due = parse_iso(c.next_run_at)
            if due is None or due > now:
                continue
            await self._run(c)

    async def _run(self, c: Campaign) -> None:
        ok, reason = self.state.can_run(c)
        if not ok:
            c.raw_state = CampaignState.PAUSED
            c.last_error = reason
            c.next_run_at = None
            self.storage.campaigns.upsert(c)
            self.bus.publish("entity.changed", entity="campaign", id=c.id)
            LOG.warning(f"campaign {c.name!r} blocked: {reason}", module=MOD)
            return

        account = self.storage.accounts.get(c.account_id)
        c.sync_results()
        c.raw_state = CampaignState.RUNNING
        c.last_run_at = now_iso()
        self.storage.campaigns.upsert(c)
        self.bus.publish("entity.changed", entity="campaign", id=c.id)
        LOG.info(f"campaign {c.name!r} started", module=MOD)

        pending = [r for r in c.results
                   if r.status in (TargetResultStatus.PENDING, TargetResultStatus.FAILED)]
        stopped_early = False

        for index, result in enumerate(pending):
            if self._stop:
                stopped_early = True
                break

            # Re-check every time: a proxy or API profile can die mid-run, and
            # the agreed behaviour is to stop at once rather than keep sending.
            ok, reason = self.state.can_run(c)
            if not ok:
                c.last_error = reason
                stopped_early = True
                LOG.warning(f"campaign {c.name!r} stopped mid-run: {reason}", module=MOD)
                break

            target = self.storage.targets.get(result.target_id)
            if target is None:
                result.status = TargetResultStatus.SKIPPED
                result.error = "Канал не найден"
                continue

            text = self._resolve_text(c, account)
            if text is None:
                c.last_error = f"{OPERATOR_TOKEN} без привязанного оператора"
                stopped_early = True
                break

            result.attempts += 1
            try:
                ref = target.username or target.telegram_id
                await self.service.send_message(account.key, ref, text)
                result.status = TargetResultStatus.SENT
                result.error = None
                result.sent_at = now_iso()
                c.sent_total += 1
                LOG.info(f"{c.name!r} -> {target.title or target.username}: sent",
                         module=MOD)
            except Exception as exc:  # noqa: BLE001
                result.status = TargetResultStatus.FAILED
                result.error = f"{type(exc).__name__}: {exc}"
                LOG.warning(f"{c.name!r} -> {target.title or target.username}: "
                            f"{result.error}", module=MOD)

            self.storage.campaigns.upsert(c)
            self.bus.publish("campaign.progress", id=c.id,
                             sent=c.sent_count, sent_total=c.sent_total,
                             failed=c.failed_count, total=len(c.target_ids))

            if index < len(pending) - 1:
                await asyncio.sleep(random.randint(c.gap_min_sec, c.gap_max_sec))

        self._finish(c, stopped_early)

    def _resolve_text(self, c: Campaign, account) -> str | None:
        """Final @operator check before a campaign message goes out."""
        text = c.message_text
        if not mentions_operator(text):
            return text
        operator = self.storage.operator_for(account)
        if operator is None or not operator.uname:
            LOG.error(f"campaign {c.name!r} BLOCKED - {OPERATOR_TOKEN} with no "
                      f"operator bound; nothing sent", module=MOD)
            return None
        import re
        return re.sub(r"@operator\b", f"@{operator.uname}", text, flags=re.IGNORECASE)

    def _finish(self, c: Campaign, stopped_early: bool) -> None:
        if stopped_early:
            c.raw_state = CampaignState.PAUSED
            c.next_run_at = None
        else:
            nxt = next_run_after(c.schedule, datetime.now())
            if nxt is None:
                c.raw_state = CampaignState.DONE
                c.next_run_at = None
            else:
                # a recurring campaign starts the next cycle from scratch
                for r in c.results:
                    if r.status == TargetResultStatus.SENT:
                        r.status = TargetResultStatus.PENDING
                c.raw_state = CampaignState.SCHEDULED
                c.next_run_at = nxt.strftime("%Y-%m-%dT%H:%M:%S")
        self.storage.campaigns.upsert(c)
        self.bus.publish("entity.changed", entity="campaign", id=c.id)
        LOG.info(f"campaign {c.name!r} finished: state={c.raw_state} "
                 f"sent={c.sent_count} total={c.sent_total} "
                 f"failed={c.failed_count}", module=MOD)
