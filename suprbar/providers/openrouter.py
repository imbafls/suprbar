"""OpenRouter source — account-wide actual spend via the OpenRouter API.

Endpoint (Bearer key, sk-or-…):
  GET https://openrouter.ai/api/v1/credits
    → {"data": {"total_credits": <purchased USD>, "total_usage": <used USD>}}

That number is cumulative across all keys/devices on the account — exactly
the "all-device actuals" layer — but it has no day buckets. "Today" is
therefore derived by persisting a daily baseline: on the first poll of each
local day we snapshot current cumulative usage, and report today's cost as
(current − baseline).

Honest limitation: supr.bar can only see usage increments that happen while
it is running. Spend between "supr.bar last saw the account" and midnight is
not retro-attributed to the old day; the first poll of a new day resets the
baseline, so overnight usage lands on the new day's sum. Treat the OpenRouter
card as "spend since supr.bar last rolled over", not a billing export.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__, config

log = logging.getLogger("suprbar.openrouter")

BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT_SECONDS = 20
CACHE_TTL_SECONDS = 60.0

_RETRY_BACKOFFS = (0.5, 1.0, 2.0)

_cache_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0

_last_fetch_ts: float = 0.0
_last_error: str | None = None


# ---------------------------------------------------------------- state ----

def _state_path() -> Path:
    return config.config_dir() / "openrouter_state.json"


def _load_state() -> dict[str, Any]:
    try:
        raw = json.loads(_state_path().read_text("utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.debug("openrouter state save failed: %s", e)


def advance_state(state: dict[str, Any], today: str,
                  total_usage: float) -> tuple[dict[str, Any], float]:
    """Pure: roll the daily baseline and return (state, today_cost).

    ``state`` is {day: "YYYY-MM-DD", usage_at_start: float}. If the stored
    day isn't today (first poll ever, or a new day began), the baseline
    resets to the current cumulative usage — today's cost then reflects
    only spend observed from now on (see module docstring).
    """
    if state.get("day") != today:
        state = {"day": today, "usage_at_start": total_usage}
    delta = total_usage - float(state.get("usage_at_start", 0.0) or 0.0)
    return state, max(0.0, delta)


# ------------------------------------------------------------------ HTTP ----

def _http_get_once(path: str, api_key: str) -> dict:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("user-agent", f"suprbar/{__version__}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(path: str, api_key: str) -> dict:
    last_exc: Exception | None = None
    for attempt, backoff in enumerate(_RETRY_BACKOFFS):
        try:
            return _http_get_once(path, api_key)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code < 500:
                raise
            log.info("OpenRouter attempt %d/%d HTTP %d; backing off %.1fs",
                     attempt + 1, len(_RETRY_BACKOFFS), e.code, backoff)
        except urllib.error.URLError as e:
            last_exc = e
            log.info("OpenRouter attempt %d/%d network %s; backing off %.1fs",
                     attempt + 1, len(_RETRY_BACKOFFS), e.reason, backoff)
        except TimeoutError as e:
            last_exc = e
            time.sleep(backoff)
            continue
        time.sleep(backoff)
    try:
        return _http_get_once(path, api_key)
    except Exception as e:
        if last_exc is not None:
            raise last_exc from e
        raise


def fetch_credits(api_key: str) -> dict[str, float]:
    """Return {total_credits, total_usage} in USD for the account."""
    raw = _http_get("/credits", api_key)
    data = raw.get("data") or {}
    return {
        "total_credits": float(data.get("total_credits", 0.0) or 0.0),
        "total_usage": float(data.get("total_usage", 0.0) or 0.0),
    }


# ----------------------------------------------------------------- main ----

def _empty_result(error: str | None = None) -> dict[str, Any]:
    return {
        "id": "openrouter",
        "label": "OpenRouter",
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
    """Return today's OpenRouter delta-spend in aggregator source shape."""
    global _cache, _cache_ts, _last_fetch_ts, _last_error

    with _cache_lock:
        if _cache is not None and (time.time() - _cache_ts) < CACHE_TTL_SECONDS:
            return _cache

    out = _empty_result()
    key = config.get_source_key("openrouter")
    if not key:
        out["error"] = "no key configured"
        _cache = out
        _cache_ts = time.time()
        return out

    try:
        credits = fetch_credits(key)
        today = datetime.now().astimezone().date().isoformat()
        state, cost_today = advance_state(_load_state(), today,
                                          credits["total_usage"])
        _save_state(state)
        out["ok"] = True
        out["error"] = None
        out["cost_today"] = round(cost_today, 4)
        out["extras"] = {
            "lifetime_usage": round(credits["total_usage"], 4),
            "total_credits": round(credits["total_credits"], 4),
            "since": state.get("usage_at_start"),
        }
        _last_fetch_ts = time.time()
        _last_error = None
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            msg = str(e)
        out["error"] = f"HTTP {e.code}: {msg}"
        _last_error = out["error"]
        log.warning("OpenRouter HTTP error: %s", out["error"])
    except urllib.error.URLError as e:
        out["error"] = f"network: {e.reason!s:.120}"
        _last_error = out["error"]
        log.warning("OpenRouter network error: %s", out["error"])
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        out["error"] = f"{type(e).__name__}: {e!s:.120}"
        _last_error = out["error"]
        log.warning("OpenRouter error: %s", out["error"])

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
        "ok": _last_error is None and config.get_source_key("openrouter") is not None,
        "last_fetch_age_seconds": age,
        "last_error": _last_error,
        "fingerprint": "openrouter:/credits",
    }


def test_connection(api_key: str) -> tuple[bool, str]:
    """Settings-UI Test button. Doesn't save anything."""
    try:
        credits = fetch_credits(api_key)
        return True, (f"ok — ${credits['total_usage']:.2f} used of "
                      f"${credits['total_credits']:.2f} credits")
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
