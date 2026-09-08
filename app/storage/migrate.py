"""One-time v1 -> v2 data migration.

Runs before Storage() is built, on the raw JSON files, and only when
settings.json says schema_version < 2.

Two hard rules:
  * `sessions/` is never read, written, moved or deleted here.
  * nothing is deleted. Records that no longer fit the model are renamed to
    `*.removed`; broken references are kept and surfaced as issues in the UI
    instead of being silently dropped.

A single snapshot of `data/` is taken into `data.v1.bak/` before anything is
written. It is never taken twice and can be deleted by hand once the user is
satisfied with the result.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .. import config
from ..logging import LOG
from ..models.enums import (
    AccountState, AutoReplyKind, CampaignState, GLOBAL_OWNER, ScheduleMode,
)
from ..util import gen_id

TARGET_SCHEMA = 3

# v1 OperationalState -> (v2 raw_state, force_disabled)
_ACCOUNT_STATE_MAP = {
    "OFFLINE": (AccountState.OFFLINE, False),
    "CHECKING": (AccountState.CHECKING, False),
    "READY": (AccountState.READY, False),
    "WARNING": (AccountState.READY, False),      # now an issue, not a state
    "RESTRICTED": (AccountState.RESTRICTED, False),
    "ERROR": (AccountState.ERROR, False),
    "DISABLED": (AccountState.OFFLINE, True),
}

_CAMPAIGN_STATE_MAP = {
    "DRAFT": CampaignState.DRAFT,
    "SCHEDULED": CampaignState.SCHEDULED,
    "RUNNING": CampaignState.PAUSED,   # nothing is running at startup
    "PAUSED": CampaignState.PAUSED,
    "COMPLETED": CampaignState.DONE,
    "FAILED": CampaignState.PAUSED,
    "CANCELLED": CampaignState.DRAFT,
}

_TRIGGER_MAP = {
    "FIRST_MESSAGE": AutoReplyKind.FIRST_MESSAGE,
    "AFTER_HOURS": AutoReplyKind.PERIODIC,
    "TRANSFER": AutoReplyKind.FAQ,
    "FAQ": AutoReplyKind.FAQ,
}


# ── low-level helpers ───────────────────────────────────────────────────
def _read(path: Path, root_key: str) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        LOG.warning(f"migrate: {path.name} unreadable ({exc}); left untouched",
                    module="migrate")
        return []
    rows = data.get(root_key, []) if isinstance(data, dict) else []
    return [r for r in rows if isinstance(r, dict)]


def _write(path: Path, root_key: str, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({root_key: rows}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _retire(path: Path) -> None:
    """Move a file out of the way instead of deleting it."""
    if path.is_file():
        dest = path.with_suffix(path.suffix + ".removed")
        if dest.exists():
            dest.unlink()
        os.replace(path, dest)
        LOG.info(f"migrate: {path.name} -> {dest.name}", module="migrate")


def _snapshot() -> None:
    if config.V1_BACKUP_DIR.exists():
        return
    if not config.DATA_DIR.is_dir() or not any(config.DATA_DIR.iterdir()):
        return
    shutil.copytree(config.DATA_DIR, config.V1_BACKUP_DIR)
    LOG.info(f"migrate: snapshot -> {config.V1_BACKUP_DIR}", module="migrate")


# ── per-file migrations ─────────────────────────────────────────────────
def _migrate_accounts() -> int:
    rows = _read(config.ACCOUNTS_FILE, "accounts")
    if not rows:
        return 0
    out = []
    for r in rows:
        state, forced_off = _ACCOUNT_STATE_MAP.get(
            str(r.get("operational_state", "OFFLINE")).upper(),
            (AccountState.OFFLINE, False))
        out.append({
            "id": r.get("id") or gen_id("acc"),
            "key": r.get("key", ""),
            "session_file": r.get("session_file", ""),
            "telegram_id": r.get("telegram_id"),
            "username": r.get("username"),
            "phone": r.get("phone", ""),
            "first_name": r.get("first_name", ""),
            "last_name": r.get("last_name", ""),
            "api_profile_id": r.get("api_profile_id"),
            "network_profile_id": r.get("network_profile_id"),
            "operator_id": r.get("operator_id"),
            "raw_state": state,
            "disabled": bool(r.get("disabled", False)) or forced_off,
            "created_at": r.get("created_at"),
            "last_check_at": r.get("last_check_at"),
            "last_error": r.get("last_error"),
        })
    _write(config.ACCOUNTS_FILE, "accounts", out)
    return len(out)


def _migrate_campaigns() -> int:
    rows = _read(config.CAMPAIGNS_FILE, "campaigns")
    if not rows:
        return 0
    templates = {t.get("id"): t for t in _read(config.TEMPLATES_FILE, "templates")}
    out = []
    for r in rows:
        tpl_id = r.get("template_id") or None
        tpl = templates.get(tpl_id) or {}
        # snapshot the template text: from now on the campaign owns its text
        text = r.get("message_text") or tpl.get("text", "")

        target_ids = r.get("target_ids")
        if not isinstance(target_ids, list):
            single = r.get("target_id")
            target_ids = [single] if single else []

        if r.get("cyclic"):
            lo = int(r.get("interval_min_sec", 15) or 15)
            hi = int(r.get("interval_max_sec", 40) or 40)
            schedule = {"mode": ScheduleMode.INTERVAL, "at": None, "times": [],
                        "every_sec": max(lo, hi)}
        elif r.get("scheduled_at"):
            schedule = {"mode": ScheduleMode.ONCE, "at": r.get("scheduled_at"),
                        "times": [], "every_sec": None}
        else:
            schedule = {"mode": ScheduleMode.ONCE, "at": None, "times": [],
                        "every_sec": None}

        out.append({
            "id": r.get("id") or gen_id("camp"),
            "name": r.get("name", ""),
            "account_id": r.get("account_id", ""),
            "target_ids": target_ids,
            "message_text": text,
            "source_template_id": tpl_id,
            "schedule": schedule,
            "gap_min_sec": int(r.get("interval_min_sec", 15) or 15),
            "gap_max_sec": int(r.get("interval_max_sec", 40) or 40),
            "raw_state": _CAMPAIGN_STATE_MAP.get(
                str(r.get("status", "DRAFT")).upper(), CampaignState.DRAFT),
            "results": [{"target_id": tid, "status": "PENDING", "error": None,
                         "sent_at": None, "attempts": 0} for tid in target_ids],
            "created_at": r.get("created_at"),
            "next_run_at": None,
            "last_run_at": None,
            "last_error": r.get("last_error"),
        })
    _write(config.CAMPAIGNS_FILE, "campaigns", out)
    return len(out)


def _migrate_autoreply() -> int:
    """v1 kept a flat list of global rules. v2 keeps one config per owner;
    the old global rules become the app-wide default every account inherits."""
    rows = _read(config.AUTO_REPLY_FILE, "rules")
    if not rows:
        return 0
    lo = int(_settings_value("autoreply.default_delay_min_sec", 300))
    hi = int(_settings_value("autoreply.default_delay_max_sec", 600))

    rules: list[dict] = []
    seen_singletons: set[str] = set()
    for r in rows:
        kind = _TRIGGER_MAP.get(str(r.get("trigger", "FAQ")).upper(),
                                AutoReplyKind.FAQ)
        if kind in AutoReplyKind.SINGLETON:
            if kind in seen_singletons:
                kind = AutoReplyKind.FAQ      # demote the extras, keep the text
            else:
                seen_singletons.add(kind)
        response = r.get("response", "") or ""
        enabled = bool(r.get("enabled", True))
        # §11: a rule that says @operator cannot be active without an operator,
        # and the global default has no account and therefore no operator.
        if "@operator" in response:
            enabled = False
            LOG.warning(
                f"migrate: rule {r.get('id')} disabled - contains @operator "
                f"but no operator is bound", module="migrate")
        rules.append({
            "id": r.get("id") or gen_id("rule"),
            "kind": kind,
            "enabled": enabled,
            "match": r.get("match") or "",
            "response": response,
            "delay_min_sec": None,
            "delay_max_sec": None,
        })

    cfg = {"owner_id": GLOBAL_OWNER, "enabled": any(r["enabled"] for r in rules),
           "delay_min_sec": lo, "delay_max_sec": hi, "rules": rules}
    _write(config.AUTO_REPLY_FILE, "configs", [cfg])
    return len(rules)


def _migrate_operators() -> int:
    rows = _read(config.OPERATORS_FILE, "operators")
    if not rows:
        return 0
    out = []
    for r in rows:
        key = r.get("key", "") or ""
        session_file = r.get("session_file", "") or ""
        # An operator may claim a session that is not on disk. Keep the
        # operator (the @username alone is enough for auto-replies) but stop
        # pretending it is logged in.
        if key:
            path = config.OPERATOR_SESSIONS_DIR / f"{key}.session"
            if not path.is_file():
                LOG.warning(f"migrate: operator {r.get('id')} claims session "
                            f"{key!r} which is not on disk; marked not logged in",
                            module="migrate")
                key, session_file = "", ""
        out.append({
            "id": r.get("id") or gen_id("op"),
            "username": r.get("username", ""),
            "display_name": r.get("display_name", ""),
            "key": key,
            "session_file": session_file,
            "telegram_id": r.get("telegram_id"),
            "notes": r.get("notes", ""),
        })
    _write(config.OPERATORS_FILE, "operators", out)
    return len(out)


def _migrate_targets() -> int:
    rows = _read(config.TARGETS_FILE, "targets")
    if not rows:
        return 0
    for r in rows:
        r.pop("advertising_allowed", None)
    _write(config.TARGETS_FILE, "targets", rows)
    return len(rows)


def _migrate_profiles() -> None:
    """last_check_status ("ONLINE" / "ERROR: ...") splits into raw_state + error."""
    for path, root in ((config.API_PROFILES_FILE, "api_profiles"),
                       (config.NETWORK_PROFILES_FILE, "network_profiles")):
        rows = _read(path, root)
        if not rows:
            continue
        for r in rows:
            legacy = r.pop("last_check_status", None) or r.pop("last_test_status", None)
            r.pop("last_test", None)
            if legacy:
                text = str(legacy)
                if text.upper().startswith("ONLINE"):
                    r["raw_state"], r["last_error"] = "ONLINE", None
                else:
                    r["raw_state"] = "ERROR"
                    r["last_error"] = text
            else:
                r.setdefault("raw_state", "UNKNOWN")
        _write(path, root, rows)


def _settings_value(dotted: str, default):
    try:
        data = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _bump_schema() -> None:
    try:
        data = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["schema_version"] = TARGET_SCHEMA
    # v1 kept these under sections that no longer exist
    for dead in ("warmup", "appearance", "health", "storage", "notifications", "scheduler"):
        data.pop(dead, None)
    tmp = config.SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, config.SETTINGS_FILE)


# ── entry point ─────────────────────────────────────────────────────────
def current_schema() -> int:
    return int(_settings_value("schema_version", 1) or 1)


def _backfill_operator_profiles() -> int:
    """Operators gained their own API profile in schema 3.

    Until then they borrowed whichever profile was default, so leaving the
    field empty would make every existing operator report "no API profile"
    for a setting they never had to make.
    """
    operators = _read(config.OPERATORS_FILE, "operators")
    if not operators:
        return 0
    profiles = _read(config.API_PROFILES_FILE, "api_profiles")
    default = next((p for p in profiles if p.get("enabled")),
                   profiles[0] if profiles else None)
    if default is None:
        return 0
    touched = 0
    for row in operators:
        if not row.get("api_profile_id"):
            row["api_profile_id"] = default.get("id")
            touched += 1
    if touched:
        _write(config.OPERATORS_FILE, "operators", operators)
        LOG.info(f"migrate: {touched} operator(s) given the default API profile",
                 module="migrate")
    return touched


def run() -> bool:
    """Migrate in place if needed. Returns True when a migration ran."""
    schema = current_schema()
    if schema >= TARGET_SCHEMA:
        return False
    if schema >= 2:
        # already on v2: only the newer steps are left
        _backfill_operator_profiles()
        _bump_schema()
        return True
    LOG.info("migrate: v1 -> v2 starting", module="migrate")
    _snapshot()

    accounts = _migrate_accounts()
    campaigns = _migrate_campaigns()
    rules = _migrate_autoreply()
    operators = _migrate_operators()
    targets = _migrate_targets()
    _migrate_profiles()

    # entities that no longer exist in the model
    _retire(config.DATA_DIR / "bindings.json")
    _retire(config.DATA_DIR / "health.json")

    _backfill_operator_profiles()
    _bump_schema()
    LOG.info(f"migrate: done - {accounts} accounts, {campaigns} campaigns, "
             f"{rules} auto-reply rules, {operators} operators, {targets} targets",
             module="migrate")
    return True
