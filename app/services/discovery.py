"""Session auto-discovery.

On startup (and on demand) scan the two session folders. Any `*.session` file
without a record yet is added — as a broadcast Account or as an Operator.
Dropping session files into the folder is the only "import" there is.

    %APPDATA%\\TelegramCenter\\sessions\\campaign\\*.session   -> broadcast accounts
    %APPDATA%\\TelegramCenter\\sessions\\operators\\*.session  -> operators
"""
from __future__ import annotations

import re

from .. import config
from ..logging import LOG
from ..models import Account, Operator
from ..models.enums import AccountState
from ..util import now_iso

MOD = "discovery"
_ID_RE = re.compile(r"(?:session|op)_(-?\d+)")


def discover(storage) -> dict:
    report = {"accounts": 0, "operators": 0}
    default_api = storage.default_api_profile()
    api_id = default_api.id if default_api else None

    for sf in sorted(config.CAMPAIGN_SESSIONS_DIR.glob("*.session")):
        key = sf.stem
        if key.startswith("pending_"):
            continue
        if storage.accounts.find(lambda a, k=key: a.key == k):
            continue
        m = _ID_RE.fullmatch(key)
        storage.accounts.add(Account(
            key=key, session_file=sf.name,
            telegram_id=int(m.group(1)) if m else None,
            api_profile_id=api_id,
            raw_state=AccountState.OFFLINE,
            created_at=now_iso()))
        report["accounts"] += 1

    for sf in sorted(config.OPERATOR_SESSIONS_DIR.glob("*.session")):
        key = sf.stem
        if key.startswith("op_pending_"):
            continue
        if storage.operators.find(lambda o, k=key: o.key == k):
            continue
        m = _ID_RE.fullmatch(key)
        storage.operators.add(Operator(
            key=key, session_file=sf.name,
            telegram_id=int(m.group(1)) if m else None,
            # The same default an account gets. Without it a discovered
            # operator had a session and no keys to use it with, so it showed
            # up as broken while an account found the same way was fine.
            api_profile_id=api_id))
        report["operators"] += 1

    if report["accounts"] or report["operators"]:
        LOG.info(f"discovered {report['accounts']} account(s), "
                 f"{report['operators']} operator(s) from session files", module=MOD)
    return report


def adopt_default_api(storage) -> int:
    """Give the default API profile to sessions that have none.

    A session discovered before any profile existed - the usual order on a
    fresh install, since discovery runs at startup - was left with no keys and
    no way back: rescanning skipped it because the record already existed. An
    empty api_profile_id is never a deliberate choice, it just makes the
    account unusable, so a rescan repairs the link.
    """
    default = storage.default_api_profile()
    if default is None:
        return 0
    fixed = 0
    for repo in (storage.accounts, storage.operators):
        for owner in repo.all():
            if owner.key and not owner.api_profile_id:
                owner.api_profile_id = default.id
                repo.upsert(owner)
                fixed += 1
    if fixed:
        LOG.info(f"gave {default.name!r} to {fixed} session(s) that had no "
                 f"API profile", module=MOD)
    return fixed


def prune_missing_sessions(storage) -> int:
    """An operator or account may point at a session file that is gone.

    We never delete the record — the @username alone is still useful — we only
    stop claiming it is logged in, so the UI can show the red dot.
    """
    changed = 0
    for op in storage.operators.all():
        if op.key and not (config.OPERATOR_SESSIONS_DIR / f"{op.key}.session").is_file():
            LOG.warning(f"operator {op.handle}: session {op.key!r} missing", module=MOD)
            op.key, op.session_file = "", ""
            storage.operators.upsert(op)
            changed += 1
    for acc in storage.accounts.all():
        if acc.key and not (config.CAMPAIGN_SESSIONS_DIR / f"{acc.key}.session").is_file():
            LOG.warning(f"account {acc.handle}: session {acc.key!r} missing", module=MOD)
            acc.key, acc.session_file = "", ""
            acc.raw_state = AccountState.OFFLINE
            acc.last_error = "Файл сессии не найден"
            storage.accounts.upsert(acc)
            changed += 1
    return changed
