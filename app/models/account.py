from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id, now_iso
from .base import Model
from .enums import AccountState, SpamState


@dataclass
class Account(Model):
    """A broadcast account. Operators are a separate entity (see Operator);
    there is no role system — only these two kinds."""
    id: str = field(default_factory=lambda: gen_id("acc"))
    key: str = ""                       # session file stem, e.g. "session_123456789"
    session_file: str = ""
    telegram_id: int | None = None
    username: str | None = None
    phone: str = ""
    first_name: str = ""
    last_name: str = ""
    api_profile_id: str | None = None
    network_profile_id: str | None = None
    operator_id: str | None = None      # the operator @operator resolves to
    raw_state: str = AccountState.OFFLINE
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)
    last_check_at: str | None = None
    last_error: str | None = None
    # result of the optional antispam check (see services/spamcheck.py)
    spam_state: str = SpamState.UNKNOWN
    spam_detail: str = ""
    spam_checked_at: str | None = None

    @property
    def display_name(self) -> str:
        name = " ".join(x for x in (self.first_name, self.last_name) if x).strip()
        if name:
            return name
        if self.username:
            return f"@{self.username}"
        return self.phone or f"ID={self.telegram_id or '?'}"

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else self.display_name
