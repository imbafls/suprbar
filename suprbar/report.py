"""Build the data payload for the "30-day Usage Report" export.

``build_report()`` returns a single JSON-serializable dict describing the last
30 days of local Claude Code usage: a per-day cost series, headline totals
(sessions / projects / cache savings / prior-month comparison), the active
budget, and breakdowns by source / model / project plus a token mix.

v1 is local-only and honest about it — the detailed source/model/project
breakdowns come from ``~/.claude`` data, so exactly one "source card" is
emitted. ``bySource`` is still a list so a future multi-source export is a
drop-in extension. Nothing here touches the network and nothing raises: on an
empty / missing ``CLAUDE_HOME`` we still return a fully-shaped dict with zeros
and a zero-filled ``byDay`` series.
"""

from __future__ import annotations

import calendar
import logging
import platform
import socket
from datetime import datetime, timedelta
from typing import Any

from . import __version__, config, scanner
from .pricing import cache_savings_over_models, family_for

log = logging.getLogger("suprbar.report")

# Accents cycled across the byModel rows (matches the report stylesheet tokens).
_MODEL_ACCENTS = [
    "var(--b-accent)",
    "var(--b-accent-2)",
    "var(--b-source-api)",
    "var(--b-tok-cache)",
]

_TOP_PROJECTS = 8


def _month_day_label(d: datetime) -> str:
    """e.g. "May 2" — built manually since Windows strftime lacks %-d."""
    return f"{calendar.month_abbr[d.month]} {d.day}"


def _humanize_range(start: datetime, end_inclusive: datetime) -> str:
    """e.g. "May 2 – May 31, 2026" from the 30-day window endpoints."""
    return (
        f"{_month_day_label(start)} – "
        f"{_month_day_label(end_inclusive)}, {end_inclusive.year}"
    )


