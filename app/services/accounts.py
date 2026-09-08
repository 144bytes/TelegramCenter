"""Broadcast accounts: CRUD and promotion to operator.

Checking a session is not here: that is one piece of code in `probing`, shared
with operators.
"""
from __future__ import annotations

import shutil

from .. import config
from ..logging import LOG
from ..models import Account, Operator
from ..models.enums import SpamState

MOD = "accounts"


class AccountService:
    def __init__(self, storage, service, bus, state):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state

    def _changed(self, id_: str) -> None:
        self.bus.publish("entity.changed", entity="account", id=id_)

    # ── credential resolution (handed to TelegramService) ───────────────
    def creds_for(self, key: str) -> dict | None:
        """Which api_id/api_hash/proxy a given session key must use.

        Accounts and operators both carry their own profiles; anything without
        one falls back to the default API profile.
        """
        owner = (self.storage.accounts.find(lambda a: a.key == key)
                 or self.storage.operators.find(lambda o: o.key == key))
        api_profile = None
        proxy = None
        if owner is not None:
            if owner.api_profile_id:
                api_profile = self.storage.api_profiles.get(owner.api_profile_id)
            if owner.network_profile_id:
                net = self.storage.network_profiles.get(owner.network_profile_id)
                if net is not None and net.enabled:
                    proxy = net.as_telethon_proxy()
        if api_profile is None:
            api_profile = self.storage.default_api_profile()
        if api_profile is None or not api_profile.api_id:
            return None
        return {"api_id": api_profile.api_id, "api_hash": api_profile.api_hash,
                "proxy": proxy}

    # ── CRUD ────────────────────────────────────────────────────────────
    def create(self, **fields) -> Account:
        if "api_profile_id" not in fields:
            default = self.storage.default_api_profile()
            fields["api_profile_id"] = default.id if default else None
        account = Account(**fields)
        self.storage.accounts.add(account)
        self._changed(account.id)
        return account

    def update(self, account: Account, **fields) -> Account:
        for k, v in fields.items():
            if hasattr(account, k):
                setattr(account, k, v)
        self.storage.accounts.upsert(account)
        self._changed(account.id)
        return account

    def set_disabled(self, account: Account, disabled: bool) -> Account:
        account.disabled = bool(disabled)
        if not account.disabled:
            # Switching it back on means "I have dealt with it". Keeping the old
            # verdict would leave the account looking broken until the next
            # check, and the next check is what will say whether it really is.
            account.spam_state = SpamState.UNKNOWN
            account.spam_detail = ""
            account.spam_checked_at = None
        self.storage.accounts.upsert(account)
        self._changed(account.id)
        return account

    def set_operator(self, account: Account, operator_id: str | None) -> Account:
        """Changing the operator is one of the five @operator validation points:
        dropping an operator that live rules depend on must not leave those
        rules armed."""
        account.operator_id = operator_id or None
        self.storage.accounts.upsert(account)
        if not account.operator_id:
            from .autoreply import disarm_operator_rules
            disarm_operator_rules(self.storage, self.bus, account)
        self._changed(account.id)
        return account

    def delete(self, account_id: str, delete_session: bool = False) -> bool:
        account = self.storage.accounts.get(account_id)
        if account is None:
            return False
        if account.key:
            try:
                self.service.run(self.service.remove_session(account.key), timeout=10)
            except Exception:  # noqa: BLE001
                pass
            if delete_session:
                for p in config.CAMPAIGN_SESSIONS_DIR.glob(f"{account.key}.session*"):
                    try:
                        p.unlink()
                    except OSError as exc:
                        LOG.warning(f"could not remove {p.name}: {exc}", module=MOD)
        # the account's own auto-reply config goes with it; campaigns are kept
        # and surface as "account not found" rather than vanishing silently
        self.storage.auto_reply.delete(account_id)
        ok = self.storage.accounts.delete(account_id)
        self._changed(account_id)
        return ok

    # ── probing ─────────────────────────────────────────────────────────
    # ── promotion ───────────────────────────────────────────────────────
    def promote_to_operator(self, account: Account) -> Operator:
        """Duplicate (never move) the session into the operator folder.

        The broadcast account keeps working; the operator gets its own copy so
        the two can be connected independently.
        """
        if not account.key:
            raise ValueError("У аккаунта нет файла сессии")
        existing = self.storage.operators.find(
            lambda o: o.telegram_id and o.telegram_id == account.telegram_id)
        if existing is not None:
            raise ValueError(f"Оператор {existing.handle} уже существует")

        new_key = f"op_{account.telegram_id or account.key.split('_')[-1]}"
        # the copy keeps the account's connection settings: it is the same
        # Telegram account, so it must reach Telegram the same way
        src = config.CAMPAIGN_SESSIONS_DIR / f"{account.key}.session"
        if not src.is_file():
            raise ValueError("Файл сессии не найден на диске")
        shutil.copy2(src, config.OPERATOR_SESSIONS_DIR / f"{new_key}.session")

        operator = Operator(
            username=account.username or "", display_name=account.display_name,
            key=new_key, session_file=f"{new_key}.session",
            telegram_id=account.telegram_id,
            api_profile_id=account.api_profile_id,
            network_profile_id=account.network_profile_id,
            raw_state=account.raw_state)
        self.storage.operators.add(operator)
        LOG.info(f"{account.handle} promoted to operator {operator.handle}", module=MOD)
        self.bus.publish("entity.changed", entity="operator", id=operator.id)
        return operator
