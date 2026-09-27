"""supr.bar — Windows tray app for Claude Code (+ Anthropic API) usage."""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading

from . import config, pricing, server, updater
from .instance import acquire_single_instance, release_single_instance

DEFAULT_PORT = 47821


def setup_logging() -> None:
    # INFO unless SUPRBAR_LOG overrides it (debugging).
    level_name = os.environ.get("SUPRBAR_LOG", "").upper() or "INFO"
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


def _terminate_stale_instances() -> None:
    """Kill leftover installed instances from a hung shutdown.

    Called right after the single-instance mutex is acquired: we are the
    primary instance now, so any other ``suprbar.exe`` is a zombie that
    survived its own quit (released the mutex, never exited) and can leave an
    always-on-top window stuck on screen with no way to close it. Developers
    running two instances on purpose can set SUPRBAR_FORCE=1 to opt out.
    """
    if sys.platform != "win32" or os.environ.get("SUPRBAR_FORCE") == "1":
        return
    log = logging.getLogger("suprbar")
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq suprbar.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    me = os.getpid()
    killed: list[int] = []
    for line in out.stdout.splitlines():
        cols = [c.strip().strip('"') for c in line.split('","')]
        if len(cols) < 2 or not cols[1].isdigit():
            continue
        pid = int(cols[1])
        if pid == me:
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=5, check=False)
            killed.append(pid)
        except (OSError, subprocess.SubprocessError):
            continue
    if killed:
        log.warning("terminated stale instance(s): %s",
                    ", ".join(str(p) for p in killed))


def _window_args(argv: list[str]) -> tuple[str, str] | None:
    """``--window <kind> --url <url>`` marks a window child process."""
    if "--window" not in argv:
        return None
    try:
        kind = argv[argv.index("--window") + 1]
        url = argv[argv.index("--url") + 1]
    except (ValueError, IndexError):
        return None
    return kind, url


def main() -> int:
    child = _window_args(sys.argv[1:])
    if child is not None:
        # A flyout / mini overlay child: no mutex, no server, no tray.
        from .windowhost import run as run_window
        return run_window(*child)

    setup_logging()
    log = logging.getLogger("suprbar")

    # Apply pricing overrides (local file now, hosted table in the background)
    # so first scans already use the freshest rates. Best-effort only.
    try:
        pricing.init_pricing()
    except Exception:
        log.debug("pricing init failed", exc_info=True)

    # Single-instance guard (Windows named mutex; a no-op on other platforms).
    # Acquire before binding a port or spawning threads so a duplicate launch
    # exits cleanly with no side effects. SUPRBAR_FORCE=1 bypasses it.
    if not acquire_single_instance():
        log.warning("another suprbar instance is already running "
                    "(set SUPRBAR_FORCE=1 to override) — exiting")
        return 0
    # We hold the mutex: any other installed instance is a hung leftover whose
    # windows would otherwise sit on screen forever.
    _terminate_stale_instances()

    httpd, port, _ = server.start_in_background(DEFAULT_PORT)
    log.info("http server on 127.0.0.1:%d (pid=%d)", port, os.getpid())

    url = f"http://127.0.0.1:{port}/"
    shutdown_event = threading.Event()

    from .tray import TrayApp
    from .windows import FlyoutController
    # Windows live in child processes (windows.py); this process never loads
    # WebView2, so it stays small while the flyout is closed.
    bridge = FlyoutController(url, on_quit=lambda: shutdown_app())
    tray = TrayApp(bridge)

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
    # Best-effort: clear leftover installer temp dirs from a prior update.
    threading.Thread(target=updater.cleanup_stale_downloads, daemon=True,
                     name="suprbar-upd-cleanup").start()

    # NOTE: the once-per-launch update check lives in tray.run() (gated on
    # updates.check_on_launch AND updater.is_updatable(), so source checkouts
    # never phone home). Don't add a second check here — that caused double
    # GitHub hits and duplicate notifications on installed builds.
    # The 6-hour periodic check below is the long-running-process safety net.

    # Periodic update check (every 6 hours) — so long-running suprbar processes
    # don't miss releases. First check after 6h to avoid stacking on the launch
    # check. Runs as a daemon; exits with the process.
    def _periodic_update_check():
        while not shutdown_event.is_set():
            shutdown_event.wait(6 * 3600)  # 6 hours
            if shutdown_event.is_set():
                break
            try:
                st = updater.check_for_update()
                if st.get("available"):
                    log.info("periodic update check found v%s", st.get("latest"))
            except Exception:
                pass
    threading.Thread(target=_periodic_update_check, daemon=True,
                     name="suprbar-upd-periodic").start()

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

    try:
        tray.run()  # blocks on the main thread until Quit
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    except Exception:
        log.exception("tray loop crashed")
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
