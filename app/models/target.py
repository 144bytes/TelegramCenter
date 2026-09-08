from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id
from .base import Model
from .enums import TargetType


@dataclass
class Target(Model):
    id: str = field(default_factory=lambda: gen_id("target"))
    title: str = ""
    username: str = ""
    telegram_id: int | None = None
    type: str = TargetType.CHANNEL
    active: bool = True
    notes: str = ""
    last_check: str | None = None
    last_check_status: str | None = None   # "AVAILABLE" | "ERROR: ..."

    @property
    def link(self) -> str:
        if self.username:
            return f"https://t.me/{self.username.lstrip('@')}"
        return str(self.telegram_id or "")
