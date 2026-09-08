from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id
from .base import Model
from .enums import AutoReplyKind


@dataclass
class AutoReplyRule(Model):
    id: str = field(default_factory=lambda: gen_id("rule"))
    kind: str = AutoReplyKind.FAQ
    enabled: bool = True
    match: str = ""                          # FAQ only: keyword / substring
    response: str = ""
    delay_min_sec: int | None = None         # None -> inherit the account range
    delay_max_sec: int | None = None

    @property
    def is_blank(self) -> bool:
        return not self.response.strip() and not self.match.strip()


@dataclass
class AutoReplyConfig(Model):
    """One set of rules per account. owner_id is an account id, or "global"
    for the app-wide default that accounts without their own inherit live."""
    owner_id: str = ""
    enabled: bool = False
    delay_min_sec: int = 300
    delay_max_sec: int = 600
    rules: list[AutoReplyRule] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[AutoReplyRule]:
        return [r for r in self.rules if r.kind == kind]

    def singleton(self, kind: str) -> AutoReplyRule | None:
        return next((r for r in self.rules if r.kind == kind), None)
