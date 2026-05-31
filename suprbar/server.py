"""Local HTTP server for the supr.bar flyout.

Routes:
  GET  /                            static index.html
  GET  /app.js, /styles.css         static
  GET  /api/ping                    liveness (used for single-instance check)
  GET  /api/today[?refresh=1]       aggregated today summary (all sources)
  GET  /api/range?key=…             usage for a time-range tab
  GET  /api/budgets                 spent-vs-limit for day/week/month
  GET  /api/config                  current config (key fingerprint only)
  POST /api/config                  update config (JSON body)
  POST /api/config/test-key         test admin key (JSON body: {"key": "..."})
  GET  /api/config/export           full public config snapshot
  POST /api/config/import           replace config (rejects plaintext keys)
  POST /api/config/reset            reset to defaults
  GET  /api/prefs, /api/prefs/schema  full preference tree + schema
  POST /api/prefs                   update preferences (dotted paths)
  POST /api/open-path               open a path under ~ in OS default app
  POST /api/quit                    request app shutdown
  GET  /api/version                 app version + build date
  GET  /api/health                  liveness + last scan + uptime
  GET  /api/diagnostics             python/platform/cache/log/source health
  GET  /report[.html]               30-day usage report page (relaxed CSP)
  GET  /api/report                  30-day report payload (JSON)
  POST /api/open-report             open the report in the default browser
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, aggregator, config, report, scanner
from .providers import anthropic_api as p_anthropic_api
from .providers import local as p_local

log = logging.getLogger("suprbar.server")

STATIC_DIR = Path(__file__).parent / "static"
GZIP_MIN_BYTES = 1024
_ALLOWED_KEYS: set[str] = {
    "anthropic_api_key",
    "anthropic_api_enabled",
    "pinned",
    "start_on_login",
}

# /api/today is cached briefly server-side. Provider-level caches still apply.
_today_cache: dict = {"data": None, "ts": 0.0}
_TODAY_TTL = 4.0

# /api/range cache — keyed by a fingerprint that includes filters so a config
# change invalidates automatically. 30s TTL → clicking between tabs is instant
# after the first miss for each tab.
_range_cache: dict[str, dict] = {}
_RANGE_TTL = 30.0

# Process-level state for diagnostics/health.
_PROCESS_STARTED = time.monotonic()
_PROCESS_STARTED_WALL = time.time()
_HTTP_PORT: int | None = None
_LAST_SCAN: dict = {"ts": 0.0, "ok": False, "elapsed_ms": 0}


def _now_monotonic() -> float:
    return time.monotonic()


def today_cached() -> dict:
    now = _now_monotonic()
    if _today_cache["data"] and (now - _today_cache["ts"]) < _TODAY_TTL:
        return _today_cache["data"]
    try:
        data = aggregator.today()
        _LAST_SCAN["ts"] = time.time()
        _LAST_SCAN["ok"] = True
        _LAST_SCAN["elapsed_ms"] = int(data.get("elapsed_ms", 0)) if isinstance(data, dict) else 0
    except Exception as e:  # noqa: BLE001
        log.exception("aggregator.today failed: %s", e)
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
    cfg = config.load()
    rng = cfg.get("range", {}) or {}
    proj = cfg.get("projects", {}) or {}
    fp = (
        key,
        custom_start or "",
        custom_end or "",
        rng.get("week_starts_on", "mon"),
        rng.get("day_boundary", "local"),
        bool(rng.get("rolling_24h", False)),
        bool(rng.get("include_weekends", True)),
        tuple(proj.get("allowlist") or []),
        tuple(proj.get("denylist")  or []),
        bool(proj.get("anonymize", False)),
    )
    cache_key = repr(fp)
    now = _now_monotonic()
    entry = _range_cache.get(cache_key)
    if entry and (now - entry["ts"]) < _RANGE_TTL:
        return entry["data"]
    data = scanner.range_summary(
        range_key=key,
        custom_start=custom_start,
        custom_end=custom_end,
        week_starts_on=rng.get("week_starts_on", "mon"),
        day_boundary=rng.get("day_boundary", "local"),
        rolling_24h=bool(rng.get("rolling_24h", False)),
        allowlist=list(proj.get("allowlist") or []),
        denylist=list(proj.get("denylist")  or []),
        anonymize=bool(proj.get("anonymize", False)),
        include_weekends=bool(rng.get("include_weekends", True)),
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

    def _send_common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _csp_header())

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
    ) -> None:
        encoded, ce = self._maybe_gzip(body)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(encoded)))
        if ce:
            self.send_header("Content-Encoding", ce)
            self.send_header("Vary", "Accept-Encoding")
        self._send_common_headers()
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

    def _send_html_with_csp(self, code: int, body: bytes, csp: str) -> None:
        """Send an HTML document with an explicit CSP override.

        Mirrors _send() (gzip + BrokenPipe guards) but writes headers manually
        so this route can relax the CSP without touching _send_common_headers().
        """
        encoded, ce = self._maybe_gzip(body)
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if ce:
            self.send_header("Content-Encoding", ce)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client (WebView2 page navigation, F5, shutdown) closed mid-write.
            pass

    def _serve_report(self):
        """Render static/report.html with the report JSON substituted in.

        Served with a relaxed CSP (inline script allowed) — see
        _report_csp_header(). This route does NOT go through _send().
        """
        template_path = STATIC_DIR / "report.html"
        if not template_path.exists():
            return self._send_error(404, "not_found", "report template not found")
        template = template_path.read_bytes().decode("utf-8")
        try:
            data = report_cached()
        except Exception as e:  # noqa: BLE001
            return self._send_error(500, "report_failed", str(e))
        # Escape "</" so a project/model name containing "</script>" can't break
        # out of the inline data literal (json.dumps does NOT escape it). Default
        # ensure_ascii already \u-escapes U+2028/U+2029. Values rendered into the
        # DOM are additionally HTML-escaped page-side (report.html `esc()`), so the
        # relaxed inline-script CSP has no attacker-reachable script path.
        payload = json.dumps(data).replace("</", "<\\/")
        html = template.replace("__SUPRBAR_REPORT_JSON__", payload)
        return self._send_html_with_csp(200, html.encode("utf-8"), _report_csp_header())

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

        if path in ("/report", "/report.html"):
            return self._serve_report()

        if path == "/api/report":
            try:
                return self._send_json(200, report_cached())
            except Exception as e:  # noqa: BLE001
                return self._send_error(500, "report_failed", str(e))

        if path == "/api/ping":
            return self._send_json(200, {"ok": True, "pid": os.getpid()})

        if path == "/api/version":
            return self._send_json(200, _version_payload())

        if path == "/api/health":
            return self._send_json(200, _health_payload())

        if path == "/api/diagnostics":
            return self._send_json(200, _diagnostics_payload())

        if path == "/api/today":
            if "refresh" in qs:
                invalidate_today_cache()
            try:
                data = today_cached()
            except Exception as e:  # noqa: BLE001
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

        if path == "/api/config":
            return self._send_json(200, _public_config())

        if path == "/api/config/export":
            return self._send_json(200, _export_config())

        if path == "/api/range":
            try:
                return self._send_json(200, _range_payload(qs))
            except Exception as e:  # noqa: BLE001
                return self._send_error(500, "range_failed", str(e))

        if path == "/api/budgets":
            try:
                return self._send_json(200, _budgets_payload())
            except Exception as e:  # noqa: BLE001
                return self._send_error(500, "budgets_failed", str(e))

        if path == "/api/prefs":
            # Return the full mutable preference tree (excluding secrets).
            return self._send_json(200, _prefs_payload())

        if path == "/api/prefs/schema":
            return self._send_json(200, _prefs_schema_payload())

        return self._send_error(404, "not_found", f"no route {path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            body = self._read_json_body()
            unknown = [k for k in body.keys() if k not in _ALLOWED_KEYS]
            if unknown:
                return self._send_error(
                    400, "unknown_keys",
                    f"unknown config keys: {', '.join(sorted(unknown))}",
                )
            try:
                _apply_config_patch(body)
            except ValueError as e:
                return self._send_error(400, "invalid_value", str(e))
            return self._send_json(200, _public_config())

        if path == "/api/config/test-key":
            body = self._read_json_body()
            key = (body.get("key") or "").strip()
            if not key:
                return self._send_error(400, "missing_key", "key required")
            ok, msg = p_anthropic_api.test_connection(key)
            return self._send_json(200, {"ok": ok, "message": msg})

        if path == "/api/config/import":
            body = self._read_json_body()
            try:
                _import_config(body)
            except ValueError as e:
                return self._send_error(400, "invalid_import", str(e))
            return self._send_json(200, _public_config())

        if path == "/api/config/reset":
            body = self._read_json_body()
            reset_key = bool(body.get("reset_key", False))
            config.reset(reset_key=reset_key)
            invalidate_today_cache()
            return self._send_json(200, _public_config())

        if path == "/api/prefs":
            body = self._read_json_body()
            if not isinstance(body, dict):
                return self._send_error(400, "bad_body", "JSON object required")
            updates = body.get("updates") if "updates" in body else body
            if not isinstance(updates, dict):
                return self._send_error(400, "bad_body", "updates must be a dict")
            try:
                applied = config.set_many(updates)
            except ValueError as e:
                return self._send_error(400, "invalid_value", str(e))
            if "ui.start_on_login" in applied:
                v = bool(applied["ui.start_on_login"])
                bat = str(Path(__file__).resolve().parent.parent / "run.bat")
                config.apply_startup_setting(v, bat if v else None)
            invalidate_today_cache()
            return self._send_json(200, {"applied": applied,
                                          "prefs": _prefs_payload()["prefs"]})

        if path == "/api/open-path":
            body = self._read_json_body()
            target = (body.get("p") or "").strip()
            return self._send_json(200, {"opened": _open_path(target)})

        if path == "/api/open-report":
            return self._send_json(200, {"opened": open_report_in_browser()})

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
            cwd=str(repo), capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        ts = (Path(__file__).parent / "__init__.py").stat().st_mtime
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return None


def _uptime_seconds() -> int:
    return int(_now_monotonic() - _PROCESS_STARTED)


def _log_file_path() -> str:
    return str(config.config_dir() / "suprbar.log")


def _health_payload() -> dict:
    return {
        "ok": True,
        "uptime_seconds": _uptime_seconds(),
        "last_scan": {
            "ts": _LAST_SCAN["ts"] or None,
            "ok": bool(_LAST_SCAN["ok"]),
            "elapsed_ms": int(_LAST_SCAN["elapsed_ms"]),
        },
    }


def _safe_self_test(provider) -> dict:
    """Call a provider's self_test() defensively for /api/diagnostics."""
    try:
        return provider.self_test()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "last_error": f"{type(e).__name__}: {e!s:.120}"}


