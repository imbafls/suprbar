"""Frameless WebView2 popout for the supr.bar flyout.

Threading:
  webview.start() must run on the main thread. We launch the tray on a
  background thread via a callback passed to start(). pystray callbacks then
  call into this module's TrayBridge to show/hide/toggle the window.

Window:
  * 360 x 480 px (matches MVP "Tray · live session" artboard, room for footer)
  * Frameless, on-top, no taskbar entry
  * Positioned bottom-right of the work area on the monitor under the cursor
  * Rounded corners + transient Mica backdrop via DWM API on Windows 11
  * Auto-hide on focus loss so it behaves like a real tray flyout

Window position is persisted to %LOCALAPPDATA%\\suprbar\\window-state.json so
the user's last placement (after dragging or snap-to-corner) is restored on
next show.
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
from typing import Callable

import webview

from . import config

log = logging.getLogger("suprbar.popup")

WIN_W = 360
WIN_H = 480
MARGIN_RIGHT = 12
MARGIN_BOTTOM = 12
SNAP_THRESHOLD = 24  # px from a work-area corner that triggers snap


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


def _bottom_right_xy() -> tuple[int, int]:
    """Default popup position: bottom-right of the monitor under the cursor."""
    l, t, r, b = _work_area()
    return (r - WIN_W - MARGIN_RIGHT, b - WIN_H - MARGIN_BOTTOM)


def _clamp_to_work_area(x: int, y: int,
                        wa: tuple[int, int, int, int] | None = None
                        ) -> tuple[int, int]:
    """Clamp a window's (x, y) so it stays fully inside the given work area."""
    l, t, r, b = wa if wa is not None else _work_area()
    # Allow at least 8px margin on all sides so the user can grab the edge.
    max_x = r - WIN_W - 1
    max_y = b - WIN_H - 1
    min_x = l
    min_y = t
    if max_x < min_x:
        max_x = min_x
    if max_y < min_y:
        max_y = min_y
    return (max(min_x, min(x, max_x)), max(min_y, min(y, max_y)))


def _snap_to_corner(x: int, y: int) -> tuple[int, int]:
    """If (x, y) is within SNAP_THRESHOLD of any work-area corner of the
    monitor containing the *window* (not the cursor), snap to it. Otherwise
    return (x, y) unchanged."""
    # Use the center of the window to pick the relevant monitor.
    cx, cy = x + WIN_W // 2, y + WIN_H // 2
    l, t, r, b = _work_area_for_point(cx, cy)
    # Candidate corners (top-left coordinates of the window when snapped):
    tl = (l + MARGIN_RIGHT, t + MARGIN_BOTTOM)
    tr = (r - WIN_W - MARGIN_RIGHT, t + MARGIN_BOTTOM)
    bl = (l + MARGIN_RIGHT, b - WIN_H - MARGIN_BOTTOM)
    br = (r - WIN_W - MARGIN_RIGHT, b - WIN_H - MARGIN_BOTTOM)
    best = None
    best_d = SNAP_THRESHOLD
    for corner in (tl, tr, bl, br):
        d = max(abs(x - corner[0]), abs(y - corner[1]))
        if d <= best_d:
            best = corner
            best_d = d
    return best if best is not None else (x, y)


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


