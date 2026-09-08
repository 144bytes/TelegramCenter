from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..util import gen_id, now_iso, parse_iso
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
    # Messages (see app/messages.py), not text: the interface words them.
    last_error: dict | None = None
    # result of the optional antispam check (see services/spamcheck.py)
    spam_state: str = SpamState.UNKNOWN
    spam_detail: dict | None = None
    spam_checked_at: str | None = None
    # Why the app switched this account off. The switch alone cannot say it:
    # "выключен" reads as something the user did. Cleared the moment they
    # decide anything about it.
    stop_note: dict | None = None
    # When Telegram was last seen refusing a method because the account is
    # frozen, and what it said. Its own field: a check writes CHECKING over
    # raw_state, and `get_me` answers on a frozen account.
    frozen_at: str | None = None
    frozen_detail: dict | None = None
    # Telegram told the account to wait (FloodWait): nothing it sends - no
    # campaign, no auto-reply - goes before this moment. Kept on the record
    # so the interface shows it and a restart keeps obeying it.
    flood_until: str | None = None

    def flood_left(self) -> float:
        """Seconds Telegram still wants this account to wait; 0 when it may go."""
        until = parse_iso(self.flood_until)
        if until is None:
            return 0.0
        return max(0.0, (until - datetime.now()).total_seconds())

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
