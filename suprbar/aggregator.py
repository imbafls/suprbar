"""Combine all data sources into a single today-summary for the flyout."""

from __future__ import annotations

import logging
import time

from . import config
from .providers import anthropic_api as p_anthropic_api
from .providers import local as p_local

log = logging.getLogger("suprbar.aggregator")


def _enabled_sources() -> list[str]:
    cfg = config.load()
    out = []
    sources = cfg.get("sources", {}) or {}
    if sources.get("local", {}).get("enabled", True):
        out.append("local")
    if sources.get("anthropic_api", {}).get("enabled", False):
        out.append("anthropic_api")
    return out


def today() -> dict:
    started = time.time()
    sources_data: list[dict] = []
    enabled = _enabled_sources()

    if "local" in enabled:
        try:
            sources_data.append(p_local.today_summary())
        except Exception as e:  # noqa: BLE001
            log.exception("local source failed")
            sources_data.append({
                "id": "local", "label": "Claude Code · local",
                "ok": False, "error": f"{type(e).__name__}: {e!s:.120}",
                "cost_today": 0.0,
                "tokens_today": {"input": 0, "output": 0,
                                 "cache_5m": 0, "cache_1h": 0, "cache_read": 0},
                "messages_today": 0, "extras": {},
            })

    if "anthropic_api" in enabled:
        try:
            sources_data.append(p_anthropic_api.today_summary())
        except Exception as e:  # noqa: BLE001
            log.exception("anthropic_api source failed")
            sources_data.append({
                "id": "anthropic_api", "label": "Anthropic API",
                "ok": False, "error": f"{type(e).__name__}: {e!s:.120}",
                "cost_today": 0.0,
                "tokens_today": {"input": 0, "output": 0,
                                 "cache_5m": 0, "cache_1h": 0, "cache_read": 0},
                "messages_today": 0, "extras": {},
            })

    total_cost = sum(s["cost_today"] for s in sources_data)
    total_tokens = {"input": 0, "output": 0,
                    "cache_5m": 0, "cache_1h": 0, "cache_read": 0}
    total_messages = 0
    for s in sources_data:
        for k in total_tokens:
            total_tokens[k] += s["tokens_today"].get(k, 0)
        total_messages += s.get("messages_today", 0) or 0

    # Lift the local source's "active" / "last_session_seen" to top level so
    # the flyout doesn't need to know about source schemas.
    active = None
    last_session_seen = None
    for s in sources_data:
        if s["id"] == "local":
            active = s["extras"].get("active")
            last_session_seen = s["extras"].get("last_session_seen")
            break

    return {
        "now": _now_iso(),
        "elapsed_ms": int((time.time() - started) * 1000),
        "today": {
            "cost": round(total_cost, 4),
            "messages": int(total_messages),
            **{k: int(v) for k, v in total_tokens.items()},
        },
        "sources": sources_data,
        "active": active,
        "last_session_seen": last_session_seen,
    }


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")
