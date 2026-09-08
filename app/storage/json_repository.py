"""Plain JSON collection storage — Telesender-style.

One file per entity type: {"<root_key>": [ {...}, ... ]}.
No backups, no recovery files. If the file is missing or unreadable we start
empty (defaults); every mutation writes the file atomically.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable, Generic, TypeVar

from ..logging import LOG

M = TypeVar("M")


class JsonRepository(Generic[M]):
    def __init__(self, path: Path, model_cls: type[M], root_key: str,
                 id_field: str = "id"):
        self.path = Path(path)
        self.model_cls = model_cls
        self.root_key = root_key
        self.id_field = id_field
        self.items: list[M] = []
        self._lock = threading.RLock()
        self.load()

    # ── loading ─────────────────────────────────────────────────────────
    def load(self) -> None:
        self.items = []
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"{self.path.name} unreadable ({exc}); starting empty", module="storage")
            return
        rows = data.get(self.root_key, []) if isinstance(data, dict) else []
        for entry in rows:
            try:
                self.items.append(self.model_cls.from_dict(entry))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                LOG.warning(f"skipped malformed {self.root_key} entry: {exc}", module="storage")

    # ── writing ─────────────────────────────────────────────────────────
    def save(self) -> None:
        with self._lock:
            payload = {self.root_key: [self._to_dict(i) for i in self.items]}
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)

    @staticmethod
    def _to_dict(item) -> dict:
        return item.to_dict() if hasattr(item, "to_dict") else dict(item)

    # ── CRUD ────────────────────────────────────────────────────────────
    def all(self) -> list[M]:
        return list(self.items)

    def _key(self, item) -> str:
        return str(getattr(item, self.id_field))

    def get(self, id_: str) -> M | None:
        return next((i for i in self.items if self._key(i) == str(id_)), None)

    def find(self, pred: Callable[[M], bool]) -> M | None:
        return next((i for i in self.items if pred(i)), None)

    def filter(self, pred: Callable[[M], bool]) -> list[M]:
        return [i for i in self.items if pred(i)]

    def add(self, item: M) -> M:
        self.items.append(item)
        self.save()
        return item

    def upsert(self, item: M) -> M:
        existing = self.get(self._key(item))
        if existing is not None:
            self.items[self.items.index(existing)] = item
        else:
            self.items.append(item)
        self.save()
        return item

    def delete(self, id_: str) -> bool:
        item = self.get(id_)
        if item is None:
            return False
        self.items.remove(item)
        self.save()
        return True

    def replace_all(self, items: list[M]) -> None:
        self.items = list(items)
        self.save()

    def __len__(self) -> int:
        return len(self.items)
