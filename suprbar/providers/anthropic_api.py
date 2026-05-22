"""Anthropic API source — hits the Admin API for org-wide usage + cost.

Endpoints used (require an admin-scoped key, sk-ant-admin01-…):
  GET /v1/organizations/cost_report?starting_at=&ending_at=&bucket_width=1d
  GET /v1/organizations/usage_report/messages?starting_at=&ending_at=
       &bucket_width=1h&group_by[]=model

Today bounds = user's local-day [00:00 .. 24:00) converted to UTC. We use 1h
buckets for usage so we can filter to local-day. Cost only supports 1d
buckets, so we fetch 3 UTC days and clip — close enough for a "today" number.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .. import config

log = logging.getLogger("suprbar.providers.anthropic_api")

BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_SECONDS = 15
CACHE_TTL_SECONDS = 60.0

_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}


def _today_bounds_utc() -> tuple[datetime, datetime]:
    now = datetime.now().astimezone()
    today = now.date()
    start_local = datetime(today.year, today.month, today.day, tzinfo=now.tzinfo)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get(path: str, params: dict, api_key: str) -> dict:
    qs = urllib.parse.urlencode(params, doseq=True)
    url = f"{BASE_URL}{path}?{qs}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("anthropic-version", ANTHROPIC_VERSION)
    req.add_header("x-api-key", api_key)
    req.add_header("user-agent", "suprbar/0.1")
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def _fetch_cost_today(api_key: str) -> tuple[float, dict]:
    """Returns (today_cost_usd, raw response). Snaps to UTC day buckets."""
    start, end = _today_bounds_utc()
    # Expand 1 day on each side to cover local-vs-UTC overlap, then clip.
    fetch_start = start - timedelta(days=1)
    fetch_end = end + timedelta(days=1)
    params = {
        "starting_at": _rfc3339(fetch_start),
        "ending_at": _rfc3339(fetch_end),
        "bucket_width": "1d",
        "limit": 7,
    }
    raw = _http_get("/v1/organizations/cost_report", params, api_key)
    total_cents = 0.0
    for bucket in raw.get("data", []):
        b_start = datetime.fromisoformat(
            bucket.get("starting_at", "").replace("Z", "+00:00"))
        b_end = datetime.fromisoformat(
            bucket.get("ending_at", "").replace("Z", "+00:00"))
        # Overlap fraction with [start, end]
        overlap_start = max(b_start, start)
        overlap_end = min(b_end, end)
        if overlap_end <= overlap_start:
            continue
        bucket_span = (b_end - b_start).total_seconds() or 1
        overlap = (overlap_end - overlap_start).total_seconds()
        frac = overlap / bucket_span
        for r in bucket.get("results", []):
            try:
                amt = float(r.get("amount", "0"))
            except (TypeError, ValueError):
                amt = 0.0
            total_cents += amt * frac
    return total_cents / 100.0, raw


def _fetch_usage_today(api_key: str) -> dict:
    """Returns dict with input/output/cache_5m/cache_1h/cache_read totals."""
    start, end = _today_bounds_utc()
    params = {
        "starting_at": _rfc3339(start),
        "ending_at": _rfc3339(end),
        "bucket_width": "1h",
        "limit": 168,
        "group_by[]": "model",
    }
    raw = _http_get("/v1/organizations/usage_report/messages", params, api_key)
    totals = {"input": 0, "output": 0, "cache_5m": 0, "cache_1h": 0, "cache_read": 0}
    msg_count = 0
    for bucket in raw.get("data", []):
        for r in bucket.get("results", []):
            totals["input"]      += int(r.get("uncached_input_tokens", 0) or 0)
            totals["output"]     += int(r.get("output_tokens", 0) or 0)
            cc = r.get("cache_creation") or {}
            totals["cache_5m"]   += int(cc.get("ephemeral_5m_input_tokens", 0) or 0)
            totals["cache_1h"]   += int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
            totals["cache_read"] += int(r.get("cache_read_input_tokens", 0) or 0)
            # Admin API doesn't return a "messages" count — leave 0 unless
            # added later via a different field.
    return {"tokens": totals, "messages": msg_count, "raw": raw}


def today_summary() -> dict:
    base = {
        "id": "anthropic_api",
        "label": "Anthropic API",
        "ok": False,
        "error": None,
        "cost_today": 0.0,
        "tokens_today": {"input": 0, "output": 0,
                         "cache_5m": 0, "cache_1h": 0, "cache_read": 0},
        "messages_today": 0,
        "extras": {},
    }

    if not config.anthropic_enabled():
        base["error"] = "disabled"
        return base
    api_key = config.get_admin_key()
    if not api_key:
        base["error"] = "no admin key configured"
        return base

    now = time.time()
    with _cache_lock:
        if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
            return _cache["data"]

    try:
        usage = _fetch_usage_today(api_key)
        cost, _ = _fetch_cost_today(api_key)
        base["ok"] = True
        base["cost_today"] = round(cost, 4)
        base["tokens_today"] = usage["tokens"]
        base["messages_today"] = usage["messages"]
        base["extras"] = {"buckets": len(usage["raw"].get("data", []))}
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")[:200] if hasattr(e, "read") else str(e)
        base["error"] = f"HTTP {e.code}: {msg}"
        log.warning("Admin API HTTP error: %s", base["error"])
    except urllib.error.URLError as e:
        base["error"] = f"network: {e.reason!s:.120}"
        log.warning("Admin API network error: %s", base["error"])
    except (TimeoutError, json.JSONDecodeError, ValueError) as e:
        base["error"] = f"{type(e).__name__}: {e!s:.120}"
        log.warning("Admin API error: %s", base["error"])

    with _cache_lock:
        _cache["ts"] = now
        _cache["data"] = base
    return base


def invalidate_cache() -> None:
    with _cache_lock:
        _cache["ts"] = 0.0
        _cache["data"] = None


def test_connection(api_key: str) -> tuple[bool, str]:
    """Used by the settings UI Test button. Doesn't save anything."""
    try:
        start, end = _today_bounds_utc()
        _http_get("/v1/organizations/cost_report", {
            "starting_at": _rfc3339(start - timedelta(days=1)),
            "ending_at": _rfc3339(end),
            "bucket_width": "1d",
            "limit": 1,
        }, api_key)
        return True, "ok"
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, f"HTTP {e.code}: {body[:200]}"
    except urllib.error.URLError as e:
        return False, f"network: {e.reason!s:.120}"
    except Exception as e:  # noqa: BLE001 - surface any other failure
        return False, f"{type(e).__name__}: {e!s:.120}"