def _diagnostics_payload() -> dict:
    cache_meta = None
    try:
        data = today_cached()
        if isinstance(data, dict):
            cache_meta = data.get("cache_meta")
    except Exception:  # noqa: BLE001
        cache_meta = None

    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "pid": os.getpid(),
        "port": _HTTP_PORT,
        "uptime_seconds": _uptime_seconds(),
        "started_at": _PROCESS_STARTED_WALL,
        "log_file": _log_file_path(),
        "config_dir": str(config.config_dir()),
        "config_path": str(config.config_path()),
        "cache_meta": cache_meta,
        "sources": {
            "local": _safe_self_test(p_local),
            "anthropic_api": _safe_self_test(p_anthropic_api),
        },
        "today_cache": {
            "has_data": _today_cache["data"] is not None,
            "age_seconds": (
                round(_now_monotonic() - _today_cache["ts"], 3)
                if _today_cache["ts"] else None
            ),
            "ttl_seconds": _TODAY_TTL,
        },
    }


def _public_config() -> dict:
    """Config view safe to send to the UI: never the admin key plaintext."""
    cfg = config.load()
    key = config.get_admin_key() or ""
    return {
        "schema_version": cfg.get("schema_version", 1),
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


def _export_config() -> dict:
    """Export the current config, scrubbing the encrypted key and surfacing
    only a fingerprint. Suitable for backup or transferring settings."""
    cfg = config.load()
    out = json.loads(json.dumps(cfg))  # deep copy
    src = out.setdefault("sources", {}).setdefault("anthropic_api", {})
    src.pop("admin_key_enc", None)
    key = config.get_admin_key() or ""
    src["key_fingerprint"] = _fingerprint(key) if key else None
    return out


def _import_config(payload: dict) -> None:
    """Replace config from an exported payload. Rejects plaintext keys."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    # Disallow any plaintext key smuggling.
    src = (payload.get("sources") or {}).get("anthropic_api") or {}
    if isinstance(src, dict):
        for forbidden in ("admin_key", "anthropic_api_key", "key", "api_key"):
            if forbidden in src and src[forbidden]:
                raise ValueError(f"plaintext keys not accepted (got '{forbidden}')")
    # Preserve existing encrypted key; never read it from the import payload.
    current = config.load()
    preserved_enc = (
        current.get("sources", {}).get("anthropic_api", {}).get("admin_key_enc", "")
    )

    incoming = json.loads(json.dumps(payload))  # deep copy
    src2 = incoming.setdefault("sources", {}).setdefault("anthropic_api", {})
    # Strip any incoming admin_key_enc; only the existing one is honored.
    src2.pop("admin_key_enc", None)
    src2.pop("key_fingerprint", None)
    src2["admin_key_enc"] = preserved_enc

    # Merge atop defaults to fill in anything missing, then save.
    from .config import _merge_defaults, _migrate  # type: ignore
    merged = _merge_defaults(_migrate(incoming))
    config.save(merged)
    invalidate_today_cache()


def _fingerprint(key: str) -> str:
    if not key:
        return ""
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    if len(key) < 12:
        return f"sha256:{h}"
    return f"{key[:6]}…{key[-4:]} (sha256:{h})"


def _apply_config_patch(body: dict) -> None:
    """Apply a partial config update. Supports admin key + toggles."""
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
        bat = str(Path(__file__).resolve().parent.parent / "run.bat")
        config.apply_startup_setting(v, bat if v else None)

    invalidate_today_cache()


# ---------- path opener (sandboxed to home dir) ----------

def _range_payload(qs: dict) -> dict:
    """Build a /api/range response using user range prefs as defaults."""
    cfg = config.load()
    rng = cfg.get("range", {})
    key = (qs.get("key") or [rng.get("default", "today")])[0]
    cs  = (qs.get("start") or [""])[0] or None
    ce  = (qs.get("end") or [""])[0] or None
    if "refresh" in qs:
        # caller is forcing a refresh; drop cached entry for this fingerprint
        # (range_cached re-computes when entry is missing).
        _range_cache.clear()
    return range_cached(key, cs, ce)


def _budgets_payload() -> dict:
    """Build a /api/budgets response using user budget prefs."""
    cfg = config.load()
    b = cfg.get("budgets", {}) or {}
    daily   = float(b.get("daily_limit",   0.0) or 0.0)
    weekly  = float(b.get("weekly_limit",  0.0) or 0.0)
    monthly = float(b.get("monthly_limit", 0.0) or 0.0)
    alert_pct = int(b.get("alert_at_pct", 80) or 80)
    week_starts = cfg.get("range", {}).get("week_starts_on", "mon")
    s = scanner.budgets_summary(
        daily, weekly, monthly,
        week_starts_on=week_starts,
        allowlist=config.project_allowlist(),
        denylist=config.project_denylist(),
    )
    # add alert flags
    for window in ("daily", "weekly", "monthly"):
        s[window]["alerting"] = s[window]["limit"] > 0 and s[window]["pct"] >= alert_pct
    s["alert_pct"] = alert_pct
    return s


def _prefs_payload() -> dict:
    """Return mutable preferences (no admin key plaintext)."""
    cfg = config.load()
    public = json.loads(json.dumps(cfg))
    # never expose the encrypted blob
    if "sources" in public and "anthropic_api" in public["sources"]:
        public["sources"]["anthropic_api"].pop("admin_key_enc", None)
        public["sources"]["anthropic_api"]["has_key"] = config.has_admin_key()
    return {"prefs": public, "schema_version": cfg.get("schema_version", 1)}


def _prefs_schema_payload() -> dict:
    """Return the SCHEMA dict in a form the UI can render generically."""
    out = []
    for path, (typ, arg) in config.SCHEMA.items():
        entry: dict = {"path": path, "type": typ}
        if typ == "enum":
            entry["options"] = list(arg)
        elif typ in ("int", "float") and arg is not None:
            entry["min"], entry["max"] = arg
        # default value pulled from current config (which is defaults-merged)
        entry["default"] = config.get_pref(path)
        out.append(entry)
    return {"settings": out}


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
    except Exception:  # noqa: BLE001
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
