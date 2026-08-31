"""OpenAI source — org-wide actual costs via the OpenAI Usage API.

Endpoint (Bearer admin key, sk-admin-…):
  GET https://api.openai.com/v1/organization/costs
        ?start_time=<unix>&end_time=<unix>&bucket_width=1d&limit=7

Returns USD amounts in UTC-day buckets; like the Anthropic provider we fetch
a one-day margin on each side and clip each bucket's overlap with the local
day, prorating by overlap fraction.

Admin keys are created in the OpenAI dashboard (Settings → API keys →
Administrator). Project-scoped keys cannot read org costs.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, UTC
from typing import Any

from .. import __version__, config

log = logging.getLogger("suprbar.openai")

BASE_URL = "https://api.openai.com"
TIMEOUT_SECONDS = 25
CACHE_TTL_SECONDS = 60.0

_RETRY_BACKOFFS = (0.5, 1.0, 2.0)

_cache_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0

_last_fetch_ts: float = 0.0
_last_error: str | None = None


# ------------------------------------------------------------------ HTTP ----

def _today_bounds_utc() -> tuple[datetime, datetime]:
    now = datetime.now().astimezone()
    start_local = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _http_get_once(path: str, params: dict, api_key: str) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{BASE_URL}{path}?{qs}", method="GET")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("user-agent", f"suprbar/{__version__}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(path: str, params: dict, api_key: str) -> dict:
    last_exc: Exception | None = None
    for attempt, backoff in enumerate(_RETRY_BACKOFFS):
        try:
            return _http_get_once(path, params, api_key)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code < 500:
                raise
            log.info("OpenAI attempt %d/%d HTTP %d; backing off %.1fs",
                     attempt + 1, len(_RETRY_BACKOFFS), e.code, backoff)
        except urllib.error.URLError as e:
            last_exc = e
            log.info("OpenAI attempt %d/%d network %s; backing off %.1fs",
                     attempt + 1, len(_RETRY_BACKOFFS), e.reason, backoff)
        except TimeoutError as e:
            last_exc = e
            time.sleep(backoff)
            continue
        time.sleep(backoff)
    try:
        return _http_get_once(path, params, api_key)
    except Exception as e:
        if last_exc is not None:
            raise last_exc from e
        raise


def _result_amount_usd(r: dict) -> float:
    """Extract USD from one cost-report result row (defensive: shape drifts)."""
    amount = r.get("amount") or {}
    val = amount.get("value")
    if isinstance(val, dict):          # {"currency": "usd", "value": "1.23"}
        val = val.get("value")
    try:
        return float(val or 0.0)
    except (TypeError, ValueError):
        return 0.0


def fetch_cost_today(api_key: str) -> tuple[float, dict]:
    """(today_cost_usd, raw) — UTC-day buckets clipped to the local day."""
    start, end = _today_bounds_utc()
    fetch_start = start - timedelta(days=1)
    fetch_end = end + timedelta(days=1)
    params = {
        "start_time": int(fetch_start.timestamp()),
        "end_time": int(fetch_end.timestamp()),
        "bucket_width": "1d",
        "limit": 7,
    }
    raw = _http_get("/v1/organization/costs", params, api_key)
    total = 0.0
    for bucket in raw.get("data", []):
        b_start = datetime.fromtimestamp(bucket.get("start_time", 0), tz=UTC)
        b_end = datetime.fromtimestamp(bucket.get("end_time", 0), tz=UTC)
        overlap_start = max(b_start, start)
        overlap_end = min(b_end, end)
        if overlap_end <= overlap_start:
            continue
        span = (b_end - b_start).total_seconds() or 1
        frac = (overlap_end - overlap_start).total_seconds() / span
        for r in bucket.get("results", []):
            total += _result_amount_usd(r) * frac
    return total, raw


# ----------------------------------------------------------------- main ----

def _empty_result(error: str | None = None) -> dict[str, Any]:
    return {
        "id": "openai",
        "label": "OpenAI API",
        "ok": False,
        "error": error,
        "cost_today": 0.0,
        "tokens_today": {"input": 0, "output": 0,
                         "cache_5m": 0, "cache_1h": 0, "cache_read": 0},
        "messages_today": 0,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "extras": {},
    }


def today_summary() -> dict[str, Any]:
    """Return today's OpenAI org cost in aggregator source shape."""
    global _cache, _cache_ts, _last_fetch_ts, _last_error

    with _cache_lock:
        if _cache is not None and (time.time() - _cache_ts) < CACHE_TTL_SECONDS:
            return _cache

    out = _empty_result()
    key = config.get_source_key("openai")
    if not key:
        out["error"] = "no key configured"
        _cache = out
        _cache_ts = time.time()
        return out

    try:
        cost, raw = fetch_cost_today(key)
        out["ok"] = True
        out["error"] = None
        out["cost_today"] = round(cost, 4)
        out["extras"] = {"buckets": len(raw.get("data", []))}
        _last_fetch_ts = time.time()
        _last_error = None
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            msg = str(e)
        out["error"] = f"HTTP {e.code}: {msg}"
        _last_error = out["error"]
        log.warning("OpenAI HTTP error: %s", out["error"])
    except urllib.error.URLError as e:
        out["error"] = f"network: {e.reason!s:.120}"
        _last_error = out["error"]
        log.warning("OpenAI network error: %s", out["error"])
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        out["error"] = f"{type(e).__name__}: {e!s:.120}"
        _last_error = out["error"]
        log.warning("OpenAI error: %s", out["error"])

    with _cache_lock:
        _cache = out
        _cache_ts = time.time()
    return out


def invalidate_cache() -> None:
    global _cache, _cache_ts
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0


def self_test() -> dict[str, Any]:
    age: float | None = None
    if _last_fetch_ts:
        age = round(time.time() - _last_fetch_ts, 3)
    return {
        "ok": _last_error is None and config.get_source_key("openai") is not None,
        "last_fetch_age_seconds": age,
        "last_error": _last_error,
        "fingerprint": "openai:/v1/organization/costs",
    }


def test_connection(api_key: str) -> tuple[bool, str]:
    """Settings-UI Test button. Doesn't save anything."""
    try:
        cost, _ = fetch_cost_today(api_key)
        return True, f"ok — ${cost:.2f} today"
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, f"HTTP {e.code}: {body[:200]}"
    except urllib.error.URLError as e:
        return False, f"network: {e.reason!s:.120}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e!s:.120}"
