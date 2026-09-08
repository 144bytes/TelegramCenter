"""Structured, thread-safe log bus.

Any thread calls LOG.log(...). A bounded in-memory buffer feeds the Logs
page; listeners (the live event stream) get every line as it happens.

The buffer holds two thousand lines, which is no use for working out what
happened overnight. So every line is also appended to a file, one per
calendar day, in %APPDATA%/TelegramCenter/logs. The day a line goes to is
the day in its own timestamp, and the folder keeps the current file and
the thirteen before it.

Writing to the file must never break anything: a full disk, a locked file
or a bad codec loses the line and nothing else.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# How a line's time is written, on the screen and in the files alike. The
# dash after the T keeps the time from running into the date.
TIME_FORMAT = "%Y-%m-%dT-%H:%M:%S"

# Daily files kept, the current one included.
KEEP_FILES = 14


@dataclass
class LogRecord:
    ts: str
    level: str
    message: str
    account: str | None = None
    module: str | None = None

    @property
    def day(self) -> str:
        """The calendar day of this line, "YYYY-MM-DD" - its file's name."""
        return self.ts[:10]

    def format_line(self, show_time: bool = True) -> str:
        head = f"{self.ts} | " if show_time else ""
        tag = f"[{self.module}] " if self.module else ""
        acc = f"({self.account}) " if self.account else ""
        return f"{head}{self.level:<7} {tag}{acc}{self.message}"

    def file_line(self) -> str:
        """The form written to disk: always timestamped, always tagged, so
        every line of every file can be sorted and grepped."""
        return (f"{self.ts} | {self.level:<7} | [{self.module or '-'}] "
                f"{f'({self.account}) ' if self.account else ''}{self.message}")


class FileSink:
    """Appends every line to the file of its day, and forgets old files.

    The handle is kept open and swapped when a line of a new day arrives,
    which is also when files past the keep window are deleted.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._day: str | None = None
        self._handle = None
        # Every thread logs; two rotating at once would leave one writing to
        # a handle the other had closed.
        self._lock = threading.Lock()

    def _rotate(self, day: str) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._handle = None
        self.directory.mkdir(parents=True, exist_ok=True)
        self._handle = (self.directory / f"{day}.log").open(
            "a", encoding="utf-8", errors="replace")
        self._day = day
        self._sweep()

    def _sweep(self) -> None:
        files = sorted(self.directory.glob("*.log"))
        for path in files[:-KEEP_FILES]:
            try:
                path.unlink()
            except OSError:
                pass

    def write(self, record: LogRecord) -> None:
        with self._lock:
            if record.day != self._day:
                self._rotate(record.day)
            self._handle.write(record.file_line() + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                except Exception:  # noqa: BLE001
                    pass
                self._handle = None
                self._day = None


@dataclass
class LogBus:
    buffer: deque = field(default_factory=lambda: deque(maxlen=2000))
    show_time: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _listeners: list = field(default_factory=list)
    _sink: FileSink | None = None

    def configure(self, *, show_time: bool = True,
                  directory: Path | None = None) -> None:
        self.show_time = bool(show_time)
        if directory is not None:
            self._sink = FileSink(directory)

    def close_file(self) -> None:
        sink, self._sink = self._sink, None
        if sink is not None:
            sink.close()

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def log(self, message: str, level: str = "INFO", *,
            account: str | None = None, module: str | None = None) -> None:
        rec = LogRecord(
            ts=time.strftime(TIME_FORMAT),
            level=level if level in LEVELS else "INFO",
            message=str(message), account=account, module=module,
        )
        with self._lock:
            self.buffer.append(rec)
            sink = self._sink
        try:
            line = rec.format_line(self.show_time)
            import sys
            if sys.stdout is not None:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
        except Exception:  # noqa: BLE001 - no console / bad codec must never break logging
            pass
        if sink is not None:
            try:
                sink.write(rec)
            except Exception:  # noqa: BLE001 - a full or locked disk loses the
                pass           # line, never the run that was writing it
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
