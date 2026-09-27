"""Frameless WebView2 popout for the supr.bar flyout.

Process model:
  This module is only imported by the flyout child process (windowhost.py),
  never by the tray process, so WebView2 is loaded only while the flyout
  exists. The tray drives it through stdin commands that land on
  FlyoutBridge; FlyoutBridge reports back through its ``notify`` callback.

Window:
  * Fixed 360 x 480 (logical px), frameless, always on top, no taskbar entry
  * Always placed at the bottom-right of the monitor under the cursor
  * Rounded corners via DWM on Windows 11
  * Hides on focus loss unless pinned, like a real tray flyout

window-state.json only holds the mini overlay's position now.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from collections.abc import Callable

import webview

from . import config

log = logging.getLogger("suprbar.popup")

# Fixed flyout size (logical px) and its margin from the work-area corner.
# It always opens at the bottom-right of the monitor under the cursor: no
# drag, no resize, no saved position that could land off-screen.
WIN_W = 360
WIN_H = 480
MARGIN = 12




# ---------- Window-state persistence (small JSON helper) ----------

def _state_dir() -> Path:
    """%LOCALAPPDATA%\\suprbar on Windows, ~/.local/share/suprbar elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local"
        )
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(
            Path.home() / ".local" / "share"
        )
    return Path(base) / "suprbar"


def _state_path() -> Path:
    return _state_dir() / "window-state.json"


_state_lock = threading.Lock()


def _read_state_unlocked() -> dict:
    """Read state without taking _state_lock. Caller must hold the lock."""
    p = _state_path()
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text("utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, json.JSONDecodeError) as e:
        log.debug("window-state load failed: %s", e)
    return {}


def load_window_state() -> dict:
    """Read persisted window state. Returns empty dict on any failure."""
    with _state_lock:
        return _read_state_unlocked()


def save_window_state(patch: dict) -> None:
    """Merge `patch` into the on-disk window-state.json (keys: x,y,w,h,pinned,last_visible)."""
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with _state_lock:
            cur = _read_state_unlocked()
            cur.update(patch)
            tmp = _state_path().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
            os.replace(tmp, _state_path())
    except OSError as e:
        log.debug("window-state save failed: %s", e)


# ---------- Win32 helpers ----------

class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG), ("top", wintypes.LONG),
        ("right", wintypes.LONG), ("bottom", wintypes.LONG),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _primary_work_area() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary monitor's work area."""
    if sys.platform != "win32":
        return (0, 0, 1920, 1040)
    user32 = ctypes.windll.user32
    rect = _RECT()
    SPI_GETWORKAREA = 0x0030
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    return rect.left, rect.top, rect.right, rect.bottom


def _work_area_for_point(px: int, py: int) -> tuple[int, int, int, int]:
    """Work area of the monitor containing (px, py). Falls back to primary."""
    if sys.platform != "win32":
        return _primary_work_area()
    try:
        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        # MonitorFromPoint takes POINT by value; pass as 64-bit on x64.
        # Use a small wrapper that takes it as two LONGs packed in a POINT.
        MonitorFromPoint = user32.MonitorFromPoint
        MonitorFromPoint.argtypes = [_POINT, wintypes.DWORD]
        MonitorFromPoint.restype = wintypes.HMONITOR
        hmon = MonitorFromPoint(_POINT(px, py), MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return _primary_work_area()
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        GetMonitorInfoW = user32.GetMonitorInfoW
        GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
        GetMonitorInfoW.restype = wintypes.BOOL
        if not GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return _primary_work_area()
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    except (OSError, AttributeError) as e:
        log.debug("monitor lookup failed: %s", e)
        return _primary_work_area()


def _cursor_pos() -> tuple[int, int]:
    if sys.platform != "win32":
        return (0, 0)
    try:
        pt = _POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y)
    except OSError:
        pass
    return (0, 0)


def _work_area() -> tuple[int, int, int, int]:
    """Work area of the monitor containing the cursor (multi-monitor aware)."""
    if sys.platform != "win32":
        return _primary_work_area()
    cx, cy = _cursor_pos()
    return _work_area_for_point(cx, cy)








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




def _dpi_scale(hwnd: int) -> float:
    """Physical/logical pixel scale for the window (1.0 when unknown)."""
    if sys.platform != "win32" or not hwnd:
        return 1.0
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    except (OSError, AttributeError):
        pass
    return 1.0


