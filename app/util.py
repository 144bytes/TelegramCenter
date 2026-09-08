"""Small shared helpers."""
from __future__ import annotations

import uuid
from datetime import datetime


def now_iso() -> str:
    """Local time, second precision, no timezone suffix."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def until_text(moment: datetime) -> str:
    """How a moment to wait until is written: «14:35» today, «27.09 14:35»
    on another day - a wait can last a week."""
    if moment.date() == datetime.now().date():
        return moment.strftime("%H:%M")
    return moment.strftime("%d.%m %H:%M")


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


