"""System tray icon — gradient 'S', toggles the popout, pin checkbox, quit."""

from __future__ import annotations

import logging
import threading

import pystray
from PIL import Image, ImageDraw, ImageFont

from . import config, server
from .popup import TrayBridge

log = logging.getLogger("suprbar.tray")

REFRESH_SECONDS = 60


# ---------- Icon drawing ----------

def _gradient_image(size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    a = (91, 141, 239)   # --b-accent
    v = (140, 91, 239)   # --b-violet
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            r = int(a[0] + (v[0] - a[0]) * t)
            g = int(a[1] + (v[1] - a[1]) * t)
            b = int(a[2] + (v[2] - a[2]) * t)
            px[x, y] = (r, g, b, 255)
    return img


def _make_icon() -> Image.Image:
    size = 64
    bg = _gradient_image(size)
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=255)
    bg.putalpha(mask)
    d = ImageDraw.Draw(bg)
    font = None
    for candidate in ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(candidate, 40)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    text = "S"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1] - 2
    d.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))
    return bg


# ---------- Tooltip ----------

def _format_tooltip(data: dict) -> str:
    today = data.get("today", {})
    cost = today.get("cost", 0.0)
    active = data.get("active")
    sources = data.get("sources", [])
    src_bits = []
    for s in sources:
        if s.get("ok") and s.get("cost_today", 0) > 0:
            src_bits.append(f"{s['label'].split('·')[0].strip()} ${s['cost_today']:.2f}")
    src_line = "  ·  ".join(src_bits) if src_bits else ""
    if active:
        return (
            f"supr.bar — live\n"
            f"${cost:,.2f} today · {active.get('messages_today', 0)} msgs\n"
            + (src_line + "\n" if src_line else "")
            + f"{(active.get('project') or '')[:40]}"
        ).rstrip()
    return (
        f"supr.bar — idle\n"
        f"${cost:,.2f} today · no live session"
        + (f"\n{src_line}" if src_line else "")
    )


# ---------- TrayApp ----------

class TrayApp:
    def __init__(self, bridge: TrayBridge):
        self.bridge = bridge
        self._icon: pystray.Icon | None = None
        self._stop = threading.Event()

    def _on_click(self, icon, item):
        self.bridge.toggle()

    def _on_refresh(self, icon, item):
        server.invalidate_today_cache()
        self._update_tooltip()

    def _on_pin_toggle(self, icon, item):
        new = not config.is_pinned()
        config.set_pinned(new)
        if self._icon:
            self._icon.update_menu()

    def _is_pinned(self, item) -> bool:
        return config.is_pinned()

    def _on_quit(self, icon, item):
        self._stop.set()
        try:
            self.bridge.quit()
        except Exception:
            pass
        try:
            if self._icon:
                self._icon.stop()
        except Exception:
            pass

    def _update_tooltip(self):
        try:
            data = server.today_cached()
            if self._icon:
                self._icon.title = _format_tooltip(data)
        except Exception as e:
            if self._icon:
                self._icon.title = f"supr.bar — error: {e!s:.60}"

    def _refresh_loop(self):
        self._update_tooltip()
        while not self._stop.is_set():
            if self._stop.wait(REFRESH_SECONDS):
                return
            server.invalidate_today_cache()
            self._update_tooltip()

    def run(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open supr.bar", self._on_click, default=True),
            pystray.MenuItem("Refresh now", self._on_refresh),
            pystray.MenuItem("Pin (don't auto-hide)", self._on_pin_toggle,
                             checked=self._is_pinned),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )
        self._icon = pystray.Icon(
            "suprbar",
            _make_icon(),
            "supr.bar — loading…",
            menu,
        )
        threading.Thread(target=self._refresh_loop, daemon=True,
                         name="suprbar-refresh").start()
        self._icon.run()
