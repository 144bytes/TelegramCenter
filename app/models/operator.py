from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id
from .base import Model
from .enums import AccountState, SpamState


@dataclass
class Operator(Model):
    """Second-line account, in one of two kinds.

    Linked (`account_id` set): made from an account. Everything but the notes
    - name, session, API, proxy, state, checks - is that account's.
    Independent: a @username alone, optionally signed in with its own
    session, API and proxy.
    """
    id: str = field(default_factory=lambda: gen_id("op"))
    account_id: str | None = None
    notes: str = ""
    # ── independent operators only ──────────────────────────────────────
    username: str = ""                 # with or without a leading @
    display_name: str = ""
    key: str = ""                      # session stem if signed in, else ""
    session_file: str = ""
    telegram_id: int | None = None
    api_profile_id: str | None = None
    network_profile_id: str | None = None
    raw_state: str = AccountState.OFFLINE
    last_check_at: str | None = None
    # Messages (see app/messages.py), as on an account.
    last_error: dict | None = None
    spam_state: str = SpamState.UNKNOWN
    spam_detail: dict | None = None
    spam_checked_at: str | None = None
    stop_note: dict | None = None
    frozen_at: str | None = None
    frozen_detail: dict | None = None

    @property
    def linked(self) -> bool:
        return bool(self.account_id)

    def link(self, account_id: str) -> None:
        """Become a link to an account: everything but the notes is its."""
        fresh = Operator(id=self.id, account_id=account_id, notes=self.notes)
        self.__dict__.update(fresh.__dict__)

    @property
    def uname(self) -> str:
        return self.username.lstrip("@")

    @property
    def handle(self) -> str:
        if self.username:
            return f"@{self.uname}"
        return self.display_name or f"op {self.id[:6]}"
