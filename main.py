"""TelegramCenter entry point.

Starts the backend and puts an icon in the tray; the interface is a browser
tab, and closing it leaves everything running. "Завершить" in the tray menu
shuts the backend down cleanly. If the tray icon cannot be created, a small
fallback window (app/panel.py) does the same job.
"""
from __future__ import annotations

import sys
import traceback
import webbrowser

from app import config
from app.logging import LOG


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
        return ControlPanel(web, storage)


def main() -> int:
    from app.core.events import EventBus
    from app.instance import SingleInstance
    from app.services import build_services
    from app.services.discovery import (discover, prune_missing_sessions,
                                        purge_pending_sessions)
    from app.storage import Storage
    from app.telegram.service import TelegramService
    from app.web import WebServer

    # 0. only one instance may run: a second backend would put two schedulers
    #    on the same sessions. Launching again simply does nothing.
    guard = SingleInstance()
    if not guard.acquire():
        return 0

    # 1. data first: everything else reads it
    storage = Storage()
    LOG.configure(
        show_time=bool(storage.settings.get("general.show_log_time", True)),
        # From here on every line also lands in the file of its day.
        directory=config.LOGS_DIR)
    LOG.info(f"log files: {config.LOGS_DIR}", module="main")

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
    # Nothing is signing in yet, so every pending_* file on disk is a
    # leftover from a login that failed or was interrupted last time.
    purge_pending_sessions()
    prune_missing_sessions(storage)
    discover(storage)

    # 5. HTTP
    web = WebServer(services, storage, telegram)
    web.start()

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
        guard.release()
        LOG.close_file()
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
