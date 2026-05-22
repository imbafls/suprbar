"""System tray icon — gradient 'S', toggles the popout, pin checkbox, quit.

The icon is rendered at 256x256 for crispness and downsampled to 64x64 via
LANCZOS so the "S" glyph stays sharp at any tray DPI. A live-indicator
variant adds a green dot when an active Claude Code session is detected.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw, ImageFont

from . import config, server
from .popup import TrayBridge

log = logging.getLogger("suprbar.tray")

# Polling cadence for tooltip + idle/live state. Dropped from 60s -> 30s so
# the green-dot icon reflects new sessions quickly. We also force-refresh
# whenever the set of enabled source IDs changes between polls.
REFRESH_SECONDS = 30
PULSE_MS = 300


# ---------- Icon drawing ----------

def _gradient_image(size: int) -> Image.Image:
    """Diagonal accent->violet gradient. Generated once at high resolution."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    a = (91, 141, 239)   # --b-accent
    v = (140, 91, 239)   # --b-violet
    px = img.load()
    denom = 2 * (size - 1) if size > 1 else 1
    for y in range(size):
        for x in range(size):
            t = (x + y) / denom
            r = int(a[0] + (v[0] - a[0]) * t)
            g = int(a[1] + (v[1] - a[1]) * t)
            b = int(a[2] + (v[2] - a[2]) * t)
            px[x, y] = (r, g, b, 255)
    return img


