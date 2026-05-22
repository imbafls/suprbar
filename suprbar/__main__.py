"""supr.bar — Windows tray app for Claude Code (+ Anthropic API) usage."""

from __future__ import annotations

import logging
import os
import sys
import threading

from . import server
from .popup import TrayBridge, run as run_popup
from .tray import TrayApp


def setup_logging() -> None:
    level = os.environ.get("SUPRBAR_LOG", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    setup_logging()
    log = logging.getLogger("suprbar")

    httpd, port, _ = server.start_in_background()
    log.info("http server on 127.0.0.1:%d", port)

    url = f"http://127.0.0.1:{port}/"
    bridge = TrayBridge()
    tray = TrayApp(bridge)

    def shutdown_app():
        # Called by /api/quit (Alt+Q from the popup, or tray Quit menu)
        try:
            tray._on_quit(None, None)
        except Exception:
            pass

    server.set_quit_callback(shutdown_app)

    def on_webview_started():
        threading.Thread(target=tray.run, daemon=True,
                         name="suprbar-tray").start()

    try:
        run_popup(url, bridge, on_webview_started)
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
