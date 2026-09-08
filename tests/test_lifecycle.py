"""Startup and shutdown.

The control window's close button runs exactly this sequence, so exercising it
here is what lets us claim the shutdown path works without clicking Tk.
"""
from __future__ import annotations

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
    while len(web.bus._subs) == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert len(web.bus._subs) >= 1

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

    assert len(web.bus._subs) == 0


def test_backend_serves_before_any_tab_is_opened(services, storage, telegram, seeded):  # noqa: ARG001
    """The browser is a client, not the app: the API works with zero tabs."""
    web = WebServer(services, storage, telegram)
    web.start()
    try:
        req = urllib.request.Request(web.origin + "/api/state")
        req.add_header("X-TC-Token", web.guard.token)
        with urllib.request.urlopen(req, timeout=10) as res:
            assert res.status == 200
        assert len(web.bus._subs) == 0, "no tab is connected"
    finally:
        web.stop()


def test_a_first_start_on_an_empty_folder_just_works(app_dir, telegram):
    """The whole promise now that nothing converts anything.

    The user deletes %APPDATA%/TelegramCenter before every clean build, so
    there is no old data to read and no migration to run: an empty folder has
    to produce the current files, with the current defaults, and the app has
    to come up on them.
    """
    from app import config
    from app.core.events import EventBus
    from app.services import build_services
    from app.storage import Storage

    assert not config.CAMPAIGNS_FILE.exists(), "nothing is there to begin with"

    storage = Storage()
    services = build_services(storage, telegram, EventBus())
    snapshot = services.state.snapshot()

    assert config.SETTINGS_FILE.is_file(), "written on the first read"
    assert storage.settings.get("accounts.connect_spread_sec") == 60
    assert storage.accounts.all() == []
    assert snapshot["accounts"] == [] and snapshot["campaigns"] == []
