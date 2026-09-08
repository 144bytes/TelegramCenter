"""Checking one Telegram session.

Accounts and operators connect and go stale the same way, so the whole
check is one piece of code, owned by neither service. They used to have
a copy each and the operators' copy fell behind.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..messages import msg
from ..models import Operator
from ..models.enums import AccountState
from ..telegram.errors import is_dead_session, is_frozen, tg_detail
from ..util import now_iso

NOT_AUTHORIZED = "not authorized"


def save(owner, storage, bus=None) -> None:
    """Write the record back and tell the interface, for either kind.

    An account and an operator differ here only in the repository and the
    word the event carries.
    """
    operator = isinstance(owner, Operator)
    (storage.operators if operator else storage.accounts).upsert(owner)
    if bus is not None:
        bus.publish("entity.changed",
                    entity="operator" if operator else "account", id=owner.id)


def switch_off(account, storage, service, bus=None,
               note: dict | None = None) -> bool:
    """Switch an account off. True when it was on.

    Off means off: the connection is dropped, checks leave it alone, its
    campaigns stop. One function for every caller, because forgetting to
    drop the connection is easy and only Telegram's logs would show it.
    """
    if account.disabled:
        return False
    account.disabled = True
    account.stop_note = note
    save(account, storage, bus)
    service.invalidate(account.key)
    if bus is not None:
        bus.publish("account.auto_disabled", account_id=account.id)
    return True


def hold_for_flood(account, storage, bus, seconds: int) -> str:
    """Telegram told the account to wait this long. Returns until when.

    A later, longer wait extends the hold; a shorter one never cuts it.
    """
    moment = datetime.now() + timedelta(seconds=max(0, seconds))
    until = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if account.flood_until is None or until > account.flood_until:
        account.flood_until = until
        save(account, storage, bus)
    return account.flood_until


def mark_frozen(owner, storage, bus=None, detail: dict | None = None) -> bool:
    """Record that Telegram has frozen this account. True when it changed.

    Written here because a freeze surfaces from the antispam check, from a
    send and from a join alike. `frozen_at` is its own field: a check writes
    CHECKING over raw_state and then comes back green, because `get_me` is
    one of the few methods a frozen account still answers.
    """
    if owner.frozen_at:
        return False
    owner.frozen_at = now_iso()
    owner.frozen_detail = detail
    save(owner, storage, bus)
    return True


def mark_dead(owner, storage, bus=None, detail: dict | None = None) -> bool:
    """Record that Telegram has thrown this session's auth key away.

    The health check is not always the one that finds out: the key was seen
    gone from a campaign's ResolveUsernameRequest and from the antispam
    check, and neither wrote it down - so the account reported READY and
    every attempt failed the same way for hours.
    """
    if owner.raw_state == AccountState.AUTH_DEAD:
        return False
    owner.raw_state = AccountState.AUTH_DEAD
    owner.last_error = detail
    owner.last_check_at = now_iso()
    save(owner, storage, bus)
    return True


def clear_frozen(owner, storage, bus=None) -> bool:
    """A real call went through, so the freeze we recorded is over.

    Only a method that does something proves this; a green health check
    does not, which is why `apply_probe_result` never calls it.
    """
    if not owner.frozen_at:
        return False
    owner.frozen_at = None
    owner.frozen_detail = None
    save(owner, storage, bus)
    return True


def apply_probe_result(owner, result: dict) -> None:
    """Write a health_check result onto an account or operator.

    A green result never lifts a recorded freeze: `get_me` answers on a
    frozen account.
    """
    detail = result.get("detail") or ""
    if result.get("ok"):
        owner.raw_state = AccountState.READY
        owner.last_error = None
    elif is_frozen(detail):
        owner.frozen_at = owner.frozen_at or now_iso()
        owner.frozen_detail = tg_detail(detail)
        owner.raw_state = AccountState.ERROR
        owner.last_error = tg_detail(detail)
    elif is_dead_session(detail):
        # Telegram threw the auth key away. Checking again can only fail the
        # same way, so it is kept apart from a plain error: the card offers a
        # fresh login.
        owner.raw_state = AccountState.AUTH_DEAD
        owner.last_error = tg_detail(detail)
    elif NOT_AUTHORIZED in detail or not detail:
        # a session that simply is not signed in is offline, not broken
        owner.raw_state = AccountState.OFFLINE
        owner.last_error = msg("probe.not_authorized")
    else:
        owner.raw_state = AccountState.ERROR
        owner.last_error = tg_detail(detail)
    owner.last_check_at = now_iso()


def _adopt_identity(owner, me) -> None:
    """Copy across what Telegram says about this account.

    An empty field means «not reported», not «cleared». Fields the record
    does not have - an operator has no first name - are skipped.
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

    Saved and announced three times on purpose: grey while the check runs,
    then the verdict. Without the middle one the colour sits stale.
    """
    operator = isinstance(owner, Operator)

    if not owner.key:
        owner.raw_state = AccountState.OFFLINE
        # A handle alone is a normal way to use an operator; an account with
        # no session cannot send anything, so that one is worth saying.
        owner.last_error = None if operator else msg("probe.no_session")
        owner.last_check_at = now_iso()
        save(owner, storage, bus)
        return owner

    owner.raw_state = AccountState.CHECKING
    save(owner, storage, bus)

    result = await service.health_check(owner.key)
    apply_probe_result(owner, result)
    me = result.get("me")
    if result["ok"] and me is not None:
        _adopt_identity(owner, me)
    save(owner, storage, bus)
    return owner
