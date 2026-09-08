"""Broadcast accounts: CRUD and making an operator of one.

Checking a session is not here: that is `probing`, shared with
independent operators.
"""
from __future__ import annotations

from ..logging import LOG
from ..messages import AppError
from ..models import Account, Operator
from ..models.enums import SpamState
from .sessions import checked_links, connection_changed, release_session

MOD = "accounts"


class AccountError(AppError):
    """Why an account cannot do what was asked."""


class AccountService:
    def __init__(self, storage, service, bus, state, campaigns, operators,
                 pacer, autoreply):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        # Deleting an account leaves its campaigns with nothing to send
        # through; they are stopped rather than left scheduled.
        self.campaigns = campaigns
        self.operators = operators
        # the safety net's count, started afresh when the user switches the
        # account back on
        self.pacer = pacer
        # the account's own auto-reply is switched in the same save
        self.autoreply = autoreply

    # ── credential resolution (handed to TelegramService) ───────────────
    def creds_for(self, key: str) -> dict | None:
        """Which api_id/api_hash/proxy a session key must use.

        Only the record's own API profile: without one there is nothing to
        connect with.
        """
        owner = self.storage.record_for_key(key)
        if owner is None or not owner.api_profile_id:
            return None
        api_profile = self.storage.api_profiles.get(owner.api_profile_id)
        if api_profile is None or not api_profile.api_id:
            return None
        proxy = None
        if owner.network_profile_id:
            net = self.storage.network_profiles.get(owner.network_profile_id)
            if net is not None and net.enabled:
                proxy = net.as_telethon_proxy()
        return {"api_id": api_profile.api_id, "api_hash": api_profile.api_hash,
                "proxy": proxy}

    # ── CRUD ────────────────────────────────────────────────────────────
    def create(self, **fields) -> Account:
        account = Account(**fields)
        self.storage.accounts.add(account)
        self._changed(account.id)
        return account

    def edit(self, account: Account, values: dict) -> Account:
        """Everything one save changes on an account, applied as one change.

        What it names is checked before anything is touched, and the own
        auto-reply - the one part that can still refuse - is switched
        first, so a refusal leaves the account as it was. Then off, if it
        goes off; the connection; the operator; on, if it goes on. Off
        first and on last means a connection is only ever made with the
        whole change in place: switching off and to a new proxy never
        reaches Telegram through that proxy on the way out.
        """
        links = checked_links(self.storage, values)
        if "autoreply" in values:
            self.autoreply.set_enabled(account.id, bool(values["autoreply"]))
        disabled = bool(values["disabled"]) if "disabled" in values else None
        if disabled is True and not account.disabled:
            self.set_disabled(account, True)
        connection = {k: v for k, v in links.items() if k != "operator_id"}
        if connection:
            self.update(account, **connection)
        if "operator_id" in links:
            self.set_operator(account, links["operator_id"])
        if disabled is False and account.disabled:
            self.set_disabled(account, False)
        return account

    def update(self, account: Account, **fields) -> Account:
        reconnects = connection_changed(account, fields)
        for k, v in fields.items():
            if hasattr(account, k):
                setattr(account, k, v)
        if reconnects:
            # The user has just chosen how this account connects, so whatever
            # the app said about the old choice is answered.
            account.stop_note = None
        self.storage.accounts.upsert(account)
        if reconnects:
            self.service.invalidate(account.key)
        self._changed(account.id)
        return account

    def _changed(self, id_: str) -> None:
        self.bus.publish("entity.changed", entity="account", id=id_)
        # a linked operator shows the account's name, state and profiles
        account = self.storage.accounts.get(id_)
        op = self.storage.linked_operator(account) if account else None
        if op is not None:
            self.bus.publish("entity.changed", entity="operator", id=op.id)

    def set_disabled(self, account: Account, disabled: bool) -> Account:
        """Switch the account on or off. Off means off: no connection, no
        checks, no campaigns, no traffic of any kind."""
        account.disabled = bool(disabled)
        # whoever moved the switch has read the note
        account.stop_note = None
        if account.disabled:
            # A pooled client holds a live connection to Telegram. Left
            # alone, a switched-off account goes on sitting there connected.
            self.service.invalidate(account.key)
        if not account.disabled:
            # Switching it back on means «I have dealt with it». Keeping the old
            # verdict would leave the account looking broken until the next check.
            account.spam_state = SpamState.UNKNOWN
            account.spam_detail = None
            account.spam_checked_at = None
            # Same for a freeze, and it is the user's only lever: nothing we can
            # ask says «no longer frozen», only a call that succeeds - and a
            # frozen account never gets that far.
            account.frozen_at = None
            account.frozen_detail = None
            # and the failures that switched it off are what they dealt with
            self.pacer.forget(account.id)
        self.storage.accounts.upsert(account)
        self._changed(account.id)
        return account

    def set_operator(self, account: Account, operator_id: str | None) -> Account:
        """The operator @operator turns into. Rules that name it stay as they
        are: the page warns, and such a reply is not sent without one."""
        account.operator_id = operator_id or None
        self.storage.accounts.upsert(account)
        self._changed(account.id)
        return account

    def delete(self, account_id: str, keep_operator: bool = False) -> bool:
        """Delete the account, its session file and everything naming it.

        An operator made from it goes too, unless the user keeps it as a
        plain @username.
        """
        account = self.storage.accounts.get(account_id)
        if account is None:
            return False
        linked = self.storage.linked_operator(account)
        if linked is not None:
            if keep_operator:
                linked.account_id = None
                linked.username = account.username or ""
                linked.display_name = account.display_name
                self.storage.operators.upsert(linked)
                self.bus.publish("entity.changed", entity="operator", id=linked.id)
            else:
                self.operators.delete(linked.id)
        release_session(self.service, account.key)
        # The auto-reply config and what the auto-responder remembered go
        # with it. The campaigns stay - they can be pointed at another
        # account - but they stop and say so.
        self.storage.auto_reply.delete(account_id)
        self.storage.conversations.delete_where(
            lambda c: c.account_id == account_id)
        ok = self.storage.accounts.delete(account_id)
        if ok:
            self.campaigns.forget_account(account_id)
        self.bus.publish("entity.changed", entity="account", id=account_id)
        return ok

    # ── promotion ───────────────────────────────────────────────────────
    def promote_to_operator(self, account: Account) -> Operator:
        """Make an operator of this account: a link, not a second session."""
        if not account.key:
            raise AccountError("err.account.no_session")
        existing = self.storage.linked_operator(account)
        if existing is not None:
            raise AccountError("err.operator.exists", name=account.handle)
        operator = Operator(account_id=account.id)
        self.storage.operators.add(operator)
        LOG.info(f"{account.handle} is now also an operator", module=MOD)
        self.bus.publish("entity.changed", entity="operator", id=operator.id)
        return operator
