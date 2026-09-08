"""Small shared helpers."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    """Local time, second precision, no timezone suffix (matches Telesender)."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None


def gen_id(prefix: str = "") -> str:
    token = uuid.uuid4().hex[:12]
    return f"{prefix}_{token}" if prefix else token


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def human_timedelta_days(dt: datetime | None) -> int:
    """Whole days between `dt` and now (>= 0)."""
    if dt is None:
        return 0
    delta = datetime.now() - dt
    return max(0, delta.days)
