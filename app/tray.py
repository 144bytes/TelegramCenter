"""Windows tray icon.

Replaces the old control window: the app lives in the notification area and
offers exactly two actions - open the interface in a browser tab, and shut the
backend down. The wording is deliberately asymmetric ("Открыть" / "Завершить",
"Open" / "Shut Down") so that closing the *tab* and stopping the *app* can
never be mistaken for one another.

Built straight on Shell_NotifyIcon through ctypes. pystray would pull in
Pillow - several megabytes of image library for a two-item menu - and the spec
asks for one small exe. Here the app icon.ico is handed to Windows as-is.

Everything below runs on the thread that calls run(), because a window and its
message loop must live on the same thread.
"""
from __future__ import annotations

import ctypes
import webbrowser
from ctypes import wintypes

from . import config
from .logging import LOG

MOD = "tray"

LABELS = {
    "ru": {"open": "Открыть", "quit": "Завершить"},
    "en": {"open": "Open", "quit": "Shut Down"},
}


def labels_for(language: str | None) -> dict[str, str]:
    """Menu wording for the language chosen in settings, Russian by default."""
    return LABELS.get((language or "ru").lower(), LABELS["ru"])


# ── Win32 plumbing ──────────────────────────────────────────────────────
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = wintypes.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1

NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512
SM_CXSMICON, SM_CYSMICON = 49, 50

MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

ID_OPEN, ID_QUIT = 1001, 1002
ERROR_CLASS_ALREADY_EXISTS = 1410


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _declare() -> None:
    """Handles are 64-bit; without explicit restypes ctypes truncates them."""
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                  wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT]
    user32.LoadIconW.restype = wintypes.HICON
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.LPVOID]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [wintypes.LPMSG, wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                          ctypes.POINTER(NOTIFYICONDATAW)]


_declare()


# ── the tray icon ───────────────────────────────────────────────────────
class TrayIcon:
    """Notification-area icon owning the main thread until the user quits."""

    CLASS_NAME = "TelegramCenterTray"

    def __init__(self, web, storage):
        self.web = web
        self.storage = storage
        self._hwnd = None
        self._hicon = None
        self._nid = None
        self._added = False
        # Windows keeps a raw pointer to the callback: hold a reference here or
        # the interpreter collects it and the process dies on the first click.
        self._wndproc = WNDPROC(self._on_message)
        self._wndclass = None
        self._registered = False
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._hinst = kernel32.GetModuleHandleW(None)

    # -- language ---------------------------------------------------------
    def _labels(self) -> dict[str, str]:
        # read on every menu open, so switching language in the UI applies at once
        return labels_for(self.storage.settings.get("general.language", "ru"))

    # -- setup ------------------------------------------------------------
    def _load_icon(self):
        path = config.get_resource_path("icon.ico")
        cx = user32.GetSystemMetrics(SM_CXSMICON)
        cy = user32.GetSystemMetrics(SM_CYSMICON)
        handle = user32.LoadImageW(None, str(path), IMAGE_ICON, cx, cy,
                                   LR_LOADFROMFILE)
        if not handle:
            handle = user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not handle:
            LOG.warning(f"could not load {path}; falling back to the system icon",
                        module=MOD)
            handle = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        return handle

    def _create_window(self) -> None:
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._hinst
        wc.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(err)
            # An earlier instance in this process left the class registered,
            # and it still points at that instance's callback. Once that object
            # is collected the pointer dangles and the next click crashes the
            # process, so replace the registration rather than reusing it.
            user32.UnregisterClassW(self.CLASS_NAME, self._hinst)
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
        self._wndclass = wc            # keep alive alongside the callback
        self._registered = True

        # A real (never shown) top-level window rather than a message-only one:
        # message-only windows do not receive the TaskbarCreated broadcast, so
        # the icon would disappear for good if Explorer ever restarted.
        self._hwnd = user32.CreateWindowExW(
            0, self.CLASS_NAME, config.APP_TITLE, 0, 0, 0, 0, 0,
            None, None, self._hinst, None)
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

    def _build_nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = f"{config.APP_TITLE} - {self.web.origin}"[:127]
        return nid

    def _add_icon(self) -> None:
        self._nid = self._build_nid()
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._added = True

    def _remove_icon(self) -> None:
        if self._added and self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._added = False

    # -- actions ----------------------------------------------------------
    def open_browser(self) -> None:
        webbrowser.open(self.web.url)
        LOG.info("interface opened in the browser", module=MOD)

    def quit(self) -> None:
        LOG.info("shutdown requested from the tray", module=MOD)
        user32.PostQuitMessage(0)

    def stop(self) -> None:
        """Ask the message loop to exit from another thread."""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    # -- menu -------------------------------------------------------------
    def _show_menu(self) -> None:
        labels = self._labels()
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, ID_OPEN, labels["open"])
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, ID_QUIT, labels["quit"])

            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            # required, or the menu refuses to close when clicked away from
            user32.SetForegroundWindow(self._hwnd)
            choice = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                point.x, point.y, 0, self._hwnd, None)
            # the documented workaround for a menu that lingers afterwards
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)

        self.activate(choice)

    def activate(self, command: int) -> None:
        """Run a menu command. Separate from the menu so it can be tested."""
        if command == ID_OPEN:
            self.open_browser()
        elif command == ID_QUIT:
            self.quit()

    # -- window procedure -------------------------------------------------
    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == WM_LBUTTONDBLCLK:
                self.open_browser()
            elif event in (WM_RBUTTONUP, WM_LBUTTONUP):
                self._show_menu()
            return 0
        if msg == self._taskbar_created and self._added:
            # Explorer restarted and forgot every icon; put ours back
            self._nid = self._build_nid()
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- loop -------------------------------------------------------------
    def run(self) -> None:
        self._hicon = self._load_icon()
        self._create_window()
        self._add_icon()
        LOG.info(f"tray icon ready - {self.web.origin}", module=MOD)
        try:
            msg = wintypes.MSG()
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._remove_icon()
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            if self._registered:
                # must follow DestroyWindow: a class with live windows cannot
                # be unregistered, and leaving it behind strands the callback
                user32.UnregisterClassW(self.CLASS_NAME, self._hinst)
                self._registered = False
            if self._hicon:
                user32.DestroyIcon(self._hicon)
                self._hicon = None
