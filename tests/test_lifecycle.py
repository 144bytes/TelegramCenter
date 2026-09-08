"""Startup and shutdown.

The control window's close button runs exactly this sequence, so exercising it
here is what lets us claim the shutdown path works without clicking Tk.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from app.web import WebServer


def _live_threads() -> set[str]:
    return {t.name for t in threading.enumerate() if t.is_alive()}


def test_server_stops_and_releases_its_thread(services, storage, telegram, seeded):  # noqa: ARG001
    web = WebServer(services, storage, telegram)
    web.start()
    assert "http" in _live_threads()

    web.stop()
    time.sleep(0.3)

    assert "http" not in _live_threads()
    # and the port is genuinely closed
    try:
        urllib.request.urlopen(web.origin + "/api/state", timeout=2)
        connected = True
    except (urllib.error.URLError, OSError):
        connected = False
    assert connected is False


def test_shutdown_releases_sse_subscribers(services, storage, telegram, seeded):  # noqa: ARG001
    web = WebServer(services, storage, telegram)
    web.start()
    finished = threading.Event()

    def listen():
        req = urllib.request.Request(web.origin + "/api/events")
        req.add_header("X-TC-Token", web.guard.token)
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                for _ in res:
                    pass
        except Exception:  # noqa: BLE001
            pass
        finished.set()

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while web.bus.subscriber_count == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert web.bus.subscriber_count >= 1

    web.stop()

    # a hanging stream must not keep the process alive
    assert finished.wait(timeout=6), "the SSE reader was never released"


def test_full_shutdown_sequence_is_clean(services, storage, telegram, seeded):  # noqa: ARG001
    """The exact order main.py uses when the control window is closed."""
    web = WebServer(services, storage, telegram)
    web.start()

    web.stop()
    services.shutdown()
    telegram.shutdown()

    assert web.bus.subscriber_count == 0


def test_lock_file_round_trip(app_dir):
    from app import config
    import main

    main._write_lock("http://127.0.0.1:5000/?t=abc", 5000)
    payload = json.loads(config.LOCK_FILE.read_text(encoding="utf-8"))
    assert payload["port"] == 5000
    assert payload["url"].endswith("t=abc")
    assert isinstance(payload["pid"], int)

    main._clear_lock()
    assert not config.LOCK_FILE.exists()
    # clearing a lock that is already gone must not raise
    main._clear_lock()


def test_backend_serves_before_any_tab_is_opened(services, storage, telegram, seeded):  # noqa: ARG001
    """The browser is a client, not the app: the API works with zero tabs."""
    web = WebServer(services, storage, telegram)
    web.start()
    try:
        req = urllib.request.Request(web.origin + "/api/state")
        req.add_header("X-TC-Token", web.guard.token)
        with urllib.request.urlopen(req, timeout=10) as res:
            assert res.status == 200
        assert web.bus.subscriber_count == 0, "no tab is connected"
    finally:
        web.stop()
