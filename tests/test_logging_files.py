"""Log lines on disk, and the tag that lets them be filtered.

The in-memory buffer holds two thousand lines. A campaign that misbehaved
overnight is long gone from it by morning, which is precisely when someone
wants to read about it.
"""
from __future__ import annotations

import time

from app.logging import KEEP_FILES, TIME_FORMAT, FileSink, LogBus, LogRecord


def test_a_line_reaches_the_file(tmp_path):
    bus = LogBus()
    bus.configure(directory=tmp_path)

    bus.info("campaign started", module="campaigns")

    written = (tmp_path / f"{time.strftime('%Y-%m-%d')}.log").read_text(
        encoding="utf-8")
    assert "campaign started" in written
    assert "[campaigns]" in written, "greppable by process"
    bus.close_file()


def test_the_file_always_carries_the_time_whatever_the_screen_shows():
    """The screen setting is about the screen. A file whose lines sometimes
    carry a time and sometimes do not cannot be sorted or grepped."""
    record = LogRecord(ts="2026-09-23T-10:00:00", level="INFO",
                       message="hello", module="campaigns")

    assert record.format_line(show_time=False).startswith("INFO")
    assert record.file_line().startswith("2026-09-23T-10:00:00")


def test_a_line_with_no_module_still_has_a_tag():
    record = LogRecord(ts="t", level="INFO", message="hello")
    assert "[-]" in record.file_line()


def test_a_broken_disk_loses_the_line_and_nothing_else(tmp_path):
    bus = LogBus()
    bus.configure(directory=tmp_path)

    class Broken:
        def write(self, record):
            raise OSError("disk full")

    bus._sink = Broken()
    bus.info("still logged", module="campaigns")   # must not raise

    assert bus.snapshot()[-1].message == "still logged"


def _record(ts):
    return LogRecord(ts=ts, level="INFO", message="hello", module="m")


def test_exactly_fourteen_files_stay_the_current_one_included(tmp_path):
    for n in range(1, 21):
        (tmp_path / f"2026-08-{n:02d}.log").write_text("old")
    sink = FileSink(tmp_path)

    sink.write(_record("2026-09-25T-22:33:06"))

    kept = sorted(p.name for p in tmp_path.glob("*.log"))
    assert len(kept) == KEEP_FILES == 14
    assert kept[-1] == "2026-09-25.log"
    assert kept[0] == "2026-08-08.log", "the oldest went"
    sink.close()


def test_a_line_goes_to_the_file_of_its_own_time(tmp_path):
    """A line stamped 23:59:59 belongs to that day, whenever it is written."""
    sink = FileSink(tmp_path)

    sink.write(_record("2026-09-25T-23:59:59"))
    sink.write(_record("2026-09-26T-00:00:01"))

    assert "23:59:59" in (tmp_path / "2026-09-25.log").read_text(encoding="utf-8")
    assert "00:00:01" in (tmp_path / "2026-09-26.log").read_text(encoding="utf-8")
    sink.close()


def test_the_time_is_written_with_a_dash_after_the_t():
    bus = LogBus()
    bus.info("hello")

    ts = bus.snapshot()[-1].ts
    assert ts[10:12] == "T-"
    assert time.strptime(ts, TIME_FORMAT)


def test_the_api_hands_the_module_to_the_page(services, storage):
    """The filter on the Logs page needs this field and nothing else."""
    from app.logging import LOG
    from app.web.api import Ctx, get_logs

    LOG.info("one line", module="autoreply")
    rows = get_logs(Ctx(services, storage, lambda c, timeout=0: None), {},
                    {"limit": "50"})

    assert any(r["module"] == "autoreply" for r in rows)
