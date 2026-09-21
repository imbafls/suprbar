"""Always-on-top mini overlay — a tiny HUD that hovers over other windows.

A second frameless WebView2 window (~176x44) showing the live pip, today's
cost, and the current burn rate. Click opens the full flyout; the tray menu
(or Settings) can hide it or make it click-through.

Threading mirrors popup.py: the window is created on the main thread before
``webview.start()``; tray callbacks (other threads) call show/hide/toggle.
Position is persisted to the same window-state.json as the flyout (keys
``mini_x`` / ``mini_y``).
"""

from __future__ import annotations

import logging
import threading
import time

import webview

from . import config
from .popup import (
    TrayBridge,
    _apply_dwm_round,
    _hide_from_taskbar,
    _hwnd_by_title,
    _work_area,
    _work_area_for_point,
    load_window_state,
    save_window_state,
    set_click_through,
    set_window_pos_size,
    set_window_size,
)

log = logging.getLogger("suprbar.mini")

MINI_W = 176
MINI_H = 44
MINI_W_EXPANDED = 208
MINI_H_EXPANDED = 118
MINI_TITLE = "supr.bar mini"
MINI_MARGIN = 12
SNAP_THRESHOLD = 24  # px from a work-area corner that triggers a snap


def _clamp(x: int, y: int, wa: tuple[int, int, int, int],
           w: int = MINI_W, h: int = MINI_H) -> tuple[int, int]:
    """Clamp (x, y) so the overlay stays fully inside the work area."""
    l, t, r, b = wa
    max_x = max(l, r - w - 1)
    max_y = max(t, b - h - 1)
    return max(l, min(x, max_x)), max(t, min(y, max_y))


def _snap(x: int, y: int) -> tuple[int, int]:
    """Snap to the nearest work-area corner within SNAP_THRESHOLD."""
    cx, cy = x + MINI_W // 2, y + MINI_H // 2
    l, t, r, b = _work_area_for_point(cx, cy)
    corners = (
        (l + MINI_MARGIN, t + MINI_MARGIN),
        (r - MINI_W - MINI_MARGIN, t + MINI_MARGIN),
        (l + MINI_MARGIN, b - MINI_H - MINI_MARGIN),
        (r - MINI_W - MINI_MARGIN, b - MINI_H - MINI_MARGIN),
    )
    best = None
    best_d = SNAP_THRESHOLD
    for corner in corners:
        d = max(abs(x - corner[0]), abs(y - corner[1]))
        if d <= best_d:
            best = corner
            best_d = d
    return best if best is not None else (x, y)


