"""Storage facade: one JSON repository per entity + the settings store."""
from __future__ import annotations

from .. import config
from ..models import (
    Account, ApiProfile, AutoReplyConfig, Campaign, ConversationState,
    NetworkProfile, Operator, Target,
)
from ..models.enums import GLOBAL_OWNER
from .json_repository import JsonRepository
from .settings import SettingsStore


class Storage:
    def __init__(self) -> None:
        self.settings = SettingsStore()

        self.accounts: JsonRepository[Account] = JsonRepository(
            config.ACCOUNTS_FILE, Account, "accounts")
        self.operators: JsonRepository[Operator] = JsonRepository(
            config.OPERATORS_FILE, Operator, "operators")
        self.targets: JsonRepository[Target] = JsonRepository(
            config.TARGETS_FILE, Target, "targets")
        self.campaigns: JsonRepository[Campaign] = JsonRepository(
            config.CAMPAIGNS_FILE, Campaign, "campaigns")
        self.api_profiles: JsonRepository[ApiProfile] = JsonRepository(
            config.API_PROFILES_FILE, ApiProfile, "api_profiles")
        self.network_profiles: JsonRepository[NetworkProfile] = JsonRepository(
            config.NETWORK_PROFILES_FILE, NetworkProfile, "network_profiles")
        self.auto_reply: JsonRepository[AutoReplyConfig] = JsonRepository(
            config.AUTO_REPLY_FILE, AutoReplyConfig, "configs",
            id_field="owner_id")
        self.conversations: JsonRepository[ConversationState] = JsonRepository(
            config.CONVERSATIONS_FILE, ConversationState, "conversations")

        # The app-wide default always exists, so reading it never writes.
        if self.auto_reply.get(GLOBAL_OWNER) is None:
            lo, hi = self.settings.default_delay_range()
            self.auto_reply.add(AutoReplyConfig(
                owner_id=GLOBAL_OWNER, enabled=False,
                delay_min_sec=lo, delay_max_sec=hi))

    def conversation(self, account_id: str, peer_id: int) -> ConversationState | None:
        return self.conversations.find(
            lambda c: c.account_id == account_id and c.peer_id == peer_id)

    # ── auto-reply resolution ───────────────────────────────────────────
    def global_autoreply(self) -> AutoReplyConfig:
        return self.auto_reply.get(GLOBAL_OWNER)

    def autoreply_for(self, account_id: str) -> AutoReplyConfig:
        """The account's own config, or a live view of the global default.

        The link is live on purpose: changing the global default immediately
        changes behaviour for every account that has not set up its own.
        """
        own = self.auto_reply.get(account_id)
        return own if own is not None else self.global_autoreply()

    def has_own_autoreply(self, account_id: str) -> bool:
        return self.auto_reply.get(account_id) is not None

    # ── convenience queries ─────────────────────────────────────────────
    def campaigns_for_account(self, account_id: str) -> list[Campaign]:
        return self.campaigns.filter(lambda c: c.account_id == account_id)

    def operator_for(self, account: Account) -> Operator | None:
        if not account.operator_id:
            return None
        return self.operators.get(account.operator_id)

    # ── operators made from an account ──────────────────────────────────
    def linked_account(self, op: Operator) -> Account | None:
        return self.accounts.get(op.account_id) if op.account_id else None

    def linked_operator(self, account: Account) -> Operator | None:
        return self.operators.find(lambda o: o.account_id == account.id)

    def session_holder(self, op: Operator):
        """The record whose session, profiles and verdicts this operator uses:
        its account when linked, itself otherwise."""
        return self.linked_account(op) if op.account_id else op

    def operator_uname(self, op: Operator) -> str:
        """The username @operator turns into."""
        holder = self.session_holder(op)
        return (getattr(holder, "username", None) or "").lstrip("@") if holder else ""

    def operator_handle(self, op: Operator) -> str:
        account = self.linked_account(op)
        return account.handle if account is not None else op.handle

    def record_for_key(self, key: str):
        """The account or independent operator that owns this session."""
        if not key:
            return None
        return (self.accounts.find(lambda a: a.key == key)
                or self.operators.find(lambda o: o.key == key))
