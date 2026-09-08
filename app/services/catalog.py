"""Targets: the list of channels, groups and people campaigns send to."""
from __future__ import annotations

import re

from ..logging import LOG
from ..messages import AppError, msg, text_of
from ..models import Target
from ..models.enums import EffectiveState, TargetType
from ..telegram.errors import Fault
from ..util import now_iso

MOD = "catalog"


class CatalogError(AppError):
    """Why a channel cannot be added."""
# An invitation, in both shapes Telegram writes them. Checked first:
# the public pattern below matches them too and throws away exactly
# what makes them work.
_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)([\w-]+)",
    re.IGNORECASE)
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(@?[\w_-]+)",
                      re.IGNORECASE)


def parse_invite(raw: str) -> str:
    """The hash of an invitation link, or "" when this is not one.

    An invitation is not a username and must never be stored as one:
    `parse_ref` used to hand «+AbCdEfGhIj» over as a handle, and every
    attempt to reach it answered «no user has that username».
    """
    text = (raw or "").strip()
    m = _INVITE_RE.search(text)
    if m:
        return m.group(1)
    # someone pasted just the hash
    if text.startswith("+") and len(text) > 8:
        return text[1:]
    return ""


def parse_ref(raw: str) -> str:
    """Accept a @name, a t.me link or a bare id and return the bare handle.

    Invitations are not handles - ask `parse_invite` first.
    """
    text = (raw or "").strip()
    m = _LINK_RE.search(text)
    if m:
        text = m.group(1)
    return text.lstrip("@").strip()


class CatalogService:
    def __init__(self, storage, service, bus, state, delivery, campaigns):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        # The same object the scheduler sends through, so a channel that
        # cannot be commented under is reported before a campaign finds out.
        self.delivery = delivery
        # Deleting a channel has to reach the campaigns that were sending to
        # it, and a campaign is edited by the campaign service or by nobody.
        self.campaigns = campaigns

    # ── targets ─────────────────────────────────────────────────────────
    def create_target(self, raw: str, title: str = "",
                      type_: str = TargetType.CHANNEL) -> Target:
        invite = parse_invite(raw)
        if invite:
            existing = self.storage.targets.find(lambda t: t.invite == invite)
            if existing is not None:
                raise CatalogError("err.target.invite_exists")
            # A private chat is a chat, not a broadcast channel: nobody hands
            # out an invitation to a channel anyone can simply open.
            target = Target(invite=invite, title=title or f"+{invite}",
                            type=TargetType.GROUP)
            self.storage.targets.add(target)
            self.bus.publish("entity.changed", entity="target", id=target.id)
            return target
        ref = parse_ref(raw)
        if not ref:
            raise CatalogError("err.target.empty")
        if ref.lstrip("-").isdigit():
            target = Target(telegram_id=int(ref), title=title or ref, type=type_)
        else:
            existing = self.storage.targets.find(
                lambda t: t.username.lower() == ref.lower())
            if existing is not None:
                raise CatalogError("err.target.exists", name=f"@{ref}")
            target = Target(username=ref, title=title or f"@{ref}", type=type_)
        self.storage.targets.add(target)
        self.bus.publish("entity.changed", entity="target", id=target.id)
        return target

    def add_many(self, blob: str) -> dict:
        """Paste a list — one link per line. Duplicates are skipped, not errors."""
        added, skipped = 0, 0
        for line in (blob or "").splitlines():
            if not line.strip():
                continue
            try:
                self.create_target(line)
                added += 1
            except CatalogError:
                skipped += 1
        return {"added": added, "skipped": skipped}

    def update_target(self, target: Target, **fields) -> Target:
        for k, v in fields.items():
            if hasattr(target, k):
                setattr(target, k, v)
        self.storage.targets.upsert(target)
        self.bus.publish("entity.changed", entity="target", id=target.id)
        return target

    def delete_target(self, target_id: str) -> bool:
        """Delete the channel everywhere, campaigns included.

        It used to stay in every campaign that named it, showing «Канал не
        найден» with no way to clear the row from that side.
        """
        ok = self.storage.targets.delete(target_id)
        if ok:
            self.campaigns.forget_target(target_id)
            self.bus.publish("entity.changed", entity="target", id=target_id)
        return ok

    def _probe_key(self, target: Target | None = None) -> str | None:
        """Which account does the checking.

        An account that will actually send here comes first: «Доступен» said by
        some other account is a promise nobody made - a chat one account has
        joined is not a chat another can write to.
        """
        ready = [a for a in self.storage.accounts.all()
                 if a.key
                 and self.state.account_effective(a) == EffectiveState.READY]
        if target is not None:
            senders = {c.account_id for c in self.storage.campaigns.all()
                       if target.id in c.target_ids}
            for account in ready:
                if account.id in senders:
                    return account.key
        return ready[0].key if ready else None

    @staticmethod
    def _reason(message: dict, fault: str | None) -> dict:
        """What the channel's status says: «Не найден» when the address leads
        nowhere, the same words a campaign that met it would use."""
        if fault == Fault.NOT_FOUND:
            return msg("delivery.not_found")
        return message

    async def check_target(self, target: Target) -> Target:
        key = self._probe_key(target)
        if key is None:
            target.last_check_error = msg("result.no_ready_account")
            target.last_check = now_iso()
            self.storage.targets.upsert(target)
            self.bus.publish("entity.changed", entity="target", id=target.id)
            return target

        name = target.username or target.invite or str(target.telegram_id or "")
        if not target.invite:
            # An invitation cannot be looked up by name - only opened - so the
            # delivery probe below is the whole check for one of those.
            result = await self.service.check_target(name, key=key)
            if not result.get("ok"):
                target.last_check_error = self._reason(result["error"],
                                                       result.get("fault"))
                LOG.warning(f"target {name}: {text_of(result['error'])}",
                            module=MOD)
                target.last_check = now_iso()
                self.storage.targets.upsert(target)
                self.bus.publish("entity.changed", entity="target", id=target.id)
                return target
            target.title = result.get("title") or target.title
            target.username = result.get("username") or target.username
            target.telegram_id = result.get("id") or target.telegram_id
            # CHANNEL / GROUP / USER as Telegram reports it. Only a label now -
            # delivery asks again when it sends - but a wrong label still shows.
            target.type = result.get("kind") or target.type

        refusal = (await self.delivery.probe(key, target)
                   if self.delivery is not None else None)
        target.last_check_error = (self._reason(refusal.message, refusal.fault)
                                   if refusal is not None else None)
        if refusal is not None:
            LOG.warning(f"target {name}: {text_of(refusal.message)}", module=MOD)
        target.last_check = now_iso()
        self.storage.targets.upsert(target)
        self.bus.publish("entity.changed", entity="target", id=target.id)
        return target

