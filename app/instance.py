"""Single-instance guard.

Only one TelegramCenter may run at a time. Launching the exe again must do
nothing rather than start a second backend on a second port, which would leave
two schedulers sending from the same sessions.

A named mutex is used rather than the lock file, because the mutex is released
by Windows when the process ends - including when it is killed or crashes - so
there is no stale lock to clean up. The lock file stays, but only as a place to
read the running instance's URL from.
"""
from __future__ import annotations

import sys

from .logging import LOG

MOD = "instance"
MUTEX_NAME = "TelegramCenter_SingleInstance_9f2a"
ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """Holds the mutex for the lifetime of the process."""

    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self._handle = None
        self._kernel32 = None

    def acquire(self) -> bool:
        """True if this process may run; False if another instance holds it."""
        if sys.platform != "win32":       # nothing to guard against elsewhere
            return True

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        self._kernel32 = kernel32

        # Global\ spans every logged-in user; it needs a privilege that some
        # accounts lack, so fall back to the per-session namespace.
        for prefix in ("Global\\", "Local\\"):
            handle = kernel32.CreateMutexW(None, False, prefix + self.name)
            err = ctypes.get_last_error()
            if not handle:
                continue
            if err == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                LOG.info("another instance is already running; exiting",
                         module=MOD)
                return False
            self._handle = handle
            return True

        # Could not create the mutex at all. Refusing to start over a guard we
        # cannot evaluate would be worse than starting, so let the app run.
        LOG.warning("could not create the single-instance mutex; "
                    "the guard is not active", module=MOD)
        return True

    def release(self) -> None:
        if self._handle and self._kernel32 is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
