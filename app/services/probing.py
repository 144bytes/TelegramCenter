"""Checking one Telegram session.

Accounts and operators connect the same way and go stale the same way, so the
whole check - ask Telegram, read the answer onto the record, tell the
interface - is one piece of code. It lives apart from both services precisely
so neither owns it and the two cannot drift: they used to have a copy each,
and the operators' copy quietly fell behind.
"""
from __future__ import annotations

from ..models import Operator
from ..models.enums import AccountState
from ..util import now_iso

NOT_AUTHORIZED = "not authorized"


def apply_probe_result(owner, result: dict) -> None:
    """Write a TelegramService.health_check result onto an account or operator."""
    if result.get("ok"):
        owner.raw_state = AccountState.READY
        owner.last_error = None
    else:
        detail = result.get("detail") or "не авторизован"
        # a session that simply is not signed in is offline, not broken
        owner.raw_state = (AccountState.OFFLINE if NOT_AUTHORIZED in detail
                           else AccountState.ERROR)
        owner.last_error = detail
    owner.last_check_at = now_iso()


def _adopt_identity(owner, me) -> None:
    """Copy across what Telegram says about this account.

    An empty field from Telegram means "not reported", not "cleared", so what
    we already knew is kept. Fields the record does not have - an operator has
    no first/last name - are simply skipped.
    """
    owner.telegram_id = getattr(me, "id", owner.telegram_id)
    owner.username = getattr(me, "username", None) or owner.username
    for field in ("first_name", "last_name"):
        if hasattr(owner, field):
            setattr(owner, field, getattr(me, field, "") or "")
    if getattr(me, "restricted", False):
        owner.raw_state = AccountState.RESTRICTED


async def probe(owner, storage, service, bus):
    """Is this session still usable? Works on an account or an operator.

    The record is saved and announced three times on purpose: grey while the
    check runs, then the verdict. Without the middle one the interface sits on
    a stale colour for as long as Telegram takes to answer.
    """
    operator = isinstance(owner, Operator)
    repo = storage.operators if operator else storage.accounts
    entity = "operator" if operator else "account"

    def save():
        repo.upsert(owner)
        bus.publish("entity.changed", entity=entity, id=owner.id)

    if not owner.key:
        owner.raw_state = AccountState.OFFLINE
        # A handle alone is a normal way to use an operator; an account with
        # no session cannot send anything, so that one is worth saying.
        owner.last_error = None if operator else "Файл сессии не назначен"
        owner.last_check_at = now_iso()
        save()
        return owner

    owner.raw_state = AccountState.CHECKING
    save()

    result = await service.health_check(owner.key)
    apply_probe_result(owner, result)
    me = result.get("me")
    if result["ok"] and me is not None:
        _adopt_identity(owner, me)
    save()
    return owner