def _load_bold_font(size: int) -> ImageFont.ImageFont:
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
                         radius=int(size * 0.19), fill=255)
    bg.putalpha(mask)

    # Draw text into a separate transparent overlay so we can sample at
    # high resolution and let the downscale anti-alias it.
    font = _load_bold_font(int(size * 0.62))
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    text = "S"
    try:
        bbox = od.textbbox((0, 0), text, font=font)
    except AttributeError:
        # Older Pillow fallback path
        try:
            tw, th = od.textsize(text, font=font)
            bbox = (0, 0, tw, th)
        except Exception:
            bbox = (0, 0, size // 2, size // 2)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1] - int(size * 0.035)
    fill = (255, 255, 255, 255) if not brighter else (255, 255, 255, 255)
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
    d.ellipse((x1, y1, x1 + dot, y1 + dot), fill=(58, 211, 138, 255))
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
    """Render a tray icon (256→64 px LANCZOS) with optional live + bright."""
    big_size = 256
    target_size = 64
    bg = _gradient_image(big_size)
    bg = _draw_S(bg)
    if live:
        bg = _add_live_dot(bg)
    if brighter:
        bg = _brighten(bg, 1.18)
    bg.thumbnail((target_size, target_size), Image.LANCZOS)
    return bg


def _make_icon() -> Image.Image:
    """Default idle icon used at startup before the first refresh."""
    return _render(live=False)


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


def _source_ids(data: dict) -> tuple[str, ...]:
    """Sorted tuple of enabled source IDs for change detection."""
    src = data.get("sources", []) or []
    return tuple(sorted(s.get("id", "") for s in src if s.get("ok")))


# ---------- TrayApp ----------

class TrayApp:
    def __init__(self, bridge: TrayBridge):
        self.bridge = bridge
        self._icon: pystray.Icon | None = None
        self._stop = threading.Event()
        self._last_live: bool | None = None
        self._last_src_ids: tuple[str, ...] = ()
        # Pre-render both variants once so updates only flip a reference.
        self._idle_icon = _render(live=False)
        self._live_icon = _render(live=True)
        self._idle_bright = _render(live=False, brighter=True)
        self._live_bright = _render(live=True, brighter=True)
        self._pulse_timer: threading.Timer | None = None

    # ---- click / menu callbacks ----

    def _on_click(self, icon, item):
        # Default action (single-click on Windows) + "Open supr.bar" menu item.
        self.bridge.toggle()

    def _on_refresh(self, icon, item):
        server.invalidate_today_cache()
        self._pulse_icon()
        self._update_tooltip()

    def _on_pin_toggle(self, icon, item):
        new = not config.is_pinned()
        config.set_pinned(new)
        if self._icon:
            self._icon.update_menu()

    def _is_pinned(self, item) -> bool:
        return config.is_pinned()

    def _on_settings(self, icon, item):
        try:
            self.bridge.open_with_settings()
        except Exception:
            log.exception("open settings failed")

    def _on_about(self, icon, item):
        try:
            if self._icon:
                self._icon.notify(
                    "supr.bar v0.1",
                    "Tray app for Claude Code usage",
                )
        except Exception:
            log.exception("notify failed")

    def _on_quit(self, icon, item):
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
                    self._icon.notify("supr.bar", f"Popup {state}")
                except Exception:
                    pass
        except Exception:
            log.exception("middle-click toggle failed")

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
        """Swap the tray icon to the live/idle variant when state changes."""
        live = bool(data.get("active"))
        if live != self._last_live and self._icon:
            try:
                self._icon.icon = self._live_icon if live else self._idle_icon
            except Exception:
                pass
        self._last_live = live

    def _update_tooltip(self):
        try:
            data = server.today_cached()
            self._apply_live_state(data)
            if self._icon:
                self._icon.title = _format_tooltip(data)
            # Track active sources so we can force a refresh on change.
            self._last_src_ids = _source_ids(data)
        except Exception as e:
            if self._icon:
                self._icon.title = f"supr.bar — error: {e!s:.60}"

    def _check_source_changed(self) -> bool:
        """Peek at the data without invalidating; if source IDs changed,
        return True so the caller knows to force a refresh."""
        try:
            data = server.today_cached()
            return _source_ids(data) != self._last_src_ids
        except Exception:
            return False

    def _refresh_loop(self):
        self._update_tooltip()
        while not self._stop.is_set():
            if self._stop.wait(REFRESH_SECONDS):
                return
            # Detect newly-enabled / disabled sources and bust the cache so the
            # next poll reflects the change immediately.
            if self._check_source_changed():
                server.invalidate_today_cache()
            else:
                server.invalidate_today_cache()
            self._update_tooltip()

    # ---- run ----

    def run(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open supr.bar", self._on_default, default=True),
            pystray.MenuItem("Refresh now", self._on_refresh),
            pystray.MenuItem("Pin (don't auto-hide)", self._on_pin_toggle,
                             checked=self._is_pinned),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings…", self._on_settings),
            pystray.MenuItem("About supr.bar", self._on_about),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )
        self._icon = pystray.Icon(
            "suprbar",
            self._idle_icon,
            "supr.bar — loading…",
            menu,
        )

        # Bind double-click and middle-click via pystray's _on_notify hook.
        # The signature is (icon, button, time) on Windows; we read the
        # button value to distinguish middle vs left-double.
        if sys.platform == "win32":
            _orig_notify = getattr(self._icon, "_on_notify", None)

            def notify_wrapper(wparam, lparam):
                try:
                    # lparam low word is the mouse message. WM_LBUTTONDBLCLK
                    # = 0x0203 ; WM_MBUTTONDOWN = 0x0207.
                    msg = lparam & 0xFFFF
                    if msg == 0x0203:  # double left click
                        self._on_default(self._icon, None)
                        return
                    if msg == 0x0207:  # middle button down
                        self._on_middle(self._icon, None)
                        return
                except Exception:
                    pass
                if callable(_orig_notify):
                    try:
                        _orig_notify(wparam, lparam)
                    except Exception:
                        pass

            try:
                # Only override if the backend exposes _on_notify so we don't
                # break other platforms.
                if _orig_notify is not None:
                    self._icon._on_notify = notify_wrapper  # type: ignore[attr-defined]
            except Exception:
                pass

        threading.Thread(target=self._refresh_loop, daemon=True,
                         name="suprbar-refresh").start()
        self._icon.run()
