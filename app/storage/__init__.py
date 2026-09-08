"""Storage facade: one JSON repository per entity + the settings store."""
from __future__ import annotations

from .. import config
from ..models import (
    Account, ApiProfile, AutoReplyConfig, Campaign, ConversationState,
    NetworkProfile, Operator, Target, Template,
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
        self.templates: JsonRepository[Template] = JsonRepository(
            config.TEMPLATES_FILE, Template, "templates")
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

    def conversation(self, account_id: str, peer_id: int) -> ConversationState | None:
        return self.conversations.find(
            lambda c: c.account_id == account_id and c.peer_id == peer_id)

    # ── auto-reply resolution ───────────────────────────────────────────
    def global_autoreply(self) -> AutoReplyConfig:
        """The app-wide default, created on first access."""
        cfg = self.auto_reply.get(GLOBAL_OWNER)
        if cfg is None:
            lo, hi = self.settings.default_delay_range()
            cfg = AutoReplyConfig(owner_id=GLOBAL_OWNER, enabled=False,
                                  delay_min_sec=lo, delay_max_sec=hi)
            self.auto_reply.add(cfg)
        return cfg

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

    def default_api_profile(self) -> ApiProfile | None:
        enabled = self.api_profiles.find(lambda p: p.enabled)
        if enabled:
            return enabled
        allp = self.api_profiles.all()
        return allp[0] if allp else None

    def operator_for(self, account: Account) -> Operator | None:
        if not account.operator_id:
            return None
        return self.operators.get(account.operator_id)
