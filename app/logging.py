"""Structured, thread-safe log bus.

Any thread calls LOG.log(...); the UI drains LOG.queue on a Tk timer. A bounded
in-memory ring buffer feeds the Logs page and is flushed to logs.json lazily.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass
class LogRecord:
    ts: str
    level: str
    message: str
    account: str | None = None
    module: str | None = None

    def format_line(self, show_time: bool = True) -> str:
        head = f"{self.ts} | " if show_time else ""
        tag = f"[{self.module}] " if self.module else ""
        acc = f"({self.account}) " if self.account else ""
        return f"{head}{self.level:<7} {tag}{acc}{self.message}"


@dataclass
class LogBus:
    queue: "queue.Queue[LogRecord]" = field(default_factory=queue.Queue)
    buffer: deque = field(default_factory=lambda: deque(maxlen=2000))
    show_time: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _listeners: list = field(default_factory=list)

    def configure(self, *, show_time: bool = True) -> None:
        self.show_time = bool(show_time)

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def log(self, message: str, level: str = "INFO", *,
            account: str | None = None, module: str | None = None) -> None:
        rec = LogRecord(
            ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
            level=level if level in LEVELS else "INFO",
            message=str(message), account=account, module=module,
        )
        with self._lock:
            self.buffer.append(rec)
        try:
            line = rec.format_line(self.show_time)
            import sys
            if sys.stdout is not None:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
        except Exception:  # noqa: BLE001 - no console / bad codec must never break logging
            pass
        self.queue.put(rec)
        for fn in list(self._listeners):
            try:
                fn(rec)
            except Exception:  # noqa: BLE001 - a broken listener must not kill logging
                pass

    def info(self, msg, **kw):
        self.log(msg, "INFO", **kw)

    def warning(self, msg, **kw):
        self.log(msg, "WARNING", **kw)

    def error(self, msg, **kw):
        self.log(msg, "ERROR", **kw)

    def debug(self, msg, **kw):
        self.log(msg, "DEBUG", **kw)

    def snapshot(self) -> list[LogRecord]:
        with self._lock:
            return list(self.buffer)


LOG = LogBus()
