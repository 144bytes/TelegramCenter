"""Session auto-discovery.

On start-up and on demand, scan the two session folders: any *.session
with no record is added as an account or as an operator. Dropping files
into the folder is the only «import» there is.
"""
from __future__ import annotations

import re

from .. import config
from ..logging import LOG
from ..messages import msg
from ..models import Account, Operator
from ..models.enums import AccountState
from ..util import now_iso

MOD = "discovery"
_ID_RE = re.compile(r"(?:session|op)_(-?\d+)")

# What a login calls its session file while it is still going. A
# successful login renames it, so a file still named this way is from
# an attempt that failed or was cut short.
PENDING_PREFIXES = ("pending_", "op_pending_")


def purge_pending_sessions(keep: set[str] | None = None) -> int:
    """Delete the session files of logins that never finished.

    Only signed-in sessions are kept: a half-finished login is a file
    nobody can use or see. Swept on every start, because the one case the
    login cannot clean up after is the app being closed mid-way. `keep`
    names logins still going, so a rescan cannot pull a file out from
    under one.
    """
    keep = keep or set()
    removed = 0
    for folder in (config.CAMPAIGN_SESSIONS_DIR, config.OPERATOR_SESSIONS_DIR):
        for path in folder.glob("*.session*"):
            stem = path.name.split(".session")[0]
            if stem in keep or not stem.startswith(PENDING_PREFIXES):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                LOG.warning(f"could not delete {path.name}: {exc}", module=MOD)
    if removed:
        LOG.info(f"removed {removed} leftover session file(s) from logins that "
                 f"never finished", module=MOD)
    return removed


def discover(storage) -> dict:
    """Add a record for every session file that has none.

    A found session gets no API profile: the card asks for one, and nothing
    connects until the user picks it.
    """
    report = {"accounts": 0, "operators": 0}

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
            telegram_id=int(m.group(1)) if m else None))
        report["operators"] += 1

    if report["accounts"] or report["operators"]:
        LOG.info(f"discovered {report['accounts']} account(s), "
                 f"{report['operators']} operator(s) from session files", module=MOD)
    return report


def prune_missing_sessions(storage) -> int:
    """An operator or account may point at a session file that is gone.

    The record stays - the @username is still useful - we only stop
    claiming it is logged in.
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
            acc.last_error = msg("probe.session_file_missing")
            storage.accounts.upsert(acc)
            changed += 1
    return changed
