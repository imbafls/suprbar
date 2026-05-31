"""supr.bar — Windows tray app for Claude Code (+ Anthropic API) usage."""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import threading

from . import config, server, updater
from .popup import (
    TrayBridge,
    acquire_single_instance,
    release_single_instance,
    run as run_popup,
)
from .tray import TrayApp

DEFAULT_PORT = 47821


def setup_logging() -> None:
    # Env var wins (debug/dev); otherwise fall back to data.log_level pref.
    level_name = os.environ.get("SUPRBAR_LOG", "").upper()
    if not level_name:
        try:
            level_name = str(config.get_pref("data.log_level", "INFO")).upper()
        except Exception:
            level_name = "INFO"
    if level_name == "OFF":
        level = logging.CRITICAL + 10  # effectively silent
    elif level_name == "WARN":
        level = logging.WARNING
    else:
        level = getattr(logging, level_name, logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Drop any handlers a prior call left behind.
    for h in list(root.handlers):
        root.removeHandler(h)

    # Console handler.
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Rotating file handler in the config dir.
    try:
        log_dir = config.config_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "suprbar.log"
        fh = logging.handlers.RotatingFileHandler(
            str(log_file),
            maxBytes=1024 * 1024,
            backupCount=5,
            encoding="utf-8",
            delay=True,
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        logging.getLogger("suprbar").warning("file logging disabled: %s", e)


def main() -> int:
    setup_logging()
    log = logging.getLogger("suprbar")

    # Single-instance guard (Windows named mutex; a no-op on other platforms).
    # Acquire before binding a port or spawning threads so a duplicate launch
    # exits cleanly with no side effects. SUPRBAR_FORCE=1 bypasses it.
    if not acquire_single_instance():
        log.warning("another suprbar instance is already running "
                    "(set SUPRBAR_FORCE=1 to override) — exiting")
        return 0

    httpd, port, _ = server.start_in_background(DEFAULT_PORT)
    log.info("http server on 127.0.0.1:%d (pid=%d)", port, os.getpid())

    url = f"http://127.0.0.1:{port}/"
    bridge = TrayBridge()
    tray = TrayApp(bridge)

    shutdown_event = threading.Event()

    def shutdown_app(*_args):
        # Called by /api/quit (Alt+Q from the popup, or tray Quit menu)
        if shutdown_event.is_set():
            return
        shutdown_event.set()
        try:
            tray._on_quit(None, None)
        except Exception:
            pass

    server.set_quit_callback(shutdown_app)
    updater.set_quit_fn(shutdown_app)

    # Signal handling. SIGINT works on all platforms; SIGTERM only off-Windows.
    try:
        signal.signal(signal.SIGINT, shutdown_app)
    except (ValueError, OSError):
        pass
    if sys.platform != "win32":
        try:
            signal.signal(signal.SIGTERM, shutdown_app)
        except (ValueError, OSError):
            pass

    def on_webview_started():
        threading.Thread(target=tray.run, daemon=True,
                         name="suprbar-tray").start()

    try:
        run_popup(url, bridge, on_webview_started)
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    except Exception:
        log.exception("popup loop crashed")
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        try:
            tray._on_quit(None, None)
        except Exception:
            pass
        release_single_instance()

    return 0


if __name__ == "__main__":
    sys.exit(main())
