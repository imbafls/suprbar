"""Combine all data sources into a single today-summary for the flyout.

Shape contract — additive only. The top-level keys ``now``, ``elapsed_ms``,
``today``, ``sources``, ``active``, ``last_session_seen`` are preserved
exactly. Everything else is appended.
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import datetime
from typing import Any

from . import config, scanner
from .providers import anthropic_api as p_anthropic_api
from .providers import hermes_local as p_hermes_local
from .providers import local as p_local
from .providers import openai as p_openai
from .providers import opencode as p_opencode
from .providers import openrouter as p_openrouter

log = logging.getLogger("suprbar.aggregator")

# Sources whose ``extras`` breakdowns (by_project / by_model / live_sessions /
# session counts) get folded into the top-level payload alongside the local
# source's own breakdowns.
_FOLD_SOURCE_IDS = ("hermes", "opencode")


def _enabled_sources() -> list[str]:
    cfg = config.load()
    out = []
    sources = cfg.get("sources", {}) or {}
    if sources.get("local", {}).get("enabled", True):
        out.append("local")
    if sources.get("anthropic_api", {}).get("enabled", False):
        out.append("anthropic_api")
    if sources.get("hermes", {}).get("enabled", True):
        out.append("hermes")
    if sources.get("opencode", {}).get("enabled", True):
        out.append("opencode")
    if sources.get("openrouter", {}).get("enabled", False):
        out.append("openrouter")
    if sources.get("openai", {}).get("enabled", False):
        out.append("openai")
    return out


def _empty_source_failure(source_id: str, label: str, err: Exception) -> dict[str, Any]:
    return {
        "id": source_id,
        "label": label,
        "ok": False,
        "error": f"{type(err).__name__}: {err!s:.120}",
        "cost_today": 0.0,
        "tokens_today": {"input": 0, "output": 0,
                         "cache_5m": 0, "cache_1h": 0, "cache_read": 0},
        "messages_today": 0,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "extras": {},
    }


def _merge_rows(rows: list[dict[str, Any]], *, key: str) -> list[dict[str, Any]]:
    """Merge breakdown rows sharing the same key value.

    Numeric fields are summed; list fields (``models``) are unioned. Used so
    the same model or project showing up in two sources (e.g. deepseek via
    Hermes and opencode) combines into one row instead of duplicating.
    Returns rows sorted by cost, descending.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in rows:
        k = str(r.get(key, "") or "")
        if not k:
            continue
        if k not in merged:
            merged[k] = copy.deepcopy(r)
            order.append(k)
            continue
        dst = merged[k]
        for f, v in r.items():
            if f == key:
                continue
            if isinstance(v, list):
                dst[f] = sorted(set(dst.get(f) or []) | set(v))
            elif isinstance(v, (int, float)):
                dst[f] = dst.get(f, 0) + v
    out = [merged[k] for k in order]
    out.sort(key=lambda r: -float(r.get("cost", 0) or 0))
    return out