def _apply_mica_backdrop(hwnd: int) -> None:
    """Win11 22H2+: ask DWM for a Mica/transient backdrop. Silent on failure."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        DwmSetWindowAttribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        DwmSetWindowAttribute.restype = ctypes.c_long
        DWMWA_SYSTEMBACKDROP_TYPE = 38
        DWMSBT_TRANSIENTWINDOW = 4  # acrylic-like transient
        v = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        DwmSetWindowAttribute(
            hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(v), ctypes.sizeof(v),
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


# ---------- Single-instance mutex ----------

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


# ---------- Bridge that tray callbacks talk to ----------

class TrayBridge:
    def __init__(self):
        # Single-instance guard: if another suprbar already owns the named
        # mutex, bail out *before* we open the webview / tray. Bypass via
        # SUPRBAR_FORCE=1 for development. Constructing TrayBridge is the
        # first thing __main__ does, so this is the earliest reliable hook.
        if not acquire_single_instance():
            sys.stderr.write(
                "supr.bar already running (set SUPRBAR_FORCE=1 to override)\n"
            )
            sys.exit(0)

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
        # Settings-open hint passed via URL hash. The frontend reads
        # location.hash on load to decide whether to jump to settings.
        self._open_settings_next_show = False

    def attach_window(self, w: webview.Window) -> None:
        self._window = w

    def cache_hwnd(self, hwnd: int) -> None:
        """Called from the `loaded` event so we stop polling FindWindowW."""
        if hwnd:
            self._hwnd = hwnd

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
        _apply_mica_backdrop(hwnd)
        _hide_from_taskbar(hwnd)

    def _resolve_show_xy(self) -> tuple[int, int]:
        """Pick (x, y) for show(): saved position clamped to current monitor
        if available; otherwise the bottom-right of the cursor's monitor."""
        state = load_window_state()
        if isinstance(state.get("x"), (int, float)) and \
                isinstance(state.get("y"), (int, float)):
            x, y = int(state["x"]), int(state["y"])
            # Clamp to the monitor that currently contains the window center.
            cx, cy = x + WIN_W // 2, y + WIN_H // 2
            wa = _work_area_for_point(cx, cy)
            return _clamp_to_work_area(x, y, wa)
        return _bottom_right_xy()

    def open_with_settings(self) -> None:
        """Show the popup and tell the frontend to open the settings view.

        We use the URL hash so this works regardless of whether the frontend
        is already loaded (a load_url with #settings will trigger hashchange).
        """
        self._open_settings_next_show = True
        if not self._window:
            return
        try:
            cur = self._window.get_current_url() or ""
        except Exception:
            cur = ""
        try:
            if cur:
                base = cur.split("#", 1)[0]
                self._window.load_url(base + "#settings")
            else:
                # No URL yet — frontend will read pending hash via JsApi.
                pass
        except Exception as e:
            log.debug("load_url for settings failed: %s", e)
        self.show()

    def show(self) -> None:
        if not self._window:
            return
        with self._lock:
            x, y = self._resolve_show_xy()
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
            save_window_state({"last_visible": time.time()})

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
        """Graceful shutdown: destroy webview, clear mutex.

        Order matters — we want the webview gone before pystray stops so the
        Windows message loop can drain cleanly. The tray's _on_quit signals
        pystray._icon.stop() afterwards.
        """
        if self._window:
            try:
                self._window.destroy()
            except Exception:
                pass
            # If pywebview ever grows a shutdown(), call it.
            shutdown = getattr(webview, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    pass
        release_single_instance()

    # ---- callbacks invoked from webview event handlers ----

    def on_moved(self, x: int, y: int) -> None:
        """Persist new position; snap to corner if close to one."""
        try:
            snapped = _snap_to_corner(int(x), int(y))
            if snapped != (int(x), int(y)) and self._window is not None:
                try:
                    self._window.move(*snapped)
                except Exception as e:
                    log.debug("snap move failed: %s", e)
                sx, sy = snapped
            else:
                sx, sy = int(x), int(y)
            save_window_state({"x": sx, "y": sy, "w": WIN_W, "h": WIN_H})
        except Exception as e:
            log.debug("on_moved failed: %s", e)


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

    def open_settings(self):
        """Programmatically open the settings view in the popup."""
        self._bridge.open_with_settings()

    def consume_pending_open(self) -> str:
        """Frontend may poll this on load to discover a queued navigation."""
        if self._bridge._open_settings_next_show:
            self._bridge._open_settings_next_show = False
            return "settings"
        return ""


# ---------- Window creation ----------

def build_window(url: str, bridge: TrayBridge) -> webview.Window:
    x, y = bridge._resolve_show_xy()
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
        # Cache hwnd once instead of polling FindWindowW on every show().
        h = _hwnd_by_title("supr.bar")
        if h:
            bridge.cache_hwnd(h)
        bridge._decorate()
    w.events.loaded += on_loaded

    def on_moved(x_new, y_new):
        bridge.on_moved(x_new, y_new)
    try:
        w.events.moved += on_moved
    except (AttributeError, TypeError) as e:
        log.debug("moved event unavailable: %s", e)

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

    try:
        webview.start(started, debug=False)
    except Exception as e:
        # Most common cause on Windows: the WebView2 runtime isn't installed.
        msg = (str(e) or "").lower()
        likely_webview2 = (
            "webview2" in msg
            or "edge" in msg
            or "runtime" in msg
            or "client" in msg
            or "0x80070005" in msg
            or "no module" in msg
            or isinstance(e, webview.WebViewException)
        )
        if likely_webview2:
            log.error("WebView2 runtime missing or failed to start: %s", e)
            _show_webview2_install_dialog()
        else:
            log.exception("webview.start failed")
        # Make sure the mutex doesn't get stranded if startup blew up.
        release_single_instance()
        raise
