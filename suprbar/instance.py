"""Single-instance guard (Windows named mutex; a no-op on other platforms).

Lives apart from popup.py so the tray process can use it without importing
pywebview — only the window child processes load WebView2.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes

log = logging.getLogger("suprbar.instance")

_mutex_handle: int | None = None


def acquire_single_instance() -> bool:
    """Try to acquire the global single-instance mutex.

    Returns True if this process is the sole instance. Returns False if
    another suprbar is already running (and SUPRBAR_FORCE is not set).
    Always returns True on non-Windows platforms.
    """
    global _mutex_handle
    if sys.platform != "win32":
        return True
    if os.environ.get("SUPRBAR_FORCE") == "1":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        CreateMutexW = kernel32.CreateMutexW
        CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        CreateMutexW.restype = wintypes.HANDLE
        ERROR_ALREADY_EXISTS = 183
        handle = CreateMutexW(None, True, "Global\\suprbar-single-instance")
        last_err = kernel32.GetLastError()
        if last_err == ERROR_ALREADY_EXISTS:
            # Another instance already owns the mutex. Release our handle.
            try:
                kernel32.CloseHandle(handle)
            except OSError:
                pass
            return False
        _mutex_handle = handle
        return True
    except OSError as e:
        log.debug("mutex acquire failed: %s", e)
        return True  # don't block startup on a mutex API error


def release_single_instance() -> None:
    global _mutex_handle
    if sys.platform != "win32" or _mutex_handle in (None, 0):
        return
    try:
        ctypes.windll.kernel32.ReleaseMutex(_mutex_handle)
    except OSError:
        pass
    try:
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
    except OSError:
        pass
    _mutex_handle = None
