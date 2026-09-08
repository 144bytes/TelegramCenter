from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id, now_iso
from .base import Model


@dataclass
class Template(Model):
    id: str = field(default_factory=lambda: gen_id("tpl"))
    name: str = ""
    text: str = ""
    media: str | None = None
    enabled: bool = True
    created_at: str = field(default_factory=now_iso)
