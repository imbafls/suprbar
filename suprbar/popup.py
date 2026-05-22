"""Frameless WebView2 popout for the supr.bar flyout.

Threading:
  webview.start() must run on the main thread. We launch the tray on a
  background thread via a callback passed to start(). pystray callbacks then
  call into this module's TrayBridge to show/hide/toggle the window.

Window:
  * 360 x 480 px (matches MVP "Tray · live session" artboard, room for footer)
  * Frameless, on-top, no taskbar entry
  * Positioned bottom-right of the work area (above the taskbar)
  * Rounded corners via DWM API on Windows 11
  * Auto-hide on focus loss so it behaves like a real tray flyout
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable

import webview

from . import config

log = logging.getLogger("suprbar.popup")

WIN_W = 360
WIN_H = 480
MARGIN_RIGHT = 12
MARGIN_BOTTOM = 12


# ---------- Win32 helpers ----------

def _work_area() -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the primary monitor's work area
    (excludes taskbar)."""
    if sys.platform != "win32":
        return (0, 0, 1920, 1040)
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG), ("top", wintypes.LONG),
            ("right", wintypes.LONG), ("bottom", wintypes.LONG),
        ]
    rect = RECT()
    SPI_GETWORKAREA = 0x0030
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    return rect.left, rect.top, rect.right, rect.bottom


def _bottom_right_xy() -> tuple[int, int]:
    l, t, r, b = _work_area()
    return (r - WIN_W - MARGIN_RIGHT, b - WIN_H - MARGIN_BOTTOM)


def _hwnd_by_title(title: str) -> int:
    if sys.platform != "win32":
        return 0
    return ctypes.windll.user32.FindWindowW(None, title) or 0


def _apply_dwm_round(hwnd: int) -> None:
    """Win11 only: round window corners via DWM."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        DwmSetWindowAttribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        DwmSetWindowAttribute.restype = ctypes.c_long
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref),
        )
    except (OSError, AttributeError):
        pass


def _hide_from_taskbar(hwnd: int) -> None:
    """Set WS_EX_TOOLWINDOW so the popup doesn't appear in Alt+Tab/taskbar."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        user32 = ctypes.windll.user32
        # GetWindowLongPtrW for 64-bit Python
        get_long = user32.GetWindowLongPtrW
        set_long = user32.SetWindowLongPtrW
        get_long.restype = ctypes.c_ssize_t
        set_long.restype = ctypes.c_ssize_t
        ex = get_long(hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        set_long(hwnd, GWL_EXSTYLE, ex)
    except OSError:
        pass


# ---------- Bridge that tray callbacks talk to ----------

class TrayBridge:
    def __init__(self):
        self._window: webview.Window | None = None
        self._hwnd: int = 0
        self._visible = False
        self._lock = threading.Lock()
        self._window_title = "supr.bar"
        self._last_hide_ts: float = 0.0
        # Suppress JS blur->hide for a brief moment right after show, otherwise
        # the window can hide on the focus-settle flicker.
        self._show_settle_ts: float = 0.0
        self._settle_seconds = 0.4
        self._toggle_grace_seconds = 0.35

    def attach_window(self, w: webview.Window) -> None:
        self._window = w

    def _resolve_hwnd(self) -> int:
        if self._hwnd:
            return self._hwnd
        # Try a few times — the window takes a beat to register its title.
        for _ in range(20):
            h = _hwnd_by_title(self._window_title)
            if h:
                self._hwnd = h
                return h
            time.sleep(0.05)
        return 0

    def _decorate(self) -> None:
        hwnd = self._resolve_hwnd()
        _apply_dwm_round(hwnd)
        _hide_from_taskbar(hwnd)

    def show(self) -> None:
        if not self._window:
            return
        with self._lock:
            x, y = _bottom_right_xy()
            try:
                self._window.move(x, y)
            except Exception as e:
                log.debug("move failed: %s", e)
            try:
                self._window.show()
            except Exception as e:
                log.debug("show failed: %s", e)
            self._decorate()
            self._visible = True
            self._show_settle_ts = time.monotonic()

    def hide(self, from_blur: bool = False) -> None:
        if not self._window:
            return
        with self._lock:
            if from_blur:
                # Don't auto-hide if user has pinned the popup.
                if config.is_pinned():
                    return
                # Ignore blur-driven hide if the window just opened — the
                # focus is still settling.
                if time.monotonic() - self._show_settle_ts < self._settle_seconds:
                    return
            try:
                self._window.hide()
            except Exception as e:
                log.debug("hide failed: %s", e)
            self._visible = False
            self._last_hide_ts = time.monotonic()

    def toggle(self) -> None:
        # If the user clicks the tray icon while the popup is visible, JS
        # blur fires first and hides the window. By the time toggle() runs,
        # _visible is False, so we'd re-show. Detect that race via the
        # recent hide timestamp and treat as toggle-off.
        if self._visible:
            self.hide()
            return
        if time.monotonic() - self._last_hide_ts < self._toggle_grace_seconds:
            return
        self.show()

    def is_visible(self) -> bool:
        return self._visible

    def quit(self) -> None:
        if not self._window:
            return
        try:
            self._window.destroy()
        except Exception:
            pass


# ---------- JS bridge exposed to the popup ----------

class JsApi:
    """Functions callable from JS as `window.pywebview.api.<name>()`."""

    def __init__(self, bridge: TrayBridge):
        self._bridge = bridge

    def hide(self):
        # JS-driven hide (Esc key, blur). Treat as blur-style so pin matters.
        self._bridge.hide(from_blur=True)

    def quit(self):
        # Alt+Q. Trigger full app shutdown via the bridge.
        self._bridge.quit()


# ---------- Window creation ----------

def build_window(url: str, bridge: TrayBridge) -> webview.Window:
    x, y = _bottom_right_xy()
    w = webview.create_window(
        title="supr.bar",
        url=url,
        width=WIN_W,
        height=WIN_H,
        x=x,
        y=y,
        frameless=True,
        easy_drag=False,         # we use CSS -webkit-app-region: drag instead
        resizable=False,
        on_top=True,
        hidden=True,              # tray click reveals it
        background_color="#0d1018",
        minimized=False,
        js_api=JsApi(bridge),
    )
    bridge.attach_window(w)

    def on_loaded():
        bridge._decorate()
    w.events.loaded += on_loaded

    return w


def run(url: str, bridge: TrayBridge, on_started: Callable[[], None]) -> None:
    """Blocks on the main thread until quit."""
    build_window(url, bridge)

    def started():
        # webview is up; safe to launch tray
        try:
            on_started()
        except Exception:
            log.exception("on_started callback failed")

    webview.start(started, debug=False)
