"""Local HTTP server for the supr.bar flyout.

Routes:
  GET  /                            static index.html
  GET  /app.js, /styles.css         static
  GET  /api/ping                    liveness
  GET  /api/today[?refresh=1]       aggregated today summary (all sources)
  GET  /api/config                  current config (key fingerprint only)
  POST /api/config                  update config (JSON body)
  POST /api/config/test-key         test admin key (JSON body: {"key": "..."})
  POST /api/open-path               open a path under ~ in OS default app
  POST /api/quit                    request app shutdown
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import aggregator, config
from .providers import anthropic_api as p_anthropic_api

STATIC_DIR = Path(__file__).parent / "static"

# /api/today is cached briefly server-side. Provider-level caches still apply.
_today_cache: dict = {"data": None, "ts": 0.0}
_TODAY_TTL = 4.0


def today_cached() -> dict:
    now = time.time()
    if _today_cache["data"] and (now - _today_cache["ts"]) < _TODAY_TTL:
        return _today_cache["data"]
    data = aggregator.today()
    _today_cache["data"] = data
    _today_cache["ts"] = now
    return data


def invalidate_today_cache() -> None:
    _today_cache["data"] = None
    _today_cache["ts"] = 0.0
    p_anthropic_api.invalidate_cache()


# ---------- shutdown callback hook ----------

_quit_callback = None


def set_quit_callback(fn) -> None:
    global _quit_callback
    _quit_callback = fn


def _trigger_quit() -> None:
    if _quit_callback:
        try:
            _quit_callback()
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    # ---- output helpers ----

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _serve_file(self, name: str, ctype: str):
        p = STATIC_DIR / name
        if not p.exists():
            return self._send(404, b"not found", "text/plain")
        return self._send(200, p.read_bytes(), ctype)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        try:
            data = self.rfile.read(length).decode("utf-8")
            return json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- routes ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self._serve_file("app.js", "application/javascript")
        if path == "/styles.css":
            return self._serve_file("styles.css", "text/css")

        if path == "/api/ping":
            return self._send_json(200, {"ok": True, "pid": os.getpid()})

        if path == "/api/today":
            if "refresh" in qs:
                invalidate_today_cache()
            return self._send_json(200, today_cached())

        if path == "/api/config":
            return self._send_json(200, _public_config())

        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            body = self._read_json_body()
            try:
                _apply_config_patch(body)
                return self._send_json(200, _public_config())
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})

        if path == "/api/config/test-key":
            body = self._read_json_body()
            key = (body.get("key") or "").strip()
            if not key:
                return self._send_json(400, {"ok": False, "error": "key required"})
            ok, msg = p_anthropic_api.test_connection(key)
            return self._send_json(200, {"ok": ok, "message": msg})

        if path == "/api/open-path":
            body = self._read_json_body()
            target = (body.get("p") or "").strip()
            return self._send_json(200, {"opened": _open_path(target)})

        if path == "/api/quit":
            self._send_json(200, {"ok": True})
            threading.Thread(target=_trigger_quit, daemon=True).start()
            return

        self._send(404, b"not found", "text/plain")


# ---------- config endpoints support ----------

def _public_config() -> dict:
    """Config view safe to send to the UI: never the admin key plaintext."""
    cfg = config.load()
    key = config.get_admin_key() or ""
    return {
        "sources": {
            "local": cfg.get("sources", {}).get("local", {}),
            "anthropic_api": {
                "enabled": cfg.get("sources", {}).get("anthropic_api", {}).get("enabled", False),
                "has_key": bool(key),
                "key_fingerprint": _fingerprint(key) if key else None,
            },
        },
        "ui": cfg.get("ui", {}),
    }


def _fingerprint(key: str) -> str:
    if len(key) < 12:
        return "•" * len(key)
    return key[:10] + "…" + key[-4:]


def _apply_config_patch(body: dict) -> None:
    """Apply a partial config update. Supports admin key + toggles."""
    # admin key (set/clear)
    if "anthropic_api_key" in body:
        v = body["anthropic_api_key"]
        if v is None or v == "":
            config.set_admin_key(None)
        else:
            if not isinstance(v, str):
                raise ValueError("anthropic_api_key must be a string")
            ok = config.set_admin_key(v.strip())
            if not ok:
                raise ValueError("failed to encrypt and store key")

    if "anthropic_api_enabled" in body:
        config.set_anthropic_enabled(bool(body["anthropic_api_enabled"]))

    if "pinned" in body:
        config.set_pinned(bool(body["pinned"]))

    if "start_on_login" in body:
        v = bool(body["start_on_login"])
        config.set_start_on_login(v)
        # apply to registry too
        bat = str(Path(__file__).resolve().parent.parent / "run.bat")
        config.apply_startup_setting(v, bat if v else None)

    invalidate_today_cache()


# ---------- path opener (sandboxed to home dir) ----------

def _open_path(target: str) -> bool:
    if not target:
        return False
    try:
        p = Path(target).expanduser().resolve()
    except (OSError, ValueError):
        return False
    home = Path.home().resolve()
    try:
        p.relative_to(home)
    except ValueError:
        return False
    if not p.exists():
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except OSError:
        return False


def _find_free_port(preferred: int = 47821) -> int:
    for port in (preferred, preferred + 1, preferred + 2, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("no port available")


def start_in_background() -> tuple[ThreadingHTTPServer, int, threading.Thread]:
    port = _find_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="suprbar-http")
    t.start()
    return httpd, port, t
