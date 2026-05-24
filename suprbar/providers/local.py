"""Local Claude Code source: reads ~/.claude/projects/**/*.jsonl."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .. import scanner

# Module-level diagnostics for self_test(). Populated by today_summary().
_last_fetch_ts: float = 0.0
_last_error: str | None = None


def today_summary() -> dict[str, Any]:
    """Return today's local-source summary in the shape aggregator expects.

    Lifts breakdowns (by_project / by_model / hourly / sessions_today /
    projects_today / top_model_today / parse_errors) into ``extras`` so the
    aggregator can reuse them without re-scanning.
    """
    global _last_fetch_ts, _last_error
    try:
        raw = scanner.today_summary()
        _last_fetch_ts = time.time()
        _last_error = None
    except Exception as e:  # noqa: BLE001 — never let the local source kill the tray
        _last_error = f"{type(e).__name__}: {e!s:.160}"
        return {
            "id": "local",
            "label": "Claude Code · local",
            "ok": False,
            "error": _last_error,
            "cost_today": 0.0,
            "tokens_today": {"input": 0, "output": 0,
                             "cache_5m": 0, "cache_1h": 0, "cache_read": 0},
            "messages_today": 0,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "extras": {},
        }

    today = raw.get("today", {}) or {}
    tokens = {
        "input": today.get("input", 0),
        "output": today.get("output", 0),
        "cache_5m": today.get("cache_5m", 0),
        "cache_1h": today.get("cache_1h", 0),
        "cache_read": today.get("cache_read", 0),
    }
    return {
        "id": "local",
        "label": "Claude Code · local",
        "ok": True,
        "error": None,
        "cost_today": today.get("cost", 0.0),
        "tokens_today": tokens,
        "messages_today": today.get("messages", 0),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "extras": {
            "active": raw.get("active"),
            "last_session_seen": raw.get("last_session_seen"),
            "files_scanned": raw.get("files_scanned", 0),
            "files_reused": raw.get("files_reused", 0),
            "files_reparsed": raw.get("files_reparsed", 0),
            "parse_errors": raw.get("parse_errors", 0),
            "scan_ms": raw.get("scan_ms", 0),
            "by_project": raw.get("by_project", []),
            "by_model": raw.get("by_model", []),
            "hourly": raw.get("hourly", []),
            "sessions_today": raw.get("sessions_today", 0),
            "projects_today": raw.get("projects_today", 0),
            "top_model_today": raw.get("top_model_today"),
            "live_sessions": raw.get("live_sessions", []),
        },
    }


def self_test() -> dict[str, Any]:
    """Lightweight diagnostics for /api/diagnostics.

    Returns ``{ok, last_fetch_age_seconds, last_error, fingerprint}`` where
    fingerprint is a stable identifier for the data source (the resolved
    ~/.claude/projects path).
    """
    age: float | None = None
    if _last_fetch_ts:
        age = round(time.time() - _last_fetch_ts, 3)
    return {
        "ok": _last_error is None and scanner.CLAUDE_HOME.exists(),
        "last_fetch_age_seconds": age,
        "last_error": _last_error,
        "fingerprint": str(scanner.CLAUDE_HOME),
    }
