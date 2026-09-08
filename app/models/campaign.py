from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id, now_iso
from .base import Model
from .enums import CampaignState, ScheduleMode, TargetResultStatus


@dataclass
class Schedule(Model):
    mode: str = ScheduleMode.ONCE
    at: str | None = None            # ONCE: "2026-09-10T14:30:00"
    times: list[str] = field(default_factory=list)   # DAILY: ["09:00", "18:30"]
    every_sec: int | None = None     # INTERVAL


@dataclass
class CampaignTargetResult(Model):
    target_id: str = ""
    status: str = TargetResultStatus.PENDING
    error: str | None = None
    sent_at: str | None = None
    attempts: int = 0


@dataclass
class Campaign(Model):
    """One campaign -> many targets. The message text is a snapshot taken when
    the campaign is saved: editing the source template later must never change
    an existing campaign."""
    id: str = field(default_factory=lambda: gen_id("camp"))
    name: str = ""
    account_id: str = ""
    target_ids: list[str] = field(default_factory=list)
    message_text: str = ""
    source_template_id: str | None = None   # provenance only, not a live link
    schedule: Schedule = field(default_factory=Schedule)
    gap_min_sec: int = 15                   # random pause between targets
    gap_max_sec: int = 40
    raw_state: str = CampaignState.DRAFT
    results: list[CampaignTargetResult] = field(default_factory=list)
    # Everything ever sent by this campaign. `results` is per-cycle and gets
    # cleared when a recurring campaign starts its next pass, so it cannot
    # answer "how much has this campaign actually sent".
    sent_total: int = 0
    created_at: str = field(default_factory=now_iso)
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_error: str | None = None

    def result_for(self, target_id: str) -> CampaignTargetResult | None:
        return next((r for r in self.results if r.target_id == target_id), None)

    def sync_results(self) -> None:
        """Keep results aligned with target_ids: add missing, drop removed."""
        known = {r.target_id: r for r in self.results}
        self.results = [
            known.get(tid) or CampaignTargetResult(target_id=tid)
            for tid in self.target_ids
        ]

    @property
    def is_recurring(self) -> bool:
        return self.schedule.mode in (ScheduleMode.DAILY, ScheduleMode.INTERVAL)

    @property
    def sent_count(self) -> int:
        """Sent in the current pass. For a one-shot campaign this is the total."""
        return sum(r.status == TargetResultStatus.SENT for r in self.results)

    @property
    def failed_count(self) -> int:
        return sum(r.status == TargetResultStatus.FAILED for r in self.results)