def _parse_day(date_iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(date_iso)
    except (ValueError, TypeError):
        return None


def _monthly_limit() -> float:
    try:
        return float(config.get_pref("budgets.monthly_limit", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _alert_pct() -> int:
    try:
        return int(config.get_pref("budgets.alert_at_pct", 80) or 80)
    except Exception:  # noqa: BLE001
        return 80


def _week_starts_on() -> str:
    try:
        return str(config.get_pref("range.week_starts_on", "mon") or "mon")
    except Exception:  # noqa: BLE001
        return "mon"


def _display_theme() -> str:
    """Report theme honoring the user's ``display.theme``.

    The report stylesheet only ships ``dark`` and ``light`` variants, so the
    ``auto`` setting (and anything unexpected) collapses to ``dark``.
    """
    try:
        theme = str(config.get_pref("display.theme", "dark") or "dark")
    except Exception:  # noqa: BLE001
        theme = "dark"
    return "light" if theme == "light" else "dark"


def _display_accent() -> str:
    """Report accent honoring the user's ``display.accent`` (default ``blue``)."""
    try:
        return str(config.get_pref("display.accent", "blue") or "blue")
    except Exception:  # noqa: BLE001
        return "blue"


def _cache_savings(totals: dict[str, Any],
                   by_model: list[dict[str, Any]]) -> float:
    """USD saved by cache reads over the window.

    ``by_model`` rows don't carry per-model ``cache_read``, so distribute the
    window's total ``cache_read`` across models proportionally to each model's
    ``tokens`` and hand the resulting per-model shares to the shared
    ``pricing.cache_savings_over_models`` helper, which charges each at its own
    input rate (the same helper ``aggregator._compute_cache_savings`` uses).
    Falls back to the opus input rate when there's no model breakdown to
    attribute against (the helper's leftover path).
    """
    cache_read = int(totals.get("cache_read", 0) or 0)
    if cache_read <= 0:
        return 0.0

    token_sum = sum(int(m.get("tokens", 0) or 0) for m in by_model)
    if not by_model or token_sum <= 0:
        # Nothing to attribute against — charge the whole window at the opus
        # rate as a conservative upper bound (the shared helper's leftover path).
        return cache_savings_over_models([], leftover_cache_read=cache_read)

    pairs: list[tuple[float, str]] = []
    for m in by_model:
        tokens = int(m.get("tokens", 0) or 0)
        if tokens <= 0:
            continue
        cr_share = cache_read * tokens / token_sum
        pairs.append((cr_share, m.get("model", "")))
    return cache_savings_over_models(pairs)


def _model_family(models: list[str]) -> str:
    """First family-ish token of the leading model, else "mixed".

    Delegates the opus/sonnet/haiku match to ``pricing.family_for`` so the
    report and the cost engine agree on family detection. ``family_for``
    defaults to ``"opus"`` for an unrecognized id; here we'd rather surface the
    raw token than mislabel it, so a defaulted ``"opus"`` (where the id doesn't
    actually contain "opus") falls back to the whole token.
    """
    if not models:
        return "mixed"
    first = str(models[0] or "").strip()
    if not first:
        return "mixed"
    # e.g. "claude-opus-4-8" -> "opus"; falls back to the whole token.
    fam = family_for(first)
    if fam == "opus" and "opus" not in first.lower():
        return first
    return fam


def _build_by_day(by_day: list[dict[str, Any]]) -> tuple[list[list[Any]], str]:
    """Return (rows, peakDayName). Rows: [label, cost, isWeekend]."""
    rows: list[list[Any]] = []
    peak_cost = -1.0
    peak_name = ""
    for entry in by_day:
        d = _parse_day(entry.get("date", ""))
        cost = round(float(entry.get("cost", 0.0) or 0.0), 2)
        if d is None:
            rows.append(["?", cost, 0])
            continue
        weekend = 1 if d.weekday() >= 5 else 0
        rows.append([_month_day_label(d), cost, weekend])
        if cost > peak_cost:
            peak_cost = cost
            peak_name = calendar.day_abbr[d.weekday()]
    if peak_cost <= 0:
        peak_name = ""
    return rows, peak_name


def _build_by_model(by_model: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, m in enumerate(by_model):
        out.append({
            "model": m.get("model", ""),
            "src": "Claude Code",
            "cost": round(float(m.get("cost", 0.0) or 0.0), 2),
            "messages": int(m.get("messages", 0) or 0),
            "tokens": int(m.get("tokens", 0) or 0),
            "accent": _MODEL_ACCENTS[i % len(_MODEL_ACCENTS)],
        })
    return out


def _build_by_project(by_project: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in by_project[:_TOP_PROJECTS]:
        out.append({
            "project": p.get("project", ""),
            "cost": round(float(p.get("cost", 0.0) or 0.0), 2),
            "messages": int(p.get("messages", 0) or 0),
            "model": _model_family(list(p.get("models", []) or [])),
            "muted": False,
        })
    rest = by_project[_TOP_PROJECTS:]
    if rest:
        out.append({
            "project": f"+{len(rest)} more",
            "cost": round(sum(float(p.get("cost", 0.0) or 0.0) for p in rest), 2),
            "messages": sum(int(p.get("messages", 0) or 0) for p in rest),
            "model": "mixed",
            "muted": True,
        })
    return out


def _zero_filled_by_day(start_day, days: int = 30) -> list[list[Any]]:
    """``days`` rows of ``[label, 0.0, isWeekend]`` for the window.

    Used when there's no scan data (missing ~/.claude or a failed scan) so the
    report renders a calm zero-state instead of a blank page — the page-side JS
    does ``Math.max(...byDay)`` / ``byDay[peakIdx]`` which break on an empty list.
    """
    rows: list[list[Any]] = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        weekend = 1 if d.weekday() >= 5 else 0
        rows.append([f"{calendar.month_abbr[d.month]} {d.day}", 0.0, weekend])
    return rows


def _machine_label() -> str:
    try:
        host = socket.gethostname() or "?"
    except Exception:  # noqa: BLE001
        host = "?"
    osname = f"{platform.system()} {platform.release()}".strip() or "?"
    return f"{host} · {osname}"


def build_report() -> dict[str, Any]:
    """Assemble the 30-day usage report payload (see module docstring)."""
    now = datetime.now().astimezone()
    today = now.date()

    allow = config.project_allowlist()
    deny = config.project_denylist()
    anon = config.anonymize_projects()
    week_starts = _week_starts_on()

    # ---- main 30-day window ----
    try:
        r = scanner.range_summary(
            "30d",
            week_starts_on=week_starts,
            allowlist=allow,
            denylist=deny,
            anonymize=anon,
        )
    except Exception:  # noqa: BLE001
        log.exception("range_summary(30d) failed; emitting empty report")
        r = None

    if not r:
        r = {
            "totals": {"cost": 0.0, "messages": 0, "input": 0, "output": 0,
                       "cache_read": 0, "cache_hit_ratio": 0.0,
                       "sessions": 0, "projects": 0},
            "by_day": [], "by_model": [], "by_project": [],
        }

    totals = r.get("totals", {}) or {}
    by_day = list(r.get("by_day", []) or [])
    by_model = list(r.get("by_model", []) or [])
    by_project = list(r.get("by_project", []) or [])

    # ---- window endpoints for the humanized label ----
    # 30d range = today-29 .. today inclusive.
    start_day = today - timedelta(days=29)
    range_label = _humanize_range(
        datetime(start_day.year, start_day.month, start_day.day),
        datetime(today.year, today.month, today.day),
    )

    by_day_rows, peak_name = _build_by_day(by_day)
    if not by_day_rows:
        # No scan data — fall back to a zero-filled window (see docstring) so the
        # report renders zeros rather than crashing on an empty byDay series.
        by_day_rows = _zero_filled_by_day(start_day)

    # ---- previous 30-day window cost (for comparison) ----
    prev_cost = 0.0
    try:
        # Build naive-local dates first, then resolve each to its own UTC
        # offset via .astimezone(). Freezing ``now.tzinfo`` would stamp the
        # current offset onto dates that may sit on the other side of a DST
        # boundary, drifting the window by an hour.
        base = datetime(today.year, today.month, today.day)
        prev_start = (base - timedelta(days=59)).astimezone()
        prev_end = (base - timedelta(days=29)).astimezone()
        prev = scanner.range_summary(
            "custom",
            custom_start=prev_start.isoformat(),
            custom_end=prev_end.isoformat(),
            week_starts_on=week_starts,
            allowlist=allow,
            denylist=deny,
            anonymize=anon,
        )
        prev_cost = float((prev.get("totals", {}) or {}).get("cost", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        log.exception("previous-window range_summary failed; prevMonthCost=0")
        prev_cost = 0.0

    # ---- cache savings + headline numbers ----
    cache_savings = _cache_savings(totals, by_model)

    total_cost = round(float(totals.get("cost", 0.0) or 0.0), 2)
    total_messages = int(totals.get("messages", 0) or 0)

    # ---- top model by cost for the source card ----
    # Sort by cost (not message count) so the headline "top model" matches the
    # cost-sorted By-model table the report renders below.
    top_model = "—"
    if by_model:
        top = max(by_model, key=lambda m: float(m.get("cost", 0.0) or 0.0))
        top_model = top.get("model", "—") or "—"

    by_source = [{
        "id": "claude-code",
        "name": "Claude Code",
        "kind": "local · ~/.claude",
        "cost": total_cost,
        "messages": total_messages,
        "topModel": top_model,
        "color": "var(--b-accent)",
        "cls": "cc",
    }]

    return {
        "meta": {
            "rangeLabel": range_label,
            "generated": now.strftime("%b ") + f"{now.day}, {now.year} · "
            + now.strftime("%I:%M %p").lstrip("0"),
            "source": "1 source",
            "machine": _machine_label(),
            "version": "v" + __version__,
            "days": 30,
            "theme": _display_theme(),
            "accent": _display_accent(),
        },
        "byDay": by_day_rows,
        "totals": {
            "sessions": int(totals.get("sessions", 0) or 0),
            "projects": int(totals.get("projects", 0) or 0),
            "cacheSavings": round(cache_savings, 2),
            "cacheHitRatio": round(float(totals.get("cache_hit_ratio", 0.0) or 0.0), 4),
            "prevMonthCost": round(prev_cost, 2),
            "peakDayName": peak_name,
        },
        "budget": {
            "spent": total_cost,
            "limit": round(_monthly_limit(), 2),
            "alertPct": _alert_pct(),
        },
        "bySource": by_source,
        "byModel": _build_by_model(by_model),
        "byProject": _build_by_project(by_project),
        "tokens": {
            "input": int(totals.get("input", 0) or 0),
            "output": int(totals.get("output", 0) or 0),
            "cache": int(totals.get("cache_read", 0) or 0),
        },
    }