def today() -> dict[str, Any]:
    """Build the unified today-payload consumed by /api/today and the popup."""
    started = time.time()
    sources_data: list[dict[str, Any]] = []
    enabled = _enabled_sources()

    if "local" in enabled:
        try:
            sources_data.append(p_local.today_summary())
        except Exception as e:
            log.exception("local source failed")
            sources_data.append(_empty_source_failure(
                "local", "Claude Code · local", e))

    if "anthropic_api" in enabled:
        try:
            sources_data.append(p_anthropic_api.today_summary())
        except Exception as e:
            log.exception("anthropic_api source failed")
            sources_data.append(_empty_source_failure(
                "anthropic_api", "Anthropic API", e))

    if "hermes" in enabled:
        try:
            sources_data.append(p_hermes_local.today_summary())
        except Exception as e:
            log.exception("hermes source failed")
            sources_data.append(_empty_source_failure(
                "hermes", "Hermes · local", e))

    if "opencode" in enabled:
        try:
            sources_data.append(p_opencode.today_summary())
        except Exception as e:
            log.exception("opencode source failed")
            sources_data.append(_empty_source_failure(
                "opencode", "opencode · local", e))

    if "openrouter" in enabled:
        try:
            sources_data.append(p_openrouter.today_summary())
        except Exception as e:
            log.exception("openrouter source failed")
            sources_data.append(_empty_source_failure(
                "openrouter", "OpenRouter", e))

    if "openai" in enabled:
        try:
            sources_data.append(p_openai.today_summary())
        except Exception as e:
            log.exception("openai source failed")
            sources_data.append(_empty_source_failure(
                "openai", "OpenAI API", e))

    # Defensive: make sure every source has an updated_at + extras dict so
    # downstream consumers can rely on the shape.
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    for s in sources_data:
        s.setdefault("updated_at", now_iso)
        s.setdefault("extras", {})

    # ---- aggregate top-level ``today`` totals across all sources ----
    total_cost = sum(s["cost_today"] for s in sources_data)
    total_tokens = {"input": 0, "output": 0,
                    "cache_5m": 0, "cache_1h": 0, "cache_read": 0}
    total_messages = 0
    for s in sources_data:
        for k in total_tokens:
            total_tokens[k] += s["tokens_today"].get(k, 0) or 0
        total_messages += s.get("messages_today", 0) or 0

    # Cache hit ratio: cache_read / (input + cache_read). 0..1, 0 if denom 0.
    denom = total_tokens["input"] + total_tokens["cache_read"]
    cache_hit_ratio = (total_tokens["cache_read"] / denom) if denom > 0 else 0.0

    # Cache savings — derive from the local source (where we know the
    # per-model rates). Falls back to opus rates inside pricing if model
    # info isn't available. We weight by the cache_read split across models
    # when by_model is present; otherwise we use a single bulk call.
    cache_savings_usd = _compute_cache_savings(sources_data,
                                               total_tokens["cache_read"])

    # ---- lift the local source's session info up to the top level ----
    active = None
    last_session_seen = None
    local_extras: dict[str, Any] = {}
    for s in sources_data:
        if s["id"] == "local":
            local_extras = s.get("extras", {}) or {}
            active = local_extras.get("active")
            last_session_seen = local_extras.get("last_session_seen")
            break

    # Burn rate is computed by the scanner now (active.burn_rate_usd_per_hour
    # already populated). Nothing else to do here.

    # ---- merge per-project / per-model / hourly from local extras ----
    by_project = list(local_extras.get("by_project", []) or [])
    by_model = list(local_extras.get("by_model", []) or [])
    hourly = list(local_extras.get("hourly", []) or [])
    sessions_today = int(local_extras.get("sessions_today", 0) or 0)
    projects_today = int(local_extras.get("projects_today", 0) or 0)
    top_model_today = local_extras.get("top_model_today")

    # Fold in non-local extras so the flyout shows all sources' usage.
    # by_model / by_project rows are merged by key so the same model or
    # project appearing in two sources combines instead of duplicating.
    by_model = _merge_rows(by_model, key="model")
    by_project = _merge_rows(by_project, key="project")
    for s in sources_data:
        if s["id"] not in _FOLD_SOURCE_IDS or not s.get("ok"):
            continue
        hx = s.get("extras", {}) or {}
        by_model = _merge_rows(
            by_model + list(hx.get("by_model", []) or []), key="model")
        by_project = _merge_rows(
            by_project + list(hx.get("by_project", []) or []), key="project")
        sessions_today += int(hx.get("sessions_today", 0) or 0)
        projects_today += int(hx.get("projects_today", 0) or 0)
        if not top_model_today:
            top_model_today = hx.get("top_model_today")

    # Parse errors surfaced across sources (so the UI / diagnostics
    # endpoint can flag malformed JSONL without rooting around).
    live_sessions: list[dict[str, Any]] = list(
        local_extras.get("live_sessions", []) or []
    )
    # Fold in non-local live sessions
    for s in sources_data:
        if s["id"] not in _FOLD_SOURCE_IDS or not s.get("ok"):
            continue
        hx = s.get("extras", {}) or {}
        live_sessions.extend(hx.get("live_sessions", []) or [])
    parse_errors = int(local_extras.get("parse_errors", 0) or 0)
    scan_source = str(scanner.CLAUDE_HOME)

    elapsed_ms = int((time.time() - started) * 1000)
    # Cache meta from the scanner — same data, surfaced at top level so
    # /api/diagnostics doesn't have to peek into ``sources[0].extras``.
    try:
        scan_meta = scanner.cache_meta()
    except Exception:
        scan_meta = {"files_reused": 0, "files_reparsed": 0,
                     "last_scan_ms": 0, "parse_errors": 0}

    today_payload = {
        "cost": round(total_cost, 4),
        "messages": int(total_messages),
        **{k: int(v) for k, v in total_tokens.items()},
        "cache_hit_ratio": round(cache_hit_ratio, 4),
        "cache_savings_usd": round(cache_savings_usd, 4),
        "projects_today": projects_today,
        "sessions_today": sessions_today,
        "top_model_today": top_model_today,
    }

    return {
        "now": now_iso,
        "elapsed_ms": elapsed_ms,
        "today": today_payload,
        "sources": sources_data,
        "active": active,
        "live_sessions": live_sessions,
        "last_session_seen": last_session_seen,
        "scan_source": scan_source,
        "insights": _build_insights(
            now_iso=now_iso,
            today=today_payload,
            active=active,
            live_sessions=live_sessions,
            by_project=by_project,
            parse_errors=parse_errors,
        ),
        # Additive: rich breakdowns + diagnostics.
        "by_project": by_project,
        "by_model": by_model,
        "hourly": hourly,
        "parse_errors": parse_errors,
        "cache_meta": {
            "files_reused": int(scan_meta.get("files_reused", 0)),
            "files_reparsed": int(scan_meta.get("files_reparsed", 0)),
            "last_scan_ms": int(scan_meta.get("last_scan_ms", elapsed_ms)),
        },
    }


