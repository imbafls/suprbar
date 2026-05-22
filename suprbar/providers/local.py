"""Local Claude Code source: reads ~/.claude/projects/**/*.jsonl."""

from __future__ import annotations

from .. import scanner


def today_summary() -> dict:
    raw = scanner.today_summary()
    today = raw.get("today", {})
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
        "extras": {
            "active": raw.get("active"),
            "last_session_seen": raw.get("last_session_seen"),
            "files_scanned": raw.get("files_scanned", 0),
            "scan_ms": raw.get("scan_ms", 0),
        },
    }
