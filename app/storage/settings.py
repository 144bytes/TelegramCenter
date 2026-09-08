"""Single settings.json with dotted-path access and immediate autosave."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .. import config
from ..logging import LOG
from ..spamphrases import (DEFAULT_BLOCKED, DEFAULT_CLEAN,
                           DEFAULT_LIMITED)

DEFAULTS: dict = {
    "general": {
        "language": "ru",         # ru | en
        "show_log_time": True,
        "open_browser_on_start": False,
    },
    "autoreply": {
        "faq_limit": 5,           # slider 0..10
        # The delay a new auto-reply starts with; each auto-reply page then
        # has its own «от … до» for the whole set of rules.
        "default_delay_min_sec": 180,
        "default_delay_max_sec": 300,
    },
    "spamcheck": {
        # off by default: it sends a message from every account, and that is
        # not something to start doing without being asked
        "enabled": False,
        "bot": "@SpamBot",
        # Operators answer people rather than send campaigns, so messaging
        # the bot from every operator is traffic for nothing. Opt in.
        "include_operators": False,
        "delay_sec": 5,
        "timeout_sec": 25,
        # Editable so a different bot - or new wording from this one - can be
        # taught without a new build.
        "clean_phrases": list(DEFAULT_CLEAN),
        "limited_phrases": list(DEFAULT_LIMITED),
        # A block is not a strong limit: it never runs out, so it needs its
        # own list. Folding the two together painted a permanent block yellow
        # and left the account sending.
        "blocked_phrases": list(DEFAULT_BLOCKED),
    },
    "campaign": {
        # «Интервал по умолчанию»: the pause after every channel of a new
        # campaign.
        "default_interval_min_sec": 15,
        "default_interval_max_sec": 40,
        # «Задержка старта»: each started campaign starts a random 0..this
        # seconds after the one started before it.
        "start_delay_sec": 60,
        # Subscribe before writing. Off by default: joining a chat is a
        # visible act by the user's account.
        "auto_join": False,
        # How long to wait between joining and writing. Joining and posting
        # in the same second is what an anti-spam system notices first.
        "auto_join_delay_sec": 10,
        # How many refusals in a row before the chat is switched off inside
        # the campaign - and left, if the app joined it. Twenty-seven «здесь
        # нельзя писать» in the field came from a handful of chats on a loop.
        # 0 never switches anything off.
        "refusals_before_off": 3,
        # Leave a reaction now and then, on whatever is newest. An account
        # that only ever posts is a shape worth not having. Broadcast
        # accounts only - never on an operator's behalf.
        "reactions": False,
        "reaction_percent": 30,
        # Insurance for when nobody is watching: at least `error_stop_min`
        # attempts in the last hour and `error_stop_percent` of them failed
        # switches the account off. 139 of 396 failed in the field and it ran
        # for hours. Zero in either field switches the guard off.
        "error_stop_percent": 30,
        "error_stop_min": 5,
    },
    "accounts": {
        "dialog_limit": 30,
        "message_limit": 30,
        # An account added this recently is warned about while it sends.
        # Telegram has no registration date, so this counts from the sign-in
        # here - the same week for a freshly bought account.
        "young_days": 7,
        # How often the background check touches every account. A range, not
        # one number: a fixed interval is a heartbeat, and a heartbeat is
        # easy to recognise.
        "probe_interval_min_sec": 900,
        "probe_interval_max_sec": 1800,
        # How far apart accounts reach Telegram when several are about to.
        # Each waits a random moment inside [0, this]. Zero - all at once.
        "connect_spread_sec": 60,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """`base` with the values of `override` for the keys `base` knows.

    A key DEFAULTS no longer has is dropped, not carried on for ever.
    """
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k not in out:
            continue
        if isinstance(out[k], dict):
            if isinstance(v, dict):
                out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def settable_keys() -> frozenset[str]:
    """Every key a user is allowed to write, derived from DEFAULTS.

    The API used to keep a hand-written copy, which fell behind the moment
    a setting was added - and the symptom was the interface refusing a
    control it shows.
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

    def number(self, path: str, low: int = 0, high: int | None = None) -> int:
        """A whole-number setting, kept inside [low, high].

        A hand-edited value that is not a number falls back to the one in
        DEFAULTS: it must never be able to stop anything.
        """
        try:
            value = int(self.get(path))
        except (TypeError, ValueError):
            value = int(self._default(path))
        value = max(low, value)
        return value if high is None else min(high, value)

    @staticmethod
    def _default(path: str):
        node = DEFAULTS
        for part in path.split("."):
            node = node[part]
        return node

    def range(self, low_path: str, high_path: str, floor: int = 0) -> tuple[int, int]:
        """Two settings that make a from-to pair, the right way round."""
        lo, hi = self.number(low_path, floor), self.number(high_path, floor)
        return (lo, hi) if lo <= hi else (hi, lo)

    def faq_limit(self) -> int:
        return self.number("autoreply.faq_limit", high=10)

    def probe_interval_range(self) -> tuple[int, int]:
        """Seconds between two background checks: never below a minute."""
        return self.range("accounts.probe_interval_min_sec",
                          "accounts.probe_interval_max_sec", floor=60)

    def default_delay_range(self) -> tuple[int, int]:
        return self.range("autoreply.default_delay_min_sec",
                          "autoreply.default_delay_max_sec")