class MiniBridge:
    """Owns the overlay window; tray + settings call into this."""

    def __init__(self, flyout: TrayBridge):
        self._flyout = flyout
        self._window: webview.Window | None = None
        self._hwnd: int = 0
        self._visible = False
        self._lock = threading.Lock()
        self._pending_pos: tuple[int, int] | None = None
        self._move_save_timer: threading.Timer | None = None
        self._move_save_delay = 0.35
        self._expanded = False

    # ---- wiring ----

    def attach_window(self, w: webview.Window) -> None:
        self._window = w

    def cache_hwnd(self, hwnd: int) -> None:
        if hwnd:
            self._hwnd = hwnd

    def _resolve_hwnd(self) -> int:
        if self._hwnd:
            return self._hwnd
        for _ in range(20):
            h = _hwnd_by_title(MINI_TITLE)
            if h:
                self._hwnd = h
                return h
            time.sleep(0.05)
        return 0

    def decorate(self) -> None:
        hwnd = self._resolve_hwnd()
        _apply_dwm_round(hwnd)
        _hide_from_taskbar(hwnd)
        self.apply_click_through()

    # ---- position ----

    def _default_xy(self) -> tuple[int, int]:
        """Top-right of the monitor under the cursor (flyout owns bottom-right)."""
        _l, t, r, _b = _work_area()
        return (r - MINI_W - MINI_MARGIN, t + MINI_MARGIN)

    def _resolve_show_xy(self) -> tuple[int, int]:
        state = load_window_state()
        x, y = state.get("mini_x"), state.get("mini_y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            xi, yi = int(x), int(y)
            cx, cy = xi + MINI_W // 2, yi + MINI_H // 2
            return _clamp(xi, yi, _work_area_for_point(cx, cy))
        return self._default_xy()

    def on_moved(self, x: int, y: int) -> None:
        try:
            self._pending_pos = (int(x), int(y))
            self._schedule_pos_save()
        except Exception as e:
            log.debug("mini on_moved failed: %s", e)

    def _schedule_pos_save(self) -> None:
        if self._move_save_timer is not None:
            try:
                self._move_save_timer.cancel()
            except Exception:
                pass
        t = threading.Timer(self._move_save_delay, self._flush_pos_save)
        t.daemon = True
        self._move_save_timer = t
        t.start()

    def _flush_pos_save(self) -> None:
        pos = self._pending_pos
        if pos is None:
            return
        try:
            sx, sy = _snap(pos[0], pos[1])
            save_window_state({"mini_x": sx, "mini_y": sy})
        except Exception as e:
            log.debug("mini pos save failed: %s", e)

    # ---- visibility ----

    def show(self) -> None:
        if not self._window:
            return
        with self._lock:
            # Always come back collapsed: a stale expanded size must not make
            # the chip reappear as a card.
            self._collapse_if_expanded()
            x, y = self._resolve_show_xy()
            try:
                self._window.move(x, y)
            except Exception as e:
                log.debug("mini move failed: %s", e)
            try:
                self._window.show()
            except Exception as e:
                log.debug("mini show failed: %s", e)
            self.decorate()
            self._visible = True

    def hide(self, persist: bool = False) -> None:
        if not self._window:
            return
        with self._lock:
            self._collapse_if_expanded()
            try:
                self._window.hide()
            except Exception as e:
                log.debug("mini hide failed: %s", e)
            self._visible = False
        if persist:
            try:
                config.set_pref("mini.enabled", False)
            except Exception:
                pass

    def toggle(self) -> None:
        if self._visible:
            self.hide(persist=True)
        else:
            try:
                config.set_pref("mini.enabled", True)
            except Exception:
                pass
            self.show()

    def is_visible(self) -> bool:
        return self._visible

    # ---- hover expansion ----

    def _current_xy(self) -> tuple[int, int]:
        """Best-known window origin (live if the backend exposes it)."""
        try:
            if self._window is not None:
                return int(self._window.x), int(self._window.y)
        except Exception:
            pass
        if self._pending_pos:
            return self._pending_pos
        return self._resolve_show_xy()

    def set_expanded(self, expanded: bool) -> None:
        """Grow/shrink the overlay (hover), anchored to its right edge."""
        if not self._window:
            return
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        with self._lock:
            x, y = self._current_xy()
            w = MINI_W_EXPANDED if expanded else MINI_W
            h = MINI_H_EXPANDED if expanded else MINI_H
            dx = MINI_W_EXPANDED - MINI_W
            nx = x - dx if expanded else x + dx
            nx, ny = _clamp(nx, y,
                            _work_area_for_point(x + MINI_W // 2,
                                                 y + MINI_H // 2),
                            w, h)
            # Our own SetWindowPos (no SWP_SHOWWINDOW): pywebview's resize()
            # would un-hide the overlay if it were hidden.
            set_window_pos_size(self._resolve_hwnd(), nx, ny, w, h)
            self._expanded = expanded

    def _collapse_if_expanded(self) -> None:
        if self._expanded:
            self._expanded = False
            set_window_size(self._resolve_hwnd(), MINI_W, MINI_H)

    def apply_click_through(self) -> None:
        """Sync overlay mouse-input transparency to mini.click_through."""
        try:
            on = config.mini_click_through()
        except Exception:
            on = False
        set_click_through(self._resolve_hwnd(), on)

    def open_flyout(self) -> None:
        self._flyout.show()


class MiniJsApi:
    """Functions callable from mini.js as ``window.pywebview.api.<name>()``."""

    def __init__(self, bridge: MiniBridge):
        self._bridge = bridge

    def open_flyout(self):
        self._bridge.open_flyout()

    def hide(self):
        # Explicit dismiss: persist so the overlay doesn't return next launch.
        self._bridge.hide(persist=True)

    def expand(self, on=True):
        """Hover in/out: grow the chip into the detail card (and back)."""
        self._bridge.set_expanded(bool(on))


def build_window(url_base: str, bridge: MiniBridge) -> webview.Window:
    """Create the overlay window (hidden). Call before webview.start()."""
    url = url_base.rstrip("/") + "/mini.html"
    w = webview.create_window(
        title=MINI_TITLE,
        url=url,
        width=MINI_W,
        height=MINI_H,
        # pywebview defaults min_size to (200, 100), which silently clamps a
        # chip this small — pin it to the requested size.
        min_size=(MINI_W, MINI_H),
        frameless=True,
        easy_drag=True,
        resizable=False,
        on_top=True,
        hidden=True,
        background_color="#0b0c0e",
        minimized=False,
        js_api=MiniJsApi(bridge),
    )
    assert w is not None
    bridge.attach_window(w)

    def on_loaded():
        h = _hwnd_by_title(MINI_TITLE)
        if h:
            bridge.cache_hwnd(h)
        bridge.decorate()
    w.events.loaded += on_loaded

    def on_moved(x_new, y_new):
        bridge.on_moved(x_new, y_new)
    try:
        w.events.moved += on_moved
    except (AttributeError, TypeError) as e:
        log.debug("mini moved event unavailable: %s", e)

    return w
