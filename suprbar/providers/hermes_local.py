"""Hermes (Nous Research) usage tracking — reads ~/.hermes/sessions/sessions.json.

The Hermes agent stores per-session token + cost tracking in a flattened JSON
object keyed by session_key. Each session carries:

  * input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
  * estimated_cost_usd
  * updated_at / created_at
  * display_name, platform (telegram/discord/cli)

This provider folds that data into the same shape the aggregator expects so
Hermes usage shows alongside Claude Code in the flyout.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("suprbar.hermes_local")

HERMES_SESSIONS = Path.home() / ".hermes" / "sessions" / "sessions.json"

_last_fetch_ts: float = 0.0
_last_error: str | None = None
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0


def _load_sessions(force: bool = False) -> dict[str, Any]:
    """Parse sessions.json, cached for 10s."""
    global _cache, _cache_ts
    now = time.time()
    if not force and _cache and (now - _cache_ts) < 10.0:
        return _cache
    path = HERMES_SESSIONS
    if not path.exists():
        _cache = {}
        _cache_ts = now
        return _cache
    try:
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, json.JSONDecodeError) as e:
        log.debug("hermes sessions parse: %s", e)
        raw = {}
    _cache = raw
    _cache_ts = now
    return raw


def today_summary() -> dict[str, Any]:
    """Return today's Hermes usage in aggregator source shape."""
    global _last_fetch_ts, _last_error
    now = datetime.now().astimezone()
    today_str = now.date().isoformat()

    try:
        sessions = _load_sessions(force=True)
        _last_fetch_ts = time.time()
        _last_error = None
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e!s:.160}"
        return _empty_source(_last_error)

    cost_today = 0.0
    tokens_today = {"input": 0, "output": 0,
                    "cache_5m": 0, "cache_1h": 0, "cache_read": 0}
    messages_today = 0
    live_sessions: list[dict[str, Any]] = []
    by_model: dict[str, dict[str, float]] = {}
    by_project: dict[str, dict[str, Any]] = {}

    for key, sess in sessions.items():
        if not isinstance(sess, dict):
            continue
        # Check if session was active today
        updated = sess.get("updated_at", "")
        if updated and updated[:10] != today_str:
            # Not today — skip for the "today" flyout
            continue

        inp = int(sess.get("input_tokens", 0) or 0)
        out = int(sess.get("output_tokens", 0) or 0)
        cr = int(sess.get("cache_read_tokens", 0) or 0)
        cw = int(sess.get("cache_write_tokens", 0) or 0)
        cost = float(sess.get("estimated_cost_usd", 0.0) or 0.0)

        cost_today += cost
        tokens_today["input"] += inp
        tokens_today["output"] += out
        tokens_today["cache_read"] += cr
        tokens_today["cache_1h"] += cw  # write tokens tracked as 1h cache
        messages_today += 1

        # Per-model tracking (Hermes stores the model in the origin or we infer)
        model = _model_from_session(sess)
        if model not in by_model:
            by_model[model] = {"cost": 0.0, "messages": 0, "tokens": 0, "cache_read": 0}
        by_model[model]["cost"] += cost
        by_model[model]["messages"] += 1
        by_model[model]["tokens"] += inp + out + cr + cw
        by_model[model]["cache_read"] += cr

        # Per-platform tracking (treat platform as "project")
        platform = sess.get("platform", "hermes")
        display = sess.get("display_name", key)
        proj_name = f"{platform}: {display}" if display else platform
        if proj_name not in by_project:
            by_project[proj_name] = {"cost": 0.0, "messages": 0, "tokens": 0, "models": set()}
        by_project[proj_name]["cost"] += cost
        by_project[proj_name]["messages"] += 1
        by_project[proj_name]["tokens"] += inp + out + cr + cw
        by_project[proj_name]["models"].add(model)

        # Live session if updated within 2 minutes
        try:
            upd_dt = datetime.fromisoformat(updated)
            age = (now - upd_dt).total_seconds()
        except (ValueError, TypeError):
            age = 99999
        if age < 120:
            live_sessions.append({
                "id": key,
                "project": proj_name,
                "path": str(HERMES_SESSIONS),
                "started_at": sess.get("created_at"),
                "last_activity": updated,
                "live": True,
                "model": model,
                "cost_today": round(cost, 4),
                "messages_today": 1,
                "burn_rate_usd_per_hour": round(cost / max(age / 3600.0, 0.01), 4) if age > 0 else 0.0,
            })

    by_model_list = [
        {"model": m, "cost": round(v["cost"], 4), "messages": int(v["messages"]),
         "tokens": int(v["tokens"]), "cache_read": int(v["cache_read"])}
        for m, v in by_model.items()
    ]

    by_project_list = [
        {"project": p, "cost": round(v["cost"], 4), "messages": int(v["messages"]),
         "tokens": int(v["tokens"]), "models": sorted(v["models"])}
        for p, v in by_project.items() if v["messages"] > 0
    ]
    by_project_list.sort(key=lambda p: -p["cost"])

    live_sessions.sort(key=lambda s: -s["cost_today"])

    return {
        "id": "hermes",
        "label": "Hermes · local",
        "ok": True,
        "error": None,
        "cost_today": round(cost_today, 4),
        "tokens_today": tokens_today,
        "messages_today": messages_today,
        "updated_at": now.isoformat(timespec="seconds"),
        "extras": {
            "by_model": by_model_list,
            "by_project": by_project_list,
            "live_sessions": live_sessions,
            "sessions_today": len([s for s in sessions.values()
                                   if isinstance(s, dict)
                                   and s.get("updated_at", "")[:10] == today_str]),
            "projects_today": len(by_project_list),
            "top_model_today": max(by_model_list, key=lambda m: m["messages"])["model"]
                               if by_model_list else None,
        },
    }


def _model_from_session(sess: dict) -> str:
    """Best-effort model name from a Hermes session.

    Hermes doesn't store the model name in sessions.json directly.
    We infer from the origin platform and any context clues.
    """
    # Hermes sessions on this machine use deepseek-v4-pro
    # In the future Hermes might store this explicitly
    return "deepseek-v4-pro"


def _empty_source(error: str) -> dict[str, Any]:
    return {
        "id": "hermes",
        "label": "Hermes · local",
        "ok": False,
        "error": error,
        "cost_today": 0.0,
        "tokens_today": {"input": 0, "output": 0,
                         "cache_5m": 0, "cache_1h": 0, "cache_read": 0},
        "messages_today": 0,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "extras": {},
    }


def self_test() -> dict[str, Any]:
    """Lightweight diagnostics for /api/diagnostics."""
    age: float | None = None
    if _last_fetch_ts:
        age = round(time.time() - _last_fetch_ts, 3)
    return {
        "ok": _last_error is None and HERMES_SESSIONS.exists(),
        "last_fetch_age_seconds": age,
        "last_error": _last_error,
        "fingerprint": str(HERMES_SESSIONS),
    }
