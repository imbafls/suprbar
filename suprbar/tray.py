"""System tray icon — gradient 'S', toggles the popout, pin checkbox, quit.

The icon is rendered at 256x256 for crispness and downsampled to 64x64 via
LANCZOS so the "S" glyph stays sharp at any tray DPI. A live-indicator
variant adds a green dot when an active Claude Code session is detected.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw, ImageFont

from . import config, server, updater
from .windows import FlyoutController

log = logging.getLogger("suprbar.tray")

# Polling cadence for tooltip + idle/live state. Dropped from 60s -> 30s so
# the green-dot icon reflects new sessions quickly. We also force-refresh
# whenever the set of enabled source IDs changes between polls. With no live
# session there is nothing to chase, so the loop backs off to IDLE seconds.
REFRESH_SECONDS = 30
REFRESH_IDLE_SECONDS = 90
PULSE_MS = 300
QUIT_WATCHDOG_SECONDS = 6.0


# ---------- Icon drawing ----------

def _gradient_image(size: int) -> Image.Image:
    """Diagonal indigo gradient (the redesign accent, 135° #5b8fe8 → #7a6cf0)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    a, v = (91, 143, 232), (122, 108, 240)
    px = img.load()
    assert px is not None  # Pillow stubs type load() Optional; it never is
    denom = 2 * (size - 1) if size > 1 else 1
    for y in range(size):
        for x in range(size):
            t = (x + y) / denom
            r = int(a[0] + (v[0] - a[0]) * t)
            g = int(a[1] + (v[1] - a[1]) * t)
            b = int(a[2] + (v[2] - a[2]) * t)
            px[x, y] = (r, g, b, 255)
    return img


def _load_bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Prefer Segoe UI Black, then Segoe UI Bold (italic backup), then Arial."""
    for candidate in ("seguibl.ttf", "segoeuib.ttf",
                      "segoeuiz.ttf",  # Segoe UI Bold Italic
                      "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_S(bg: Image.Image, brighter: bool = False) -> Image.Image:
    """Composite a centered, anti-aliased 'S' onto the gradient background."""
    size = bg.size[0]
    # Rounded-rectangle mask for the whole tile.
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, size - 1, size - 1),
                         radius=int(size * 0.22), fill=255)
    bg.putalpha(mask)

    # Draw text into a separate transparent overlay so we can sample at
    # high resolution and let the downscale anti-alias it.
    font = _load_bold_font(int(size * 0.62))
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    text = "S"
    # textbbox exists in every Pillow this app supports (>= 10).
    bbox = od.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1] - int(size * 0.035)
    fill = (255, 255, 255, 255)
    od.text((tx, ty), text, font=font, fill=fill)

    bg = Image.alpha_composite(bg, overlay)
    return bg


def _add_live_dot(img: Image.Image) -> Image.Image:
    """Overlay a small green dot (live indicator) at the bottom-right corner."""
    size = img.size[0]
    out = img.copy()
    d = ImageDraw.Draw(out)
    # 8x8 in final 64x64 → ratio 0.125; here we draw at the rendered size.
    dot = max(8, int(size * 0.125))
    pad = max(2, int(size * 0.03))
    x1 = size - dot - pad
    y1 = size - dot - pad
    # White halo so the dot is visible regardless of background luminance.
    halo = max(1, dot // 6)
    d.ellipse(
        (x1 - halo, y1 - halo, x1 + dot + halo, y1 + dot + halo),
        fill=(255, 255, 255, 230),
    )
    d.ellipse((x1, y1, x1 + dot, y1 + dot), fill=(43, 208, 122, 255))  # live #2bd07a
    return out


def _brighten(img: Image.Image, factor: float = 1.18) -> Image.Image:
    """Return a brighter copy of `img` (used for the refresh pulse)."""
    bands = img.split()
    if len(bands) == 4:
        r, g, b, a = bands
        rgb = Image.merge("RGB", (r, g, b))
        from PIL import ImageEnhance
        bright = ImageEnhance.Brightness(rgb).enhance(factor)
        br, bg, bb = bright.split()
        return Image.merge("RGBA", (br, bg, bb, a))
    return img


def _render(live: bool = False, brighter: bool = False) -> Image.Image:
    """Render a tray icon (256→64 px LANCZOS), optionally live + brighter."""
    big_size = 256
    target_size = 64
    bg = _gradient_image(big_size)
    bg = _draw_S(bg)
    if live:
        bg = _add_live_dot(bg)
    if brighter:
        bg = _brighten(bg, 1.18)
    bg.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
    return bg


# ---------- Tooltip ----------

def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


def _format_tooltip(data: dict) -> str:
    """Multi-line tooltip. Kept under 255 chars (Windows balloon limit)."""
    today = data.get("today", {}) or {}
    cost = today.get("cost", 0.0) or 0.0
    msgs = today.get("messages", 0) or 0
    active = data.get("active") or None
    sources = data.get("sources", []) or []

    # Per-source mini line ("Claude Code $X.XX  ·  Anthropic API $Y.YY")
    src_bits: list[str] = []
    for s in sources:
        if not s.get("ok"):
            continue
        label = (s.get("label") or s.get("id") or "?").split("·")[0].strip()
        c = s.get("cost_today", 0) or 0
        src_bits.append(f"{label} ${c:,.2f}")
    multi_source = len(src_bits) > 1
    src_line = "  ·  ".join(src_bits) if (multi_source and src_bits) else ""

    if active:
        proj = (active.get("project") or "").strip()
        proj_short = _truncate(proj, 40) if proj else ""
        live_msgs = active.get("messages_today")
        if live_msgs is None:
            live_msgs = msgs
        lines = [
            "supr.bar — live",
            f"${cost:,.2f} today · {live_msgs} msgs",
        ]
        if src_line:
            lines.append(src_line)
        if proj_short:
            lines.append(proj_short)
    else:
        lines = [
            "supr.bar — idle",
            f"${cost:,.2f} today · no live session",
        ]
        if src_line:
            lines.append(src_line)

    tooltip = "\n".join(lines).rstrip()
    # Windows tooltip cap is 127 for older shells, 255 for modern. Stay safe.
    if len(tooltip) > 250:
        tooltip = tooltip[:249] + "…"
    return tooltip


# ---------- TrayApp ----------

def _off_ui(fn):
    """Run a tray callback on a worker thread.

    pystray calls menu/click handlers on its Win32 message loop; anything that
    blocks there (starting or waiting on a window child, a scan, a browser
    launch) freezes the icon and Windows reports the app as hung.
    """
    def wrapper(self, icon=None, item=None):
        def run():
            try:
                fn(self, icon, item)
            except Exception:
                log.exception("tray action %s failed", fn.__name__)
        threading.Thread(target=run, daemon=True,
                         name=f"suprbar-{fn.__name__}").start()
    wrapper.__name__ = fn.__name__
    return wrapper

class TrayApp:
    def __init__(self, bridge: FlyoutController):
        self.bridge = bridge
        self._icon: pystray.Icon | None = None
        self._stop = threading.Event()
        self._last_live: bool | None = None
        # Pre-render both variants once so updates only flip a reference.
        self._idle_icon = _render(live=False)
        self._live_icon = _render(live=True)
        self._idle_bright = _render(live=False, brighter=True)
        self._live_bright = _render(live=True, brighter=True)
        self._pulse_timer: threading.Timer | None = None

    # ---- click / menu callbacks ----


    @_off_ui
    def _on_refresh(self, icon, item):
        server.invalidate_today_cache()
        self._pulse_icon()
        self._update_tooltip()

    @_off_ui
    def _on_report(self, icon, item):
        try:
            server.open_report_in_browser()
        except Exception:
            log.exception("open report failed")



    @_off_ui
    def _on_settings(self, icon, item):
        try:
            self.bridge.open_with_settings()
        except Exception:
            log.exception("open settings failed")


    # ---- update menu callbacks ----

    def _update_available(self) -> bool:
        st = updater.cached_status()
        return bool(st and st.get("available"))

    def _update_item_text(self, item) -> str:
        st = updater.cached_status() or {}
        return f"Update to v{st.get('latest')}…" if st.get("available") else "Update"


    def _on_apply_update(self, icon, item):
        # Same path the flyout button uses; updater quits the app when done.
        threading.Thread(
            target=updater.download_and_apply, daemon=True).start()

    def _on_quit(self, icon, item):
        # Whatever hangs (a child, pystray, a scan thread), the process must
        # be gone within a few seconds: a zombie tray keeps the port and the
        # single-instance lock and blocks the next launch.
        def _force_exit():
            time.sleep(QUIT_WATCHDOG_SECONDS)
            log.warning("shutdown hung — forcing exit")
            os._exit(0)
        threading.Thread(target=_force_exit, daemon=True,
                         name="suprbar-exit-watchdog").start()
        self._stop.set()
        try:
            self.bridge.quit()
        except Exception:
            pass
        if self._pulse_timer is not None:
            try:
                self._pulse_timer.cancel()
            except Exception:
                pass
            self._pulse_timer = None
        try:
            if self._icon:
                self._icon.stop()
        except Exception:
            pass

    # ---- pystray default-item compatibility for double-click ----

    @_off_ui
    def _on_default(self, icon, item):
        # Bound through MenuItem(..., default=True). pystray on Windows fires
        # this on left single-click; we also bind via _on_notify below so
        # double-click is handled.
        self.bridge.toggle()

    def _on_middle(self, icon=None, item=None):
        """Middle-click on the tray icon toggles the pin state."""
        try:
            new = not config.is_pinned()
            config.set_pinned(new)
            if self._icon:
                self._icon.update_menu()
                # Surface the change so the user sees what middle-click did.
                state = "pinned" if new else "unpinned"
                try:
                    self._icon.notify(f"Popup {state}", "supr.bar")
                except Exception:
                    pass
        except Exception:
            log.exception("middle-click toggle failed")

    # ---- mini overlay menu callbacks ----

    @_off_ui
    def _on_mini_toggle(self, icon, item):
        try:
            mini = self.bridge.mini
            if mini is None:
                return
            enabled = not config.mini_enabled()
            config.set_pref("mini.enabled", enabled)
            if enabled:
                mini.show()
            else:
                mini.hide()
            if self._icon:
                self._icon.update_menu()
        except Exception:
            log.exception("mini toggle failed")

    def _is_mini_enabled(self, item) -> bool:
        try:
            return config.mini_enabled()
        except Exception:
            return False





    # ---- icon / tooltip updates ----

    def _current_icon(self, brighter: bool = False) -> Image.Image:
        live = bool(self._last_live)
        if brighter:
            return self._live_bright if live else self._idle_bright
        return self._live_icon if live else self._idle_icon

    def _pulse_icon(self):
        """Briefly swap to a brighter icon, then revert after PULSE_MS."""
        if not self._icon:
            return
        try:
            self._icon.icon = self._current_icon(brighter=True)
        except Exception:
            pass
        # Cancel any in-flight pulse so we don't double-revert.
        if self._pulse_timer is not None:
            try:
                self._pulse_timer.cancel()
            except Exception:
                pass

        def revert():
            if not self._icon or self._stop.is_set():
                return
            try:
                self._icon.icon = self._current_icon(brighter=False)
            except Exception:
                pass

        self._pulse_timer = threading.Timer(PULSE_MS / 1000.0, revert)
        self._pulse_timer.daemon = True
        self._pulse_timer.start()

    def _apply_live_state(self, data: dict) -> None:
        """Swap to the green-dot icon while a session is live."""
        live = bool(data.get("active"))
        if live != self._last_live and self._icon:
            try:
                self._icon.icon = self._live_icon if live else self._idle_icon
            except Exception:
                pass
        self._last_live = live

    def _sync_mini(self) -> None:
        """Keep overlay visibility in step with the persisted pref."""
        try:
            self.bridge.mini.sync()
        except Exception:
            log.debug("mini sync failed", exc_info=True)

    def _update_tooltip(self):
        try:
            data = server.today_cached()
            self._apply_live_state(data)
            self._sync_mini()
            if self._icon:
                self._icon.title = _format_tooltip(data)
        except Exception as e:
            if self._icon:
                self._icon.title = f"supr.bar — error: {e!s:.60}"


    def _refresh_loop(self):
        self._update_tooltip()
        while not self._stop.is_set():
            # Idle backoff: nothing to chase without a live session.
            wait_s = REFRESH_SECONDS if self._last_live else REFRESH_IDLE_SECONDS
            if self._stop.wait(wait_s):
                return
            # Bust the today cache so the next tooltip reflects fresh data.
            # (Light invalidation: range/report caches stay valid.)
            server.invalidate_today_only()
            self._update_tooltip()

    # ---- run ----

    def build_menu(self) -> pystray.Menu:
        """Six items; "Update to vX…" appears only when a release is out.

        Pin lives on the flyout's pin button and on tray middle-click.
        """
        return pystray.Menu(
            pystray.MenuItem("Open supr.bar", self._on_default, default=True),
            pystray.MenuItem("Mini overlay", self._on_mini_toggle,
                             checked=self._is_mini_enabled),
            pystray.MenuItem("Refresh", self._on_refresh),
            pystray.MenuItem("30-day report", self._on_report),
            pystray.MenuItem("Settings…", self._on_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._update_item_text, self._on_apply_update,
                             visible=lambda item: self._update_available()),
            pystray.MenuItem("Quit", self._on_quit),
        )

    def run(self):
        menu = self.build_menu()
        self._icon = pystray.Icon(
            "suprbar",
            self._idle_icon,
            "supr.bar — loading…",
            menu,
        )

        # Middle-click toggles pin; a double-click counts as one click.
        # pystray's win32 backend dispatches through the _message_handlers
        # dict it builds in __init__, so the handler must be replaced there —
        # rebinding the _on_notify attribute afterwards is never called.
        handlers = getattr(self._icon, "_message_handlers", None)
        if sys.platform == "win32" and isinstance(handlers, dict):
            from pystray._util import win32 as _w32
            orig_notify = handlers.get(_w32.WM_NOTIFY)
            swallow_up = False

            def notify_wrapper(wparam, lparam):
                nonlocal swallow_up
                msg = lparam & 0xFFFF
                if msg == 0x0207:  # WM_MBUTTONDOWN
                    self._on_middle(self._icon, None)
                    return 0
                if msg == 0x0203:  # WM_LBUTTONDBLCLK
                    # Windows sends up, dblclk, up: the first up already
                    # toggled, so drop the second instead of toggling back.
                    swallow_up = True
                    return 0
                if msg == 0x0202 and swallow_up:  # WM_LBUTTONUP
                    swallow_up = False
                    return 0
                return orig_notify(wparam, lparam) if orig_notify else 0

            handlers[_w32.WM_NOTIFY] = notify_wrapper

        threading.Thread(target=self._refresh_loop, daemon=True,
                         name="suprbar-refresh").start()

        # Once-per-launch update check (background, non-blocking, opt-out).
        if config.get_pref("updates.check_on_launch", True) and updater.is_updatable():
            def _bg_update_check():
                try:
                    st = updater.check_for_update()
                    if st.get("available") and self._icon:
                        try:
                            self._icon.notify(
                                "supr.bar update available",
                                f"v{st.get('latest')} — open supr.bar to update")
                        except Exception:
                            pass
                        self._icon.update_menu()   # surface the "Update to vX" item
                except Exception:
                    log.debug("background update check failed", exc_info=True)
            threading.Thread(target=_bg_update_check, daemon=True,
                             name="suprbar-update-check").start()

        self._icon.run()
