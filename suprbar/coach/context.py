"""SessionContext — what a Rule sees.

A read-only snapshot derived from aggregator.today() plus a 7-day window
fetched from scanner.range_summary. Built once per engine cycle and passed
to every rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class SessionInfo:
    id: str | None
    project: str | None
    model: str | None
    started_at: datetime | None
    last_activity: datetime | None
    live: bool
    messages_today: int
    cost_today: float
    burn_rate_usd_per_hour: float

    @property
    def duration_hours(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.last_activity or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds() / 3600.0)


@dataclass(frozen=True)
class TodayTotals:
    cost: float
    messages: int
    input: int
    output: int
    cache_read: int
    cache_5m: int
    cache_1h: int
    cache_hit_ratio: float
    sessions_today: int
    projects_today: int


@dataclass(frozen=True)
class RollingStats:
    days: int
    total_cost: float
    median_cost_per_day: float
    avg_session_minutes: float
    by_hour: list[float]   # 24-element cost-per-local-hour rollup
    top_model: str | None
    by_project_count: int


@dataclass(frozen=True)
class SessionContext:
    now: datetime
    session: SessionInfo
    today: TodayTotals
    rolling: RollingStats
    raw_active: dict[str, Any] = field(default_factory=dict)

    # convenience derivative used by several rules
    @property
    def has_active_session(self) -> bool:
        return self.session.live and self.session.id is not None


# ---------- builder ----------

def build_context() -> SessionContext:
    """Construct a fresh SessionContext from the existing aggregator+scanner."""
    # Lazy imports keep coach package importable without these heavyweights
    # in unit tests that pass a hand-rolled ctx.
    from .. import aggregator, scanner

    today_payload = aggregator.today()
    active = today_payload.get("active") or {}

    started = _parse_dt(active.get("started_at"))
    last    = _parse_dt(active.get("last_activity"))

    session = SessionInfo(
        id=active.get("id"),
        project=active.get("project"),
        model=active.get("model"),
        started_at=started,
        last_activity=last,
        live=bool(active.get("live")),
        messages_today=int(active.get("messages_today") or 0),
        cost_today=float(active.get("cost_today") or 0.0),
        burn_rate_usd_per_hour=float(active.get("burn_rate_usd_per_hour") or 0.0),
    )

    today_dict = today_payload.get("today", {}) or {}
    today = TodayTotals(
        cost=float(today_dict.get("cost") or 0.0),
        messages=int(today_dict.get("messages") or 0),
        input=int(today_dict.get("input") or 0),
        output=int(today_dict.get("output") or 0),
        cache_read=int(today_dict.get("cache_read") or 0),
        cache_5m=int(today_dict.get("cache_5m") or 0),
        cache_1h=int(today_dict.get("cache_1h") or 0),
        cache_hit_ratio=float(today_dict.get("cache_hit_ratio") or 0.0),
        sessions_today=int(today_dict.get("sessions_today") or 0),
        projects_today=int(today_dict.get("projects_today") or 0),
    )

    # 7-day rolling — uses scanner directly so we can read by_hour and
    # by_project without re-hitting the aggregator's "today" shape.
    try:
        r = scanner.range_summary("7d")
    except Exception:
        r = {"totals": {}, "by_day": [], "by_model": [], "by_project": [],
             "hourly": [{"cost": 0.0}] * 24}
    totals = r.get("totals", {}) or {}
    days = r.get("by_day", []) or []
    costs = sorted([float(d.get("cost") or 0.0) for d in days])
    median = costs[len(costs) // 2] if costs else 0.0
    # crude avg session minutes proxy from totals.sessions and totals.messages
    sess = int(totals.get("sessions") or 1)
    rolling = RollingStats(
        days=int(r.get("range", {}).get("days") or 7),
        total_cost=float(totals.get("cost") or 0.0),
        median_cost_per_day=float(median),
        avg_session_minutes=float(r.get("range", {}).get("days") or 7) * 24 * 60 / max(sess, 1),
        by_hour=[float(h.get("cost") or 0.0)
                 for h in (r.get("hourly") or [])][:24] or [0.0] * 24,
        top_model=(r.get("by_model") or [{}])[0].get("model") if r.get("by_model") else None,
        by_project_count=int(totals.get("projects") or 0),
    )

    return SessionContext(
        now=datetime.now(timezone.utc),
        session=session,
        today=today,
        rolling=rolling,
        raw_active=active,
    )


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
