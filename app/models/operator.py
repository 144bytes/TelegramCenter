from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id
from .base import Model
from .enums import AccountState, SpamState


@dataclass
class Operator(Model):
    """Second-line account. A @username alone is enough for auto-replies;
    logging in additionally unlocks chat viewing and replying."""
    id: str = field(default_factory=lambda: gen_id("op"))
    username: str = ""                 # with or without a leading @
    display_name: str = ""
    key: str = ""                      # session stem if logged in, else ""
    session_file: str = ""
    telegram_id: int | None = None
    notes: str = ""
    # An operator connects to Telegram exactly like a broadcast account does,
    # so it carries the same dependencies and the same probe result.
    api_profile_id: str | None = None
    network_profile_id: str | None = None
    raw_state: str = AccountState.OFFLINE
    last_check_at: str | None = None
    last_error: str | None = None
    # Same antispam fields as an account, so one check writes one shape of
    # result whichever kind it ran on. Only filled when the operator antispam
    # option is switched on; off by default.
    spam_state: str = SpamState.UNKNOWN
    spam_detail: str = ""
    spam_checked_at: str | None = None

    @property
    def logged_in(self) -> bool:
        return bool(self.key)

    @property
    def uname(self) -> str:
        return self.username.lstrip("@")

    @property
    def handle(self) -> str:
        if self.username:
            return f"@{self.uname}"
        return self.display_name or f"op {self.id[:6]}"
