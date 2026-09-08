from __future__ import annotations

import random
from dataclasses import dataclass, field
from ..util import gen_id, now_iso
from .base import Model
from .enums import CampaignState, ScheduleMode, TargetResultStatus


@dataclass
class Schedule(Model):
    mode: str = ScheduleMode.LOOP
    at: str | None = None            # ONCE: "2026-09-10T14:30:00"
    times: list[str] = field(default_factory=list)   # DAILY: ["09:00", "18:30"]


@dataclass
class CampaignMessage(Model):
    """One of the texts a campaign rotates through.

    The id is what makes «not the same text as last cycle» survive editing:
    the rotation remembers an id, so minting new ones on every save would
    quietly reset it.
    """
    id: str = field(default_factory=lambda: gen_id("msg"))
    text: str = ""
    # A picture sent with this text, in the app's own media folder: an
    # advert that is always a bare block of text is a shape in itself,
    # and a copy cannot break when the original leaves Downloads.
    file: str = ""


@dataclass
class CampaignTargetResult(Model):
    target_id: str = ""
    status: str = TargetResultStatus.PENDING
    error: dict | None = None            # a message, see app/messages.py
    sent_at: str | None = None
    attempts: int = 0
    # Not before this moment. Written when Telegram answers with a wait -
    # retrying inside it earns the next one. Stored, not in memory: one
    # chat asked for 55 minutes and a restart must not forget.
    retry_at: str | None = None
    # How many times in a row this chat has refused the account while it
    # was a member. One success resets it.
    refusals: int = 0
    # Switched off inside this campaign: the channel keeps its place and
    # history. Written when the account is banned there, or when the chat
    # has refused often enough.
    excluded: bool = False
    # Why, as a message.
    excluded_reason: dict | None = None


@dataclass
class Campaign(Model):
    """One campaign -> one account -> many targets, and one or more texts.

    The texts are the campaign's own copy. A message is picked per channel,
    not per pass: one text in twenty chats within minutes is the shape
    being punished - each chat sees one message, anything looking across
    them sees one text copied twenty times.
    """
    id: str = field(default_factory=lambda: gen_id("camp"))
    name: str = ""
    account_id: str = ""
    target_ids: list[str] = field(default_factory=list)
    messages: list[CampaignMessage] = field(default_factory=list)
    # Which message went out last, so the next channel can avoid it.
    # Saved with every target: stopping between two channels must not let
    # the same text go out twice.
    last_message_id: str | None = None
    schedule: Schedule = field(default_factory=Schedule)
    # The pause after every channel of a pass, drawn inside this range.
    interval_min_sec: int = 15
    interval_max_sec: int = 40
    raw_state: str = CampaignState.DRAFT
    results: list[CampaignTargetResult] = field(default_factory=list)
    # Everything ever sent. `results` is per cycle and gets cleared, so
    # it cannot answer «how much has this campaign sent».
    sent_total: int = 0
    created_at: str = field(default_factory=now_iso)
    next_run_at: str | None = None
    last_run_at: str | None = None
    # Why it stopped, as a message. A campaign only ever stops one way -
    # `CampaignService.pause` - and only ever starts one way, which is the
    # user pressing Start.
    last_error: dict | None = None

    def result_for(self, target_id: str) -> CampaignTargetResult | None:
        return next((r for r in self.results if r.target_id == target_id), None)

    def message_for(self, message_id: str | None) -> CampaignMessage | None:
        return next((m for m in self.messages if m.id == message_id), None)

    def pick_message(self) -> CampaignMessage | None:
        """The text for the next channel, and never the one just used.

        Called once per channel. One message: always it. Two: strict
        alternation, starting with the first. Three or more: at random minus
        the last one sent.

        `last_message_id` is saved with every target, so the rule holds across
        channels, cycles and restarts. If it points at a deleted message it
        simply excludes nothing.
        """
        if not self.messages:
            return None
        if self.last_message_id is None and len(self.messages) <= 2:
            chosen = self.messages[0]
        else:
            pool = [m for m in self.messages if m.id != self.last_message_id]
            chosen = random.choice(pool or self.messages)
        self.last_message_id = chosen.id
        return chosen

    def sync_results(self) -> None:
        """Keep results aligned with target_ids: add missing, drop removed."""
        known = {r.target_id: r for r in self.results}
        self.results = [
            known.get(tid) or CampaignTargetResult(target_id=tid)
            for tid in self.target_ids
        ]

    @property
    def is_recurring(self) -> bool:
        return self.schedule.mode in (ScheduleMode.DAILY, ScheduleMode.LOOP)

    @property
    def has_text(self) -> bool:
        return any(m.text.strip() for m in self.messages)

    @property
    def needs_operator(self) -> bool:
        """Does any of the texts name @operator?

        Any of them may be the one the next channel gets.
        """
        from ..state.manager import mentions_operator
        return any(mentions_operator(m.text) for m in self.messages)

    @property
    def sent_count(self) -> int:
        """Sent in the current pass. For a one-shot campaign this is the total."""
        return sum(r.status == TargetResultStatus.SENT for r in self.results)

    @property
    def failed_count(self) -> int:
        return sum(r.status == TargetResultStatus.FAILED for r in self.results)
