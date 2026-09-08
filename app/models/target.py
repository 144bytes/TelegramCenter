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
    # The hash from a t.me/+… or /joinchat/… link, without the plus. A private
    # chat reached by invitation has no username and no id anyone could look
    # up, so it is the only way to address one. It used to be stripped down to
    # its bare text and stored as `username`, where it resolved to nothing and
    # every send failed with "no user has that username".
    invite: str = ""
    telegram_id: int | None = None
    type: str = TargetType.CHANNEL
    active: bool = True
    notes: str = ""
    last_check: str | None = None
    # Why the last check failed, as a message; None when it passed.
    last_check_error: dict | None = None

    @property
    def link(self) -> str:
        if self.username:
            return f"https://t.me/{self.username.lstrip('@')}"
        if self.invite:
            return f"https://t.me/+{self.invite}"
        return str(self.telegram_id or "")
