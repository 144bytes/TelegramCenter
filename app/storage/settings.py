"""Single settings.json with dotted-path access and immediate autosave."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .. import config
from ..logging import LOG
from ..spamphrases import DEFAULT_CLEAN, DEFAULT_LIMITED

DEFAULTS: dict = {
    "schema_version": 1,          # bumped to 2 by storage.migrate
    "general": {
        "language": "ru",         # ru | en
        "show_log_time": True,
        "open_browser_on_start": False,
    },
    "autoreply": {
        "faq_limit": 5,           # slider 0..10
        "default_delay_min_sec": 300,
        "default_delay_max_sec": 600,
    },
    "spamcheck": {
        # off by default: it sends a message from every account, and that is
        # not something to start doing without being asked
        "enabled": False,
        "bot": "@SpamBot",
        # Operators answer people rather than send campaigns, so the limit
        # matters less to them and messaging the bot from every operator is
        # traffic for nothing. Opt in.
        "include_operators": False,
        "delay_sec": 5,
        "timeout_sec": 25,
        # Editable so a different bot - or new wording from this one - can be
        # taught without a new build.
        "clean_phrases": list(DEFAULT_CLEAN),
        "limited_phrases": list(DEFAULT_LIMITED),
    },
    "campaign": {
        "default_gap_min_sec": 15,
        "default_gap_max_sec": 40,
    },
    "accounts": {
        "dialog_limit": 30,
        "message_limit": 30,
        "probe_interval_sec": 300,
    },
    "operators": {
        "after_hours": "20:00-09:00",
    },
    "dev": {
        "test_mode": False,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def settable_keys() -> frozenset[str]:
    """Every key a user is allowed to write, derived from DEFAULTS.

    The API used to keep its own hand-written copy of this list. It fell
    behind the moment a setting was added, and the only symptom was the
    interface refusing a control that plainly exists - so the list is computed
    from the one place that defines the settings instead.
    """
    return frozenset(f"{section}.{name}"
                     for section, values in DEFAULTS.items()
                     if isinstance(values, dict)
                     for name in values)

class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.SETTINGS_FILE)
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if self.path.is_file():
            try:
                disk = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = _deep_merge(DEFAULTS, disk)
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"settings.json unreadable ({exc}); using defaults",
                          module="storage")
                self.data = copy.deepcopy(DEFAULTS)
        else:
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # dotted paths: get("autoreply.faq_limit")
    def get(self, path: str, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value) -> None:
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self.save()

    def update(self, values: dict) -> None:
        """Bulk set from a flat {dotted.path: value} mapping, one save."""
        for path, value in values.items():
            parts = path.split(".")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        self.save()

    def section(self, name: str) -> dict:
        return dict(self.data.get(name, {}))

    def faq_limit(self) -> int:
        return max(0, min(10, int(self.get("autoreply.faq_limit", 5))))

    def default_delay_range(self) -> tuple[int, int]:
        lo = int(self.get("autoreply.default_delay_min_sec", 300))
        hi = int(self.get("autoreply.default_delay_max_sec", 600))
        return (lo, hi) if lo <= hi else (hi, lo)
