"""TelegramCenter entry point.

Starts the backend, then shows a small control window ("пульт"). The browser
tab is just a client: closing it leaves everything running. Closing the control
window shuts the backend down cleanly.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import webbrowser

from app import config
from app.logging import LOG


def _write_lock(url: str, port: int) -> None:
    try:
        config.LOCK_FILE.write_text(json.dumps(
            {"pid": os.getpid(), "port": port, "url": url}, indent=2), encoding="utf-8")
    except OSError as exc:
        LOG.warning(f"could not write lock file: {exc}", module="main")


def _clear_lock() -> None:
    try:
        config.LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _build_shell(web, storage, services):
    """The tray icon, or the old control window if the tray cannot be created.

    Shell_NotifyIcon can fail (a broken shell, Explorer not running). Falling
    back keeps a way to open the interface and stop the app instead of leaving
    a process with no visible controls.
    """
    from app.tray import TrayIcon
    try:
        return TrayIcon(web, storage)
    except Exception as exc:  # noqa: BLE001
        LOG.warning(f"tray unavailable ({exc}); using the control window",
                    module="main")
        from app.panel import ControlPanel
        return ControlPanel(web, services)


def main() -> int:
    from app.core.events import EventBus
    from app.instance import SingleInstance
    from app.services import build_services
    from app.services.discovery import discover, prune_missing_sessions
    from app.storage import Storage, migrate
    from app.telegram.service import TelegramService
    from app.web import WebServer

    # 0. only one instance may run: a second backend would put two schedulers
    #    on the same sessions. Launching again simply does nothing.
    guard = SingleInstance()
    if not guard.acquire():
        return 0

    # 1. data first: migrate before anything reads the files
    migrate.run()
    storage = Storage()
    LOG.configure(show_time=bool(storage.settings.get("general.show_log_time", True)))

    # 2. Telegram loop
    telegram = TelegramService()
    telegram.start()

    # 3. services
    bus = EventBus()
    services = build_services(storage, telegram, bus)

    # stream log lines to the browser over SSE
    LOG.add_listener(lambda rec: bus.publish(
        "log", ts=rec.ts, level=rec.level, message=rec.message, module=rec.module))

    # 4. pick up session files that appeared on disk, drop stale claims
    prune_missing_sessions(storage)
    discover(storage)

    # 5. HTTP
    web = WebServer(services, storage, telegram)
    web.start()
    _write_lock(web.url, web.port)

    # 6. background work
    services.start_background()

    if storage.settings.get("general.open_browser_on_start", False):
        webbrowser.open(web.url)

    # 7. the tray icon owns the main thread until the user picks "Завершить"
    shell = _build_shell(web, storage, services)
    try:
        shell.run()
    finally:
        LOG.info("shutting down", module="main")
        web.stop()
        services.shutdown()
        telegram.shutdown()
        _clear_lock()
        guard.release()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc()
        try:
            config.LAST_ERROR_FILE.write_text(detail, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        try:
            if sys.stderr is not None:
                sys.stderr.write(detail)
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
