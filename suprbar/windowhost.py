"""Child process hosting one WebView2 window: the flyout or the mini overlay.

Why a separate process: WebView2 costs ~150 MB (a browser, GPU, utility and
renderer process tree plus the .NET/WinForms host) and pywebview can't tear
it down without ending its whole GUI loop. Hosting each window in its own
short-lived child means the tray process never loads WebView2 at all, and
the memory is returned to the system whenever the window is gone.

Protocol (one word per line, UTF-8):
  tray -> child (stdin):  show | hide | toggle | settings | exit
  child -> tray (stdout): shown | hidden | quit | open_flyout | disable
EOF on stdin means the tray process is gone, so the child exits too.

Launched as ``suprbar --window flyout|mini --url <base url>``.
"""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import BinaryIO

from . import config

log = logging.getLogger("suprbar.windowhost")

# WebView2 teardown has been seen to hang; a child must never outlive its
# window by more than this.
_EXIT_WATCHDOG_SECONDS = 3.0


def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "suprbar"


def _setup_logging(kind: str) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    level_name = os.environ.get("SUPRBAR_LOG", "").upper() or "INFO"
    if level_name == "OFF":
        root.setLevel(logging.CRITICAL + 10)
        return
    root.setLevel(logging.WARNING if level_name == "WARN"
                  else getattr(logging, level_name, logging.INFO))
    try:
        log_dir = config.config_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        # One file per window kind: at most one child of each kind runs at a
        # time, so rotation never races another process.
        fh = logging.handlers.RotatingFileHandler(
            str(log_dir / f"suprbar-{kind}.log"), maxBytes=256 * 1024,
            backupCount=1, encoding="utf-8", delay=True)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    except OSError:
        pass


def _std_stream(which: int) -> BinaryIO:
    """Binary stdin (0) / stdout (1) for the command channel.

    A windowed frozen build (and pythonw) can leave sys.stdin/sys.stdout as
    None even when the parent passed pipes, so fall back to the inherited
    OS handles.
    """
    s = sys.stdin if which == 0 else sys.stdout
    if s is not None and hasattr(s, "buffer"):
        return s.buffer
    if sys.platform != "win32":
        raise RuntimeError("window child started without stdio pipes")
    import msvcrt
    get_std = ctypes.windll.kernel32.GetStdHandle
    get_std.restype = ctypes.c_void_p
    handle = get_std(-10 if which == 0 else -11)
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if which == 0 else 0)
    return os.fdopen(fd, "rb" if which == 0 else "wb", buffering=0)


def run(kind: str, url: str) -> int:
    _setup_logging(kind)
    cmd_in = _std_stream(0)
    evt_out = _std_stream(1)
    out_lock = threading.Lock()

    def notify(event: str) -> None:
        with out_lock:
            try:
                evt_out.write(event.encode("utf-8") + b"\n")
                evt_out.flush()
            except (OSError, ValueError):
                pass  # tray process gone; stdin EOF will end us

    import webview

    if kind == "flyout":
        from . import popup
        bridge = popup.FlyoutBridge(notify)
        window = popup.build_window(url, bridge)
        handlers = {
            "show": bridge.show,
            "hide": bridge.hide,
            "toggle": bridge.toggle,
            "settings": bridge.open_with_settings,
        }
    elif kind == "mini":
        from . import mini
        mbridge = mini.MiniBridge(notify)
        window = mini.build_window(url, mbridge)
        handlers = {
            "show": mbridge.show,
            "hide": mbridge.hide,
        }
    else:
        log.error("unknown window kind %r", kind)
        return 2

    def exit_now() -> None:
        t = threading.Timer(_EXIT_WATCHDOG_SECONDS, lambda: os._exit(0))
        t.daemon = True
        t.start()
        try:
            window.destroy()
        except Exception:
            os._exit(0)

    def read_commands() -> None:
        try:
            for raw in cmd_in:
                cmd = raw.decode("utf-8", "ignore").strip()
                if cmd == "exit":
                    break
                fn = handlers.get(cmd)
                if fn is None:
                    continue
                try:
                    fn()
                except Exception:
                    log.exception("%s command %r failed", kind, cmd)
        except (OSError, ValueError):
            pass
        exit_now()

    def started() -> None:
        threading.Thread(target=read_commands, daemon=True,
                         name=f"suprbar-{kind}-cmds").start()

    try:
        # A persistent profile (not pywebview's default private temp dir):
        # the flyout and overlay share one WebView2 browser process, starts
        # are faster, and no EBWebView temp folders are left behind.
        webview.start(started, debug=False, private_mode=False,
                      storage_path=str(_data_dir() / "webview"))
    except Exception as e:
        msg = (str(e) or "").lower()
        if kind == "flyout" and (
                isinstance(e, webview.WebViewException)
                or any(s in msg for s in ("webview2", "edge", "runtime",
                                          "0x80070005", "no module"))):
            log.error("WebView2 runtime missing or failed to start: %s", e)
            from .popup import _show_webview2_install_dialog
            _show_webview2_install_dialog()
        else:
            log.exception("webview.start failed")
        return 1
    return 0