# SetWindowPos flags: NOMOVE | NOZORDER | NOACTIVATE. Deliberately never
# SWP_SHOWWINDOW — pywebview's own Window.resize() passes it (64), which
# force-shows hidden windows and left the flyout painted-blank on startup.
_SWP_NOMOVE_NOZORDER_NOACTIVATE = 0x0002 | 0x0004 | 0x0010
_SWP_NOZORDER_NOACTIVATE = 0x0004 | 0x0010


def set_window_size(hwnd: int, width: int, height: int) -> None:
    """Resize a window without showing or moving it (frameless drag-resize)."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        s = _dpi_scale(hwnd)
        ctypes.windll.user32.SetWindowPos(
            hwnd, None, 0, 0, int(width * s), int(height * s),
            _SWP_NOMOVE_NOZORDER_NOACTIVATE,
        )
    except OSError as e:
        log.debug("set_window_size failed: %s", e)


def set_window_pos_size(hwnd: int, x: int, y: int,
                        width: int, height: int) -> None:
    """Move + resize a window without showing it (mini hover expansion)."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        s = _dpi_scale(hwnd)
        ctypes.windll.user32.SetWindowPos(
            hwnd, None, int(x * s), int(y * s),
            int(width * s), int(height * s),
            _SWP_NOZORDER_NOACTIVATE,
        )
    except OSError as e:
        log.debug("set_window_pos_size failed: %s", e)


# ---------- WebView2 runtime detection ----------

def _show_webview2_install_dialog() -> None:
    """Pop a modal MessageBox pointing the user at the WebView2 download."""
    if sys.platform != "win32":
        log.error("WebView2 runtime appears to be missing.")
        return
    try:
        MB_ICONERROR = 0x10
        MB_OK = 0x00
        text = (
            "supr.bar needs Microsoft Edge WebView2 Runtime to display "
            "its popup window.\n\n"
            "Install it from:\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
            "After installing, restart supr.bar."
        )
        ctypes.windll.user32.MessageBoxW(
            None, text, "supr.bar — WebView2 required", MB_ICONERROR | MB_OK,
        )
    except OSError:
        pass


# ---------- Placement ----------

