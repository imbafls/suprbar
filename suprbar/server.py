"""Local HTTP server for the supr.bar flyout.

Routes:
  GET  /, /app.js, /styles.css, /mini.html, /mini.js, /mini.css   pages
  GET  /api/ping                    liveness
  GET  /api/today[?refresh=1]       aggregated today summary (all sources)
  GET  /api/range?key=…[&refresh=1] usage for a range tab (7d / 30d / 90d)
  GET  /api/settings                the five settings + key fingerprints
  POST /api/settings                {"settings": {path: value}, "keys": {source: key}}
  POST /api/settings/test-key       {"source": …, "key": …} → {ok, message}
  POST /api/quit                    request app shutdown
  GET  /api/version                 app version + build date
  GET  /report[.html]               30-day usage report page (relaxed CSP)
  GET  /api/report                  30-day report payload (JSON)
  POST /api/open-report             open the report in the default browser
  GET  /api/update/status           cached update status (no network)
  POST /api/update/check            re-check GitHub for a new release
  POST /api/update/apply            download + launch installer, then quit
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, aggregator, config, report, scanner, updater
from .providers import anthropic_api as p_anthropic_api
from .providers import openai as p_openai
from .providers import openrouter as p_openrouter
from datetime import UTC

log = logging.getLogger("suprbar.server")

STATIC_DIR = Path(__file__).parent / "static"
GZIP_MIN_BYTES = 1024

# /api/today is cached briefly server-side. Provider-level caches still apply.
_today_cache: dict = {"data": None, "ts": 0.0}
_TODAY_TTL = 4.0
# Idle backoff: with no live session there is nothing new to show, so the
# cache can breathe — but it must stay under the pages' 30 s idle poll, or a
# poll can be served a result older than one interval (stale numbers).
_TODAY_TTL_IDLE = 25.0
# Single-flight: without this, every HTTP thread + the tray refresh loop can
# miss the TTL simultaneously and all run a full aggregator scan at once.
_today_lock = threading.Lock()

# /api/range cache — keyed by a fingerprint that includes filters so a config
# change invalidates automatically. 60s TTL → clicking between tabs is instant
# after the first miss for each tab (range scans are the expensive ones).
_range_cache: dict[str, dict] = {}
_RANGE_TTL = 60.0

# Process-level state for diagnostics/health.
_HTTP_PORT: int | None = None
_LAST_SCAN: dict = {"ts": 0.0, "ok": False, "elapsed_ms": 0}


def _now_monotonic() -> float:
    return time.monotonic()


def _today_ttl() -> float:
    """Short TTL while a session is live, longer TTL when idle."""
    data = _today_cache.get("data")
    if isinstance(data, dict) and (data.get("active")
                                   or data.get("live_sessions")):
        return _TODAY_TTL
    return _TODAY_TTL_IDLE


def today_cached() -> dict:
    with _today_lock:
        now = _now_monotonic()
        if _today_cache["data"] and (now - _today_cache["ts"]) < _today_ttl():
            return _today_cache["data"]
        try:
            data = aggregator.today()
            _LAST_SCAN["ts"] = time.time()
            _LAST_SCAN["ok"] = True
            _LAST_SCAN["elapsed_ms"] = int(data.get("elapsed_ms", 0)) if isinstance(data, dict) else 0
        except Exception:
            log.exception("aggregator.today failed")
            _LAST_SCAN["ts"] = time.time()
            _LAST_SCAN["ok"] = False
            raise
        _today_cache["data"] = data
        _today_cache["ts"] = now
        return data


def invalidate_today_cache() -> None:
    _today_cache["data"] = None
    _today_cache["ts"] = 0.0
    _range_cache.clear()
    _report_cache["data"] = None
    _report_cache["ts"] = 0.0
    p_anthropic_api.invalidate_cache()
    p_openrouter.invalidate_cache()
    p_openai.invalidate_cache()


def invalidate_today_only() -> None:
    """Bust just the /api/today cache (tray poll path).

    Kept separate from invalidate_today_cache() so the periodic tray refresh
    doesn't also nuke the range/report/provider caches — those are expensive
    to rebuild and don't need busting every 30s.
    """
    _today_cache["data"] = None
    _today_cache["ts"] = 0.0


# /report + /api/report payload cache. build_report() runs TWO full range scans
# (current + previous 30d) and range_summary is NOT memoized, so without this
# every browser refresh of the report re-scans all of ~/.claude. Invalidated
# alongside the today/range caches on any config change.
_report_cache: dict = {"data": None, "ts": 0.0}
_REPORT_TTL = 30.0


def report_cached() -> dict:
    now = _now_monotonic()
    if _report_cache["data"] and (now - _report_cache["ts"]) < _REPORT_TTL:
        return _report_cache["data"]
    data = report.build_report()
    _report_cache["data"] = data
    _report_cache["ts"] = now
    return data


def range_cached(key: str, custom_start: str | None, custom_end: str | None) -> dict:
    """Return a cached range payload or compute + cache one."""
    fp = (key, custom_start or "", custom_end or "")
    cache_key = repr(fp)
    now = _now_monotonic()
    entry = _range_cache.get(cache_key)
    if entry and (now - entry["ts"]) < _RANGE_TTL:
        return entry["data"]
    data = scanner.range_summary(
        range_key=key,
        custom_start=custom_start,
        custom_end=custom_end,
    )
    _range_cache[cache_key] = {"data": data, "ts": now}
    return data


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


# ---------- response helpers ----------

def _csp_header() -> str:
    # Fully local/offline: no remote fonts or scripts. Everything is same-origin.
    return (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )


def _report_csp_header() -> str:
    # The report page is a self-contained document with inline <script> and
    # inline <style>; the strict CSP (no script-src) would block it. This
    # relaxation applies ONLY to GET /report — every other route keeps the
    # strict header from _csp_header(). Still same-origin/offline otherwise.
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default stderr logging
        pass

    # ---- output helpers ----

    def _send_common_headers(self, csp: str | None = None) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp or _csp_header())

    def _maybe_gzip(self, body: bytes) -> tuple[bytes, str | None]:
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        if "gzip" in ae and len(body) >= GZIP_MIN_BYTES:
            return gzip.compress(body), "gzip"
        return body, None

    def _send(
        self,
        code: int,
        body: bytes,
        ctype: str,
        *,
        extra_headers: dict[str, str] | None = None,
        csp: str | None = None,
    ) -> None:
        encoded, ce = self._maybe_gzip(body)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(encoded)))
        if ce:
            self.send_header("Content-Encoding", ce)
            self.send_header("Vary", "Accept-Encoding")
        self._send_common_headers(csp)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client (WebView2 page navigation, F5, shutdown) closed mid-write.
            pass

    def _send_json(self, code: int, obj: dict, *, etag: str | None = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        extra = {"ETag": etag} if etag else None
        self._send(code, body, "application/json", extra_headers=extra)

    def _send_304(self, etag: str) -> None:
        self.send_response(304)
        self.send_header("ETag", etag)
        self._send_common_headers()
        self.end_headers()

    def _send_error(self, code: int, err_code: str, message: str) -> None:
        self._send_json(code, _error(err_code, message))

    def _serve_file(self, name: str, ctype: str):
        p = STATIC_DIR / name
        if not p.exists():
            return self._send_error(404, "not_found", "static file not found")
        return self._send(200, p.read_bytes(), ctype)

    def _serve_report(self):
        """Render static/report.html with the report JSON substituted in.

        Served with a relaxed CSP (inline script allowed) via _send()'s ``csp``
        override — see _report_csp_header().
        """
        template_path = STATIC_DIR / "report.html"
        if not template_path.exists():
            return self._send_error(404, "not_found", "report template not found")
        template = template_path.read_bytes().decode("utf-8")
        try:
            data = report_cached()
        except Exception as e:
            return self._send_error(500, "report_failed", str(e))
        # Escape "</" so a project/model name containing "</script>" can't break
        # out of the inline data literal (json.dumps does NOT escape it). Default
        # ensure_ascii already \u-escapes U+2028/U+2029. Values rendered into the
        # DOM are additionally HTML-escaped page-side (report.html `esc()`), so the
        # relaxed inline-script CSP has no attacker-reachable script path.
        payload = json.dumps(data).replace("</", "<\\/")
        html = template.replace("__SUPRBAR_REPORT_JSON__", payload)
        return self._send(200, html.encode("utf-8"),
                          "text/html; charset=utf-8", csp=_report_csp_header())

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        try:
            data = self.rfile.read(length).decode("utf-8")
            return json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return {}

    def _origin_allowed(self) -> bool:
        """CSRF guard for state-changing POSTs.

        The flyout + report are served same-origin from 127.0.0.1, so legitimate
        requests are same-origin; a malicious web page the user visits in any
        browser hitting our localhost port is cross-site. We reject when the
        browser tells us the request is cross-site (``Sec-Fetch-Site``) or when
        an ``Origin`` is present whose host isn't loopback. Non-browser clients
        send neither header and are allowed — a local process can act directly
        anyway, so CSRF protection only needs to stop browser-driven requests.
        """
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                host = urlparse(origin).hostname
            except ValueError:
                return False
            if host not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

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
        if path == "/mini.html":
            return self._serve_file("mini.html", "text/html; charset=utf-8")
        if path == "/mini.js":
            return self._serve_file("mini.js", "application/javascript")
        if path == "/mini.css":
            return self._serve_file("mini.css", "text/css")

        if path in ("/report", "/report.html"):
            return self._serve_report()

        if path == "/api/report":
            try:
                return self._send_json(200, report_cached())
            except Exception as e:
                return self._send_error(500, "report_failed", str(e))

        if path == "/api/ping":
            return self._send_json(200, {"ok": True, "pid": os.getpid()})

        if path == "/api/version":
            return self._send_json(200, _version_payload())

        if path == "/api/update/status":
            return self._send_json(200, _update_status_payload())

        if path == "/api/today":
            if "refresh" in qs:
                invalidate_today_cache()
            try:
                data = today_cached()
            except Exception as e:
                return self._send_error(500, "aggregator_failed", str(e))
            body = json.dumps(data).encode("utf-8")
            etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
            inm = self.headers.get("If-None-Match")
            if inm and inm == etag:
                return self._send_304(etag)
            return self._send(
                200, body, "application/json",
                extra_headers={"ETag": etag},
            )

        if path == "/api/settings":
            return self._send_json(200, _settings_payload())

        if path == "/api/range":
            try:
                return self._send_json(200, _range_payload(qs))
            except Exception as e:
                return self._send_error(500, "range_failed", str(e))

        return self._send_error(404, "not_found", f"no route {path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # CSRF: reject browser cross-origin POSTs before any mutating route runs.
        # Matters most for /api/update/apply (download+install+quit) and /api/quit.
        if not self._origin_allowed():
            return self._send_error(403, "forbidden", "cross-origin request rejected")

        if path == "/api/settings":
            body = self._read_json_body()
            if not isinstance(body, dict):
                return self._send_error(400, "bad_body", "JSON object required")
            try:
                _apply_settings(body)
            except ValueError as e:
                return self._send_error(400, "invalid_value", str(e))
            return self._send_json(200, _settings_payload())

        if path == "/api/settings/test-key":
            body = self._read_json_body()
            source = str(body.get("source") or "")
            tester = _KEY_TESTERS.get(source)
            if tester is None:
                return self._send_error(400, "unknown_source",
                                        f"no key for source {source!r}")
            key = str(body.get("key") or "").strip() or _stored_key(source) or ""
            if not key:
                return self._send_error(400, "missing_key", "key required")
            ok, msg = tester(key)
            return self._send_json(200, {"ok": ok, "message": msg})

        if path == "/api/open-report":
            return self._send_json(200, {"opened": open_report_in_browser()})

        if path == "/api/update/check":
            # Synchronous re-check (manual). Bounded by retry budget (~few s).
            st = updater.check_for_update()
            return self._send_json(200,
                {k: v for k, v in st.items() if not k.startswith("_")})

        if path == "/api/update/apply":
            if not updater.is_updatable():
                return self._send_error(
                    409, "not_frozen",
                    "auto-update is only available in the installed build")
            cst = updater.cached_status()
            if not cst or not cst.get("available"):
                return self._send_error(409, "no_update", "no update available")
            self._send_json(200, {"ok": True, "message": "update starting…"})
            # Reuse the quit hook: download_and_apply launches the installer then
            # calls _trigger_quit via the registered quit_fn. Worker thread so the
            # response flushes first (same pattern as /api/quit).
            threading.Thread(
                target=updater.download_and_apply,
                kwargs={"quit_fn": _trigger_quit},
                daemon=True,
            ).start()
            return

        if path == "/api/quit":
            self._send_json(200, {"ok": True})
            threading.Thread(target=_trigger_quit, daemon=True).start()
            return

        return self._send_error(404, "not_found", f"no route {path}")


# ---------- payloads ----------

def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _version_payload() -> dict:
    import sys

    return {
        "version": __version__,
        "build_date": _build_date(),
        "dev": not getattr(sys, "frozen", False),
    }


def _build_date() -> str | None:
    # Best-effort: try git, otherwise mtime of __init__.py.
    repo = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=str(repo), capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        ts = (Path(__file__).parent / "__init__.py").stat().st_mtime
        from datetime import datetime
        return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")
    except OSError:
        return None












def _update_status_payload() -> dict:
    """Public update status — strips internal underscore-prefixed keys."""
    st = updater.cached_status()
    if st is None:
        st = {"current": __version__, "latest": None, "available": False,
              "asset_name": None, "asset_url": None, "size": None,
              "notes_url": None, "error": None, "checked_at": None}
    return {k: v for k, v in st.items() if not k.startswith("_")}








def _fingerprint(key: str) -> str:
    if not key:
        return ""
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    if len(key) < 12:
        return f"sha256:{h}"
    return f"{key[:6]}…{key[-4:]} (sha256:{h})"




# ---------- path opener (sandboxed to home dir) ----------

# ---------- settings ----------

# Settings the UI may change (update bookkeeping stays internal).
_USER_SETTINGS = tuple(p for p in config.SCHEMA
                       if p not in ("updates.last_check", "updates.skip_version"))
_KEY_TESTERS = {
    "anthropic_api": p_anthropic_api.test_connection,
    "openrouter": p_openrouter.test_connection,
    "openai": p_openai.test_connection,
}
_settings_callback = None


def set_settings_callback(fn) -> None:
    """``fn(applied)`` runs after every settings change (tray: mini sync)."""
    global _settings_callback
    _settings_callback = fn


def _stored_key(source: str) -> str | None:
    if source == "anthropic_api":
        return config.get_admin_key()
    return config.get_source_key(source)


def _settings_payload() -> dict:
    """The five settings, key fingerprints (never plaintext) and version."""
    keys = {}
    for source in _KEY_TESTERS:
        k = _stored_key(source)
        keys[source] = _fingerprint(k) if k else ""
    return {
        "settings": {p: config.get_pref(p) for p in _USER_SETTINGS},
        "keys": keys,
        "version": __version__,
    }


def _apply_settings(body: dict) -> None:
    """Validate everything first, then write. Raises ValueError on bad input."""
    settings = body.get("settings") or {}
    keys = body.get("keys") or {}
    if not isinstance(settings, dict) or not isinstance(keys, dict):
        # ValueError, not TypeError: the route turns it into a 400.
        raise ValueError("settings and keys must be objects")  # noqa: TRY004
    for p in settings:
        if p not in _USER_SETTINGS:
            raise ValueError(f"unknown setting: {p}")
    for source, key in keys.items():
        if source not in _KEY_TESTERS or not isinstance(key, str):
            raise ValueError(f"bad key entry: {source}")
    applied = config.set_many(settings) if settings else {}
    for source, key in keys.items():
        key = key.strip()
        ok = (config.set_admin_key(key) if source == "anthropic_api"
              else config.set_source_key(source, key))
        if not ok:
            raise ValueError(f"could not store the {source} key")
        applied[f"keys.{source}"] = bool(key)
    if "ui.start_on_login" in applied:
        v = bool(applied["ui.start_on_login"])
        config.apply_startup_setting(v, _startup_command_target() if v else None)
    # Sources or keys changed what gets counted: drop every cached result.
    invalidate_today_cache()
    if _settings_callback is not None:
        try:
            _settings_callback(applied)
        except Exception:
            log.exception("settings callback failed")


def _startup_command_target() -> str | None:
    """What the HKCU Run entry should point at.

    Installed (frozen) builds: the exe itself — the old run.bat path resolved
    to ``_internal\\run.bat`` under the PyInstaller 6 layout, so the registry
    value pointed at a file that didn't exist and start-on-login silently
    failed. Source checkouts: run.bat next to the package, if present.
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    bat = Path(__file__).resolve().parent.parent / "run.bat"
    return str(bat) if bat.exists() else None


def _range_payload(qs: dict) -> dict:
    """Build a /api/range response (defaults to today)."""
    key = (qs.get("key") or ["today"])[0]
    cs  = (qs.get("start") or [""])[0] or None
    ce  = (qs.get("end") or [""])[0] or None
    if "refresh" in qs:
        # caller is forcing a refresh; drop cached entry for this fingerprint
        # (range_cached re-computes when entry is missing).
        _range_cache.clear()
    return range_cached(key, cs, ce)








def open_report_in_browser() -> bool:
    """Open the 30-day report in the user's default browser.

    The flyout WebView is only ~360px wide, so a real browser window is the
    right surface for the full report. Returns False if the server port is
    not yet known or the browser launch fails.
    """
    if _HTTP_PORT is None:
        return False
    url = f"http://127.0.0.1:{_HTTP_PORT}/report"
    try:
        return webbrowser.open(url)
    except Exception:
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


def start_in_background(
    preferred_port: int = 47821,
) -> tuple[ThreadingHTTPServer, int, threading.Thread]:
    global _HTTP_PORT
    port = _find_free_port(preferred_port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="suprbar-http")
    t.start()
    _HTTP_PORT = port
    return httpd, port, t