def _build_insights(
    *,
    now_iso: str,
    today: dict[str, Any],
    active: dict[str, Any] | None,
    live_sessions: list[dict[str, Any]],
    by_project: list[dict[str, Any]],
    parse_errors: int,
) -> dict[str, Any]:
    """Small derived metrics that make the flyout immediately actionable."""
    cost = float(today.get("cost", 0.0) or 0.0)
    messages = int(today.get("messages", 0) or 0)
    cost_per_message = (cost / messages) if messages > 0 else 0.0

    burn = float((active or {}).get("burn_rate_usd_per_hour", 0.0) or 0.0)
    if not live_sessions:
        burn = 0.0
    projected = cost
    if burn > 0:
        try:
            now = datetime.fromisoformat(now_iso)
            seconds_left = 86400 - (
                (now.hour * 3600) + (now.minute * 60) + now.second
            )
            projected = cost + burn * max(seconds_left, 0) / 3600.0
        except ValueError:
            projected = cost

    top_project_share = 0.0
    if cost > 0 and by_project:
        top_project_share = max(float(p.get("cost", 0.0) or 0.0)
                                for p in by_project) / cost

    return {
        "live_count": len(live_sessions),
        "projected_today_cost": round(projected, 4),
        "cost_per_message": round(cost_per_message, 4),
        "cache_savings_usd": round(float(today.get("cache_savings_usd", 0.0) or 0.0), 4),
        "top_project_share": round(top_project_share, 4),
        "parse_errors": int(parse_errors or 0),
    }


def _compute_cache_savings(sources_data: list[dict[str, Any]],
                           total_cache_read: int) -> float:
    """Approximate USD saved by cache reads vs. uncached input.

    The local scanner now tracks ``cache_read`` per model, so we sum
    pricing.cache_savings_for at each model's own input rate — accurate for
    mixed Haiku/Sonnet/Opus usage instead of charging everything at the
    priciest model's rate. Any cache_read not attributable to a model (e.g.
    from the Admin API source) is estimated at the opus rate as a
    conservative upper bound.
    """
    if total_cache_read <= 0:
        return 0.0
    # Late import to dodge circular reference at module load time.
    from .pricing import cache_savings_over_models

    # Prefer per-model breakdowns from every source's extras — the local
    # scanner tracks cache_read per model, and opencode/hermes report it too.
    pairs: list[tuple[float, str]] = []
    attributed = 0
    for s in sources_data:
        for m in (s.get("extras", {}) or {}).get("by_model", []) or []:
            cr = int(m.get("cache_read", 0) or 0)
            if cr <= 0:
                continue
            pairs.append((cr, m.get("model", "")))
            attributed += cr

    leftover = max(0, total_cache_read - attributed)
    return cache_savings_over_models(pairs, leftover)
