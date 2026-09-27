"""Tray-side handles for the window child processes (see windowhost.py).

The tray process never loads WebView2. The flyout is started on demand and
retired FLYOUT_IDLE_EXIT_SECONDS after it is hidden (a quick re-open reuses
it); the mini overlay's process exists only while the overlay is enabled.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from . import config

log = logging.getLogger("suprbar.windows")

# How long a hidden flyout keeps its process before it is ended. Re-opening
# within this window is instant; after it, a fresh child starts (~1s).
FLYOUT_IDLE_EXIT_SECONDS = 60.0
# How long a child gets to close its window before it is killed.
_STOP_GRACE_SECONDS = 5.0

_CREATE_NO_WINDOW = 0x08000000


def _child_cmd(kind: str, url: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--window", kind, "--url", url]
    exe = Path(sys.executable)
    # Prefer pythonw from a source checkout so the child gets no console.
    pythonw = exe.with_name("pythonw.exe")
    if exe.name.lower() == "python.exe" and pythonw.exists():
        exe = pythonw
    return [str(exe), "-m", "suprbar", "--window", kind, "--url", url]


class _Child:
    """One window child process plus its event reader."""

    def __init__(self, kind: str, url: str,
                 on_event: Callable[[str], None]):
        self.kind = kind
        self._url = url
        self._on_event = on_event
        self._proc: subprocess.Popen[bytes] | None = None
        self._stopping = False
        self._lock = threading.RLock()

    def alive(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None and not self._stopping

    def ensure(self) -> None:
        """Start the child unless a usable one is already running."""
        with self._lock:
            if self.alive():
                return
            old = self._proc
            if old is not None and old.poll() is None:
                # Still shutting down: let it finish before replacing it.
                try:
                    old.wait(timeout=_STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    old.kill()
            cmd = _child_cmd(self.kind, self._url)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=str(Path(__file__).resolve().parent.parent),
                    creationflags=_CREATE_NO_WINDOW,
                )
            except OSError:
                log.exception("could not start the %s window", self.kind)
                self._proc = None
                return
            self._proc = proc
            self._stopping = False
            threading.Thread(target=self._read_events, args=(proc,),
                             daemon=True,
                             name=f"suprbar-{self.kind}-events").start()

    def send(self, cmd: str) -> bool:
        with self._lock:
            p = self._proc
            if p is None or p.poll() is not None or p.stdin is None:
                return False
            try:
                p.stdin.write(cmd.encode("utf-8") + b"\n")
                p.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def stop(self, wait: bool = False) -> None:
        """Ask the child to exit; kill it if it doesn't within the grace."""
        with self._lock:
            p = self._proc
            if p is None or p.poll() is not None:
                return
            self._stopping = True
            self.send("exit")
            try:
                if p.stdin:
                    p.stdin.close()
            except OSError:
                pass

        def _reap() -> None:
            try:
                p.wait(timeout=_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning("%s window did not exit; killing it", self.kind)
                p.kill()

        if wait:
            _reap()
        else:
            threading.Thread(target=_reap, daemon=True,
                             name=f"suprbar-{self.kind}-reap").start()

    def join(self) -> None:
        """Wait for a stopping child (its reaper kills it after the grace)."""
        p = self._proc
        if p is None:
            return
        try:
            p.wait(timeout=_STOP_GRACE_SECONDS + 1.0)
        except subprocess.TimeoutExpired:
            pass

    def _read_events(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                event = raw.decode("utf-8", "ignore").strip()
                if not event:
                    continue
                try:
                    self._on_event(event)
                except Exception:
                    log.exception("%s event %r failed", self.kind, event)
        except (OSError, ValueError):
            pass


class MiniController:
    """The always-on-top overlay; its process runs only while enabled."""

    def __init__(self, url: str, flyout: FlyoutController):
        self._flyout = flyout
        self._child = _Child("mini", url, self._on_event)

    def show(self) -> None:
        self._child.ensure()
        self._child.send("show")

    def hide(self) -> None:
        self._child.stop()

    def is_visible(self) -> bool:
        return self._child.alive()


    def sync(self) -> None:
        """Match the overlay to the persisted mini.* prefs."""
        try:
            enabled = config.mini_enabled()
        except Exception:
            enabled = False
        if enabled and not self.is_visible():
            self.show()
        elif not enabled and self.is_visible():
            self.hide()

    def stop(self, wait: bool = False) -> None:
        self._child.stop(wait=wait)

    def join(self) -> None:
        self._child.join()

    def _on_event(self, event: str) -> None:
        if event == "open_flyout":
            self._flyout.show()
        elif event == "disable":
            config.set_pref("mini.enabled", False)
            self.hide()


class FlyoutController:
    """Tray-side handle for the flyout; same calls the tray always made."""

    def __init__(self, url: str, on_quit: Callable[[], None]):
        self._on_quit = on_quit
        self._child = _Child("flyout", url, self._on_event)
        self._idle_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self.mini = MiniController(url, self)

    # ---- tray-facing API ----

    def toggle(self) -> None:
        # A live child resolves toggle itself (it knows whether a blur just
        # hid it); a retired one has nothing to hide, so this opens it.
        if not self._child.alive() or not self._child.send("toggle"):
            self.show()

    def show(self) -> None:
        self._cancel_idle_exit()
        self._child.ensure()
        self._child.send("show")

    def open_with_settings(self) -> None:
        self._cancel_idle_exit()
        self._child.ensure()
        self._child.send("settings")

    def quit(self) -> None:
        # Both children close at once; each is killed after the grace period.
        self._cancel_idle_exit()
        self._child.stop()
        self.mini.stop()
        self._child.join()
        self.mini.join()

    # ---- child events ----

    def _on_event(self, event: str) -> None:
        if event == "shown":
            self._cancel_idle_exit()
        elif event == "hidden":
            self._schedule_idle_exit()
        elif event == "quit":
            threading.Thread(target=self._on_quit, daemon=True,
                             name="suprbar-quit").start()

    def _schedule_idle_exit(self) -> None:
        with self._timer_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            t = threading.Timer(FLYOUT_IDLE_EXIT_SECONDS, self._child.stop)
            t.daemon = True
            self._idle_timer = t
            t.start()

    def _cancel_idle_exit(self) -> None:
        with self._timer_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
