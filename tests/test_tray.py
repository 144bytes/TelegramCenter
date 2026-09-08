"""The tray icon.

The menu itself cannot be clicked from a test, so the parts are exercised
separately: the label lookup, the real Shell_NotifyIcon registration, and the
command handlers the menu dispatches to.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="the tray is a Windows shell feature")

from app import config           # noqa: E402
from app.tray import ID_OPEN, ID_QUIT, TrayIcon, labels_for   # noqa: E402
from app.web import WebServer    # noqa: E402


# ── wording ─────────────────────────────────────────────────────────────
def test_russian_labels():
    labels = labels_for("ru")
    assert labels["open"] == "Открыть"
    assert labels["quit"] == "Завершить"


def test_english_labels():
    labels = labels_for("en")
    assert labels["open"] == "Open"
    assert labels["quit"] == "Shut Down"


def test_unknown_language_falls_back_to_russian():
    assert labels_for(None) == labels_for("ru")
    assert labels_for("de") == labels_for("ru")
    assert labels_for("RU") == labels_for("ru")


def test_open_and_quit_never_share_wording():
    """The user asked for words that cannot be confused with one another."""
    for lang in ("ru", "en"):
        labels = labels_for(lang)
        assert labels["open"] != labels["quit"]
        assert labels["quit"].lower() not in labels["open"].lower()


def test_menu_language_follows_settings(storage, seeded):  # noqa: ARG001
    tray = TrayIcon(_FakeWeb(), storage)

    storage.settings.set("general.language", "en")
    assert tray._labels()["quit"] == "Shut Down"

    storage.settings.set("general.language", "ru")
    assert tray._labels()["quit"] == "Завершить"


# ── icon ────────────────────────────────────────────────────────────────
def test_icon_file_is_bundled_and_loadable(storage):
    path = config.get_resource_path("icon.ico")
    assert path.is_file(), f"{path} is missing"

    tray = TrayIcon(_FakeWeb(), storage)
    handle = tray._load_icon()
    assert handle, "icon.ico could not be loaded as an HICON"

    from app.tray import user32
    user32.DestroyIcon(handle)


def test_spec_bundles_the_icon():
    spec = (config.get_base_dir() / "TelegramCenter.spec").read_text(encoding="utf-8")
    assert '("icon.ico", ".")' in spec


# ── live registration ───────────────────────────────────────────────────
class _FakeWeb:
    origin = "http://127.0.0.1:65000"
    url = "http://127.0.0.1:65000/?t=token"


def test_icon_registers_and_unregisters(storage, seeded):  # noqa: ARG001
    """Actually put an icon in the notification area and take it away again."""
    tray = TrayIcon(_FakeWeb(), storage)
    started = threading.Event()
    failure: list[BaseException] = []

    original_add = tray._add_icon

    def add_and_signal():
        original_add()
        started.set()

    tray._add_icon = add_and_signal

    def loop():
        try:
            tray.run()
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)
            started.set()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    assert started.wait(timeout=10), "the tray icon never came up"
    assert not failure, f"tray failed: {failure[0]!r}"
    assert tray._added is True
    assert tray._hwnd, "no window was created"

    tray.stop()
    thread.join(timeout=10)

    assert not thread.is_alive(), "the message loop did not exit"
    assert tray._added is False, "the icon was left in the tray"
    assert tray._hwnd is None


def test_activate_open_launches_the_browser(storage, monkeypatch, seeded):  # noqa: ARG001
    opened: list[str] = []
    monkeypatch.setattr("app.tray.webbrowser.open", opened.append)

    tray = TrayIcon(_FakeWeb(), storage)
    tray.activate(ID_OPEN)

    assert opened == [_FakeWeb.url], "Открыть must open the tab with its token"


def test_activate_quit_posts_a_quit_message(storage, seeded):  # noqa: ARG001
    """Picking "Завершить" / "Shut Down" must end the message loop, which is
    what lets main.py run its shutdown sequence."""
    import ctypes
    from ctypes import wintypes
    from app.tray import user32

    tray = TrayIcon(_FakeWeb(), storage)
    tray.activate(ID_QUIT)

    WM_QUIT, PM_REMOVE = 0x0012, 0x0001
    msg = wintypes.MSG()
    got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE)

    assert got, "no message was queued for this thread"
    assert msg.message == WM_QUIT


def test_a_second_tray_can_be_created_in_the_same_process(storage, seeded):  # noqa: ARG001
    """The window class must not outlive its callback: a stale registration
    pointing at a collected WNDPROC crashes the process on the next click."""
    for _ in range(2):
        tray = TrayIcon(_FakeWeb(), storage)
        ready = threading.Event()
        original_add = tray._add_icon

        def add_and_signal(_add=original_add, _flag=ready):
            _add()
            _flag.set()

        tray._add_icon = add_and_signal
        thread = threading.Thread(target=tray.run, daemon=True)
        thread.start()
        assert ready.wait(timeout=10), "the tray icon never came up"

        tray.stop()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert tray._registered is False, "the window class was left behind"


def test_open_does_not_stop_the_backend(services, storage, telegram, monkeypatch,
                                        seeded):  # noqa: ARG001
    """Opening a tab, or closing one, must never touch the server."""
    monkeypatch.setattr("app.tray.webbrowser.open", lambda _url: None)
    web = WebServer(services, storage, telegram)
    web.start()
    try:
        tray = TrayIcon(web, storage)
        tray.activate(ID_OPEN)
        time.sleep(0.2)

        import urllib.request
        req = urllib.request.Request(web.origin + "/api/state")
        req.add_header("X-TC-Token", web.guard.token)
        with urllib.request.urlopen(req, timeout=10) as res:
            assert res.status == 200
    finally:
        web.stop()


def test_quit_command_id_is_distinct():
    assert ID_OPEN != ID_QUIT