def _monitor_dpi_scale(px: int, py: int) -> float:
    """DPI scale of the monitor containing (px, py); 1.0 when unknown."""
    if sys.platform != "win32":
        return 1.0
    try:
        user32 = ctypes.windll.user32
        MonitorFromPoint = user32.MonitorFromPoint
        MonitorFromPoint.argtypes = [_POINT, wintypes.DWORD]
        MonitorFromPoint.restype = wintypes.HMONITOR
        hmon = MonitorFromPoint(_POINT(px, py), 2)  # MONITOR_DEFAULTTONEAREST
        dx, dy = wintypes.UINT(), wintypes.UINT()
        # MDT_EFFECTIVE_DPI = 0
        if ctypes.windll.shcore.GetDpiForMonitor(
                hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
            return dx.value / 96.0
    except (OSError, AttributeError) as e:
        log.debug("monitor dpi lookup failed: %s", e)
    return 1.0


def flyout_rect() -> tuple[int, int, int, int]:
    """Physical-pixel (x, y, w, h) for the flyout on the cursor's monitor.

    The work area is in physical pixels, so the logical size and margin are
    scaled by that monitor's DPI before subtracting — mixing the two put the
    flyout partly off-screen on scaled displays.
    """
    cx, cy = _cursor_pos()
    left, top, right, bottom = _work_area_for_point(cx, cy)
    s = _monitor_dpi_scale(cx, cy)
    w, h, m = int(WIN_W * s), int(WIN_H * s), int(MARGIN * s)
    x = max(left, right - w - m)
    y = max(top, bottom - h - m)
    return x, y, w, h


def _place(hwnd: int) -> None:
    """Move + size the (hidden or shown) flyout without activating it."""
    if sys.platform != "win32" or not hwnd:
        return
    x, y, w, h = flyout_rect()
    try:
        ctypes.windll.user32.SetWindowPos(hwnd, None, x, y, w, h,
                                          _SWP_NOZORDER_NOACTIVATE)
    except OSError as e:
        log.debug("flyout placement failed: %s", e)


# ---------- Flyout window bridge (runs in the flyout child process) ----------

class FlyoutBridge:
    """Owns the flyout window inside its child process (see windowhost.py).

    ``notify`` sends an event line to the tray process: "shown" / "hidden"
    (so it can retire an idle flyout) and "quit" (Alt+Q).
    """

    def __init__(self, notify: Callable[[str], None]):
        self._notify = notify
        self._window: webview.Window | None = None
        self._hwnd: int = 0
        self._visible = False
        self._lock = threading.Lock()
        self._last_hide_ts: float = 0.0
        # Ignore a blur right after show: focus is still settling and the
        # window would otherwise hide itself immediately.
        self._show_settle_ts: float = 0.0
        self._settle_seconds = 0.4
        self._toggle_grace_seconds = 0.35
        # Settings-open hint; the page reads it via consume_pending_open().
        self._open_settings_next_show = False

    def attach_window(self, w: webview.Window) -> None:
        self._window = w

    def _resolve_hwnd(self) -> int:
        if not self._hwnd:
            for _ in range(20):
                self._hwnd = _hwnd_by_title("supr.bar")
                if self._hwnd:
                    break
                time.sleep(0.05)
        return self._hwnd

    def decorate(self) -> None:
        hwnd = self._resolve_hwnd()
        _apply_dwm_round(hwnd)
        _hide_from_taskbar(hwnd)

    def open_with_settings(self) -> None:
        """Show the flyout with the settings sheet open."""
        self._open_settings_next_show = True
        w = self._window
        if w is not None:
            def _push():
                try:
                    w.evaluate_js("window.__suprbarOpenSettings"
                                  " && window.__suprbarOpenSettings()")
                except Exception as e:
                    log.debug("open settings push failed: %s", e)
            threading.Thread(target=_push, daemon=True,
                             name="suprbar-flyout-settings").start()
        self.show()

    def show(self) -> None:
        if self._window is None:
            return
        with self._lock:
            _place(self._resolve_hwnd())
            try:
                self._window.show()
            except Exception as e:
                log.debug("show failed: %s", e)
            self.decorate()
            # Re-assert the rect: WinForms may rescale the form on first show.
            _place(self._resolve_hwnd())
            self._visible = True
            self._show_settle_ts = time.monotonic()
        self._set_page_visible(True)
        self._notify("shown")

    def hide(self, from_blur: bool = False) -> None:
        if self._window is None:
            return
        with self._lock:
            if from_blur:
                if config.is_pinned():
                    return
                if time.monotonic() - self._show_settle_ts < self._settle_seconds:
                    return
            was_visible = self._visible
            try:
                self._window.hide()
            except Exception as e:
                log.debug("hide failed: %s", e)
            self._visible = False
            self._last_hide_ts = time.monotonic()
        if was_visible:
            self._set_page_visible(False)
            self._notify("hidden")

    def _set_page_visible(self, on: bool) -> None:
        """Pause/resume the page's polling (app.js ``__suprbarVisible``).

        Runs off-thread: evaluate_js blocks until the page has loaded.
        """
        w = self._window
        if w is None:
            return
        js = ("window.__suprbarVisible && window.__suprbarVisible(%s)"
              % ("true" if on else "false"))

        def _run():
            try:
                w.evaluate_js(js)
            except Exception as e:
                log.debug("visibility push failed: %s", e)
        threading.Thread(target=_run, daemon=True,
                         name="suprbar-flyout-vis").start()

    def toggle(self) -> None:
        # A tray click on a visible flyout first blurs it (hiding it), then
        # toggles: treat a hide in the last moment as the toggle-off.
        if self._visible:
            self.hide()
            return
        if time.monotonic() - self._last_hide_ts < self._toggle_grace_seconds:
            return
        self.show()

    def quit(self) -> None:
        """Alt+Q: ask the tray process to shut the whole app down."""
        self._notify("quit")


# ---------- JS bridge exposed to the flyout page ----------

class JsApi:
    """Functions callable from JS as `window.pywebview.api.<name>()`."""

    def __init__(self, bridge: FlyoutBridge):
        self._bridge = bridge

    def hide(self):
        # Esc / focus loss: treated as a blur so Pin is honoured.
        self._bridge.hide(from_blur=True)

    def quit(self):
        self._bridge.quit()

    def consume_pending_open(self) -> str:
        """The page asks once on load whether to open settings first."""
        if self._bridge._open_settings_next_show:
            self._bridge._open_settings_next_show = False
            return "settings"
        return ""


# ---------- Window creation ----------

def build_window(url: str, bridge: FlyoutBridge) -> webview.Window:
    x, y, _w, _h = flyout_rect()
    w = webview.create_window(
        title="supr.bar",
        url=url,
        width=WIN_W,
        height=WIN_H,
        x=x,
        y=y,
        frameless=True,
        easy_drag=False,
        resizable=False,
        on_top=True,
        hidden=True,
        background_color="#0d1018",
        js_api=JsApi(bridge),
    )
    # pywebview's stubs type create_window() as Optional; it always returns a
    # Window in practice.
    assert w is not None
    bridge.attach_window(w)
    w.events.loaded += bridge.decorate
    return w
