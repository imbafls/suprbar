"""Scan ~/.claude/projects/**/*.jsonl and aggregate Claude Code token usage.

Two main entry points:
  * scan(days)          — historical aggregate (used by future dashboard)
  * today_summary()     — today-only state for the MVP flyout: active session,
                          today's cost, token mix, messages, model, started

Performance notes:
  * Files are parsed in a small thread pool (I/O-bound, not CPU-bound).
  * Per-file results are memoized by (path, mtime, size). When today's date
    rolls over the cache is reset so stale "yesterday" buckets don't leak.
  * Each line is cheaply pre-filtered for ``"usage"`` before json.loads.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .pricing import cost_for, family_for

CLAUDE_HOME = Path.home() / ".claude" / "projects"

# A session is "live" if its JSONL was appended to within this many seconds.
LIVE_WINDOW_SECONDS = 60

# Earliest gap inside a single session that breaks "today's session" into a new
# one. Used so a session that started yesterday still shows "started X ago" if
# it's been continuously active.
SESSION_START_GRACE_HOURS = 12

# Worker count for the thread pool.
_MAX_WORKERS = min(8, (os.cpu_count() or 4))


# ---------- incremental scan cache ----------

# Module-level lock protects the cache + last-seen-date.
_scan_lock = threading.Lock()

# path -> {"mtime": float, "size": int, "result": dict, "today_date": str}
_file_cache: dict[str, dict[str, Any]] = {}

# Last observed local-date string; we reset the cache when it rolls over.
_cache_date: str | None = None

# Counters for the most-recent scan (debug surface for aggregator).
_last_scan_meta: dict[str, int] = {
    "files_reused": 0,
    "files_reparsed": 0,
    "last_scan_ms": 0,
    "parse_errors": 0,
}


# ---------- helpers ----------

def _zero_bucket() -> dict[str, float]:
    return {
        "input": 0, "output": 0,
        "cache_5m": 0, "cache_1h": 0, "cache_read": 0,
        "cost": 0.0, "messages": 0,
    }


def _add(dst: dict, fields: dict) -> None:
    for k, v in fields.items():
        dst[k] = dst.get(k, 0) + v


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _project_name(path: Path) -> str:
    try:
        return path.relative_to(CLAUDE_HOME).parts[0]
    except ValueError:
        return path.parent.name


def _extract_usage_fields(usage: dict, model: str) -> dict:
    """Extract token counts + cost for a single usage record.

    ``model`` is the full model id; pricing.cost_for picks per-model rates
    (with family fallback) and applies the 1M-context premium when present.
    """
    cache_create = usage.get("cache_creation") or {}
    c5 = cache_create.get("ephemeral_5m_input_tokens", 0) or 0
    c1 = cache_create.get("ephemeral_1h_input_tokens", 0) or 0
    if not (c5 or c1):
        c5 = usage.get("cache_creation_input_tokens", 0) or 0
    fam = family_for(model)
    # Pass the model id to cost_for when we have one — it falls back to
    # family rates internally. Keeps backward compat with old callers that
    # may still pass a family name directly.
    cost_key = model if model else fam
    return {
        "input":      usage.get("input_tokens", 0) or 0,
        "output":     usage.get("output_tokens", 0) or 0,
        "cache_5m":   c5,
        "cache_1h":   c1,
        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
        "cost":       cost_for(cost_key, usage),
        "messages":   1,
    }


def _serialize(b: dict) -> dict:
    return {
        "input": int(b["input"]),
        "output": int(b["output"]),
        "cache_5m": int(b["cache_5m"]),
        "cache_1h": int(b["cache_1h"]),
        "cache_read": int(b["cache_read"]),
        "cost": round(b["cost"], 4),
        "messages": int(b["messages"]),
        "tokens": int(b["input"] + b["output"] + b["cache_5m"]
                      + b["cache_1h"] + b["cache_read"]),
    }


# ---------- per-file scan worker ----------

def _scan_one_file(path: Path, midnight_utc: datetime) -> dict[str, Any]:
    """Parse one JSONL and produce a partial aggregate for today.

    Returns a dict with everything ``today_summary`` needs from this file:
    today_totals, session summary, model/hour/project breakdowns, parse
    errors. Pure function — safe to run in a thread.
    """
    sess_first_ts: datetime | None = None
    sess_last_ts: datetime | None = None
    sess_model: str | None = None
    sess_cost_today = 0.0
    sess_msgs_today = 0
    sess_today_fields = _zero_bucket()
    sess_id_in_file: str | None = None

    today_totals = _zero_bucket()
    # 24-element hourly cost/token totals (local-hour from each event ts).
    hourly = [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
              for h in range(24)]
    # model_id -> {cost, messages, tokens}
    by_model: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost": 0.0, "messages": 0, "tokens": 0})

    parse_errors = 0

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                # Cheap pre-filter — usage-bearing records always contain
                # the literal "usage" key. Saves a json.loads on most
                # lines (which are tool calls / user messages).
                has_usage_key = '"usage"' in line
                if not has_usage_key and '"sessionId"' not in line \
                        and '"timestamp"' not in line:
                    # nothing we care about
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
                dt = _parse_ts(rec.get("timestamp"))
                if dt is None:
                    continue
                if sess_first_ts is None or dt < sess_first_ts:
                    sess_first_ts = dt
                if sess_last_ts is None or dt > sess_last_ts:
                    sess_last_ts = dt
                sid_in_rec = rec.get("sessionId")
                if sid_in_rec and not sess_id_in_file:
                    sess_id_in_file = sid_in_rec

                if not has_usage_key:
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                if dt < midnight_utc:
                    continue
                model = msg.get("model") or ""
                fields = _extract_usage_fields(usage, model)
                _add(today_totals, fields)
                _add(sess_today_fields, fields)
                sess_cost_today += fields["cost"]
                sess_msgs_today += 1
                if model:
                    sess_model = model
                    by_model[model]["cost"] += fields["cost"]
                    by_model[model]["messages"] += 1
                    by_model[model]["tokens"] += (
                        fields["input"] + fields["output"]
                        + fields["cache_5m"] + fields["cache_1h"]
                        + fields["cache_read"]
                    )
                # local hour bucket
                local_dt = dt.astimezone()
                h = local_dt.hour
                hourly[h]["cost"] += fields["cost"]
                hourly[h]["tokens"] += (
                    fields["input"] + fields["output"]
                    + fields["cache_5m"] + fields["cache_1h"]
                    + fields["cache_read"]
                )
                hourly[h]["messages"] += 1
    except OSError:
        return {"ok": False, "parse_errors": parse_errors}

    return {
        "ok": True,
        "parse_errors": parse_errors,
        "today_totals": today_totals,
        "hourly": hourly,
        "by_model": dict(by_model),
        "sess_first_ts": sess_first_ts,
        "sess_last_ts": sess_last_ts,
        "sess_model": sess_model,
        "sess_cost_today": sess_cost_today,
        "sess_msgs_today": sess_msgs_today,
        "sess_today_fields": sess_today_fields,
        "sess_id_in_file": sess_id_in_file,
    }


def _reset_cache_if_date_rolled(today_iso: str) -> None:
    """Drop the per-file cache when the local date rolls over."""
    global _cache_date
    if _cache_date != today_iso:
        _file_cache.clear()
        _cache_date = today_iso


# ---------- main: today-only summary (MVP) ----------

def today_summary() -> dict[str, Any]:
    """Return today's usage state for the tray flyout.

    Active session = the JSONL with the most-recent modified time, IF that
    mtime is within LIVE_WINDOW_SECONDS. Otherwise the flyout is "idle" and
    we report when we last saw a session.

    Side effects: updates a module-level per-file mtime/size cache so
    follow-up calls reuse parse work for unchanged files.
    """
    started_at = time.time()
    now = datetime.now().astimezone()
    today = now.date()
    midnight_local = datetime(today.year, today.month, today.day,
                              tzinfo=now.tzinfo)
    midnight_utc = midnight_local.astimezone(timezone.utc)

    today_totals = _zero_bucket()
    hourly = [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
              for h in range(24)]
    by_model_global: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost": 0.0, "messages": 0, "tokens": 0})
    # session_id -> dict of session info
    sessions: dict[str, dict] = {}
    # project name -> aggregate
    by_project: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cost": 0.0, "messages": 0, "tokens": 0,
                 "models": set()})
    files_scanned = 0
    files_reused = 0
    files_reparsed = 0
    parse_errors_total = 0
    last_file_seen_ts: float = 0.0

    if not CLAUDE_HOME.exists():
        return _empty_today(started_at, files_scanned)

    # Honor project allow/deny filters from config.
    try:
        from . import config as _cfg
        _allow = set(_cfg.project_allowlist())
        _deny  = set(_cfg.project_denylist())
        _anonymize = _cfg.anonymize_projects()
    except Exception:
        _allow, _deny, _anonymize = set(), set(), False

    # Collect path + stat first so we can decide cached vs. reparse, then
    # parallelize the reparse work.
    with _scan_lock:
        _reset_cache_if_date_rolled(today.isoformat())

    candidates: list[tuple[Path, float, int, dict | None]] = []
    for path in CLAUDE_HOME.rglob("*.jsonl"):
        proj_name = _project_name(path)
        if _allow and proj_name not in _allow:
            continue
        if proj_name in _deny:
            continue
        files_scanned += 1
        try:
            st = path.stat()
        except OSError:
            continue
        last_file_seen_ts = max(last_file_seen_ts, st.st_mtime)
        key = str(path)
        cached = _file_cache.get(key)
        if cached and cached.get("mtime") == st.st_mtime \
                and cached.get("size") == st.st_size:
            candidates.append((path, st.st_mtime, st.st_size, cached["result"]))
        else:
            candidates.append((path, st.st_mtime, st.st_size, None))

    # Parse anything without a fresh cache hit in a small thread pool.
    to_parse = [(p, m, sz) for (p, m, sz, c) in candidates if c is None]
    parsed_results: dict[str, dict[str, Any]] = {}
    if to_parse:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futs = {ex.submit(_scan_one_file, p, midnight_utc): (p, m, sz)
                    for (p, m, sz) in to_parse}
            for fut in futs:
                p, m, sz = futs[fut]
                try:
                    parsed_results[str(p)] = fut.result()
                except Exception:  # noqa: BLE001
                    parsed_results[str(p)] = {"ok": False, "parse_errors": 0}

    # Walk candidates in order, attaching the freshly-parsed result where
    # needed, and update the cache atomically.
    with _scan_lock:
        for (path, mtime, size, cached_result) in candidates:
            key = str(path)
            if cached_result is not None:
                files_reused += 1
                result = cached_result
            else:
                result = parsed_results.get(key, {"ok": False,
                                                  "parse_errors": 0})
                files_reparsed += 1
                if result.get("ok"):
                    _file_cache[key] = {
                        "mtime": mtime, "size": size, "result": result,
                        "today_date": today.isoformat(),
                    }

            parse_errors_total += int(result.get("parse_errors", 0))
            if not result.get("ok"):
                continue

            _add(today_totals, result["today_totals"])
            for i, h in enumerate(result["hourly"]):
                hourly[i]["cost"] += h["cost"]
                hourly[i]["tokens"] += h["tokens"]
                hourly[i]["messages"] += h["messages"]
            for m, agg in result["by_model"].items():
                by_model_global[m]["cost"] += agg["cost"]
                by_model_global[m]["messages"] += agg["messages"]
                by_model_global[m]["tokens"] += agg["tokens"]

            sess_last_ts = result["sess_last_ts"]
            if sess_last_ts is None:
                continue

            sid_from_name = path.stem
            sid = result["sess_id_in_file"] or sid_from_name
            proj = _project_name(path)

            # Per-project aggregate (today only, only sessions that booked
            # usage today).
            if result["sess_msgs_today"] > 0:
                p_agg = by_project[proj]
                p_agg["cost"] += result["sess_cost_today"]
                p_agg["messages"] += result["sess_msgs_today"]
                t = result["sess_today_fields"]
                p_agg["tokens"] += (
                    t["input"] + t["output"] + t["cache_5m"]
                    + t["cache_1h"] + t["cache_read"]
                )
                if result["sess_model"]:
                    p_agg["models"].add(result["sess_model"])

            prior = sessions.get(sid)
            if prior and prior["last_ts"] > sess_last_ts:
                continue
            sessions[sid] = {
                "id": sid,
                "project": proj,
                "path": str(path),
                "mtime": mtime,
                "first_ts": result["sess_first_ts"],
                "last_ts": sess_last_ts,
                "model": result["sess_model"],
                "cost_today": result["sess_cost_today"],
                "msgs_today": result["sess_msgs_today"],
                "today": _serialize(result["sess_today_fields"]),
            }

    # Find the active session: most-recent mtime + within live window
    active = None
    if sessions:
        latest = max(sessions.values(), key=lambda s: s["mtime"])
        age = time.time() - latest["mtime"]
        # Read live-window from config so the user setting actually applies.
        try:
            from . import config as _cfg
            live_window = _cfg.live_threshold_seconds()
        except Exception:
            live_window = LIVE_WINDOW_SECONDS
        if age <= live_window:
            active = latest

    # If no live session, find the most recently active one (for "last seen")
    last_seen_session = None
    if not active and sessions:
        last_seen_session = max(sessions.values(), key=lambda s: s["mtime"])

    # Derive a few summary fields for the aggregator to lift.
    sessions_today = sum(
        1 for s in sessions.values() if s["msgs_today"] > 0
    )
    projects_today = len([p for p, agg in by_project.items()
                          if agg["messages"] > 0])
    top_model_today: str | None = None
    if by_model_global:
        top_model_today = max(by_model_global.items(),
                              key=lambda kv: kv[1]["messages"])[0]

    by_project_list = [
        {
            "project": proj,
            "cost": round(agg["cost"], 4),
            "messages": int(agg["messages"]),
            "tokens": int(agg["tokens"]),
            "models": sorted(agg["models"]),
        }
        for proj, agg in by_project.items()
        if agg["messages"] > 0
    ]
    by_project_list.sort(key=lambda p: -p["cost"])

    elapsed_ms = int((time.time() - started_at) * 1000)
    with _scan_lock:
        _last_scan_meta["files_reused"] = files_reused
        _last_scan_meta["files_reparsed"] = files_reparsed
        _last_scan_meta["last_scan_ms"] = elapsed_ms
        _last_scan_meta["parse_errors"] = parse_errors_total

    out: dict[str, Any] = {
        "now": now.isoformat(timespec="seconds"),
        "today_date": today.isoformat(),
        "scan_ms": elapsed_ms,
        "files_scanned": files_scanned,
        "files_reused": files_reused,
        "files_reparsed": files_reparsed,
        "parse_errors": parse_errors_total,
        "today": _serialize(today_totals),
        "active": None,
        "last_session_seen": None,
        "by_project": by_project_list,
        "by_model": [
            {
                "model": m,
                "cost": round(agg["cost"], 4),
                "messages": int(agg["messages"]),
                "tokens": int(agg["tokens"]),
            }
            for m, agg in sorted(by_model_global.items(),
                                 key=lambda kv: -kv[1]["cost"])
        ],
        "hourly": [
            {"hour": h["hour"],
             "cost": round(h["cost"], 4),
             "tokens": int(h["tokens"]),
             "messages": int(h["messages"])}
            for h in hourly
        ],
        "sessions_today": sessions_today,
        "projects_today": projects_today,
        "top_model_today": top_model_today,
    }

    if active:
        # burn rate (USD/hr) for the live session
        burn = 0.0
        if active["first_ts"]:
            secs = max((now - active["first_ts"].astimezone()).total_seconds(),
                       1.0)
            burn = active["cost_today"] / (secs / 3600.0)
        out["active"] = {
            "id": active["id"],
            "project": active["project"],
            "path": active["path"],
            "started_at": active["first_ts"].isoformat() if active["first_ts"] else None,
            "last_activity": active["last_ts"].isoformat(),
            "live": True,
            "model": active["model"],
            "cost_today": round(active["cost_today"], 4),
            "messages_today": active["msgs_today"],
            "burn_rate_usd_per_hour": round(burn, 4),
            "today": active["today"],
        }
    elif last_seen_session:
        out["last_session_seen"] = {
            "last_activity": last_seen_session["last_ts"].isoformat(),
            "model": last_seen_session["model"],
            "project": last_seen_session["project"],
        }

    return out


def cache_meta() -> dict[str, int]:
    """Diagnostics surface used by the aggregator."""
    with _scan_lock:
        return dict(_last_scan_meta)


def _empty_today(started_at: float, files_scanned: int) -> dict[str, Any]:
    return {
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
        "today_date": datetime.now().date().isoformat(),
        "scan_ms": int((time.time() - started_at) * 1000),
        "files_scanned": files_scanned,
        "files_reused": 0,
        "files_reparsed": 0,
        "parse_errors": 0,
        "today": _serialize(_zero_bucket()),
        "active": None,
        "last_session_seen": None,
        "by_project": [],
        "by_model": [],
        "hourly": [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
                   for h in range(24)],
        "sessions_today": 0,
        "projects_today": 0,
        "top_model_today": None,
    }


# ---------- historical scan (kept for future dashboard use) ----------

def scan(days: int = 30) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    started = time.time()
    by_day: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(_zero_bucket))
    by_project: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(_zero_bucket))
    totals: dict[str, dict] = defaultdict(_zero_bucket)
    sessions: set[str] = set()
    files_scanned = 0
    messages = 0
    parse_errors = 0

    if not CLAUDE_HOME.exists():
        return _empty_scan(days, started)

    for path in CLAUDE_HOME.rglob("*.jsonl"):
        files_scanned += 1
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                proj = _project_name(path)
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    dt = _parse_ts(rec.get("timestamp"))
                    if not dt or dt < cutoff:
                        continue
                    model = msg.get("model", "") or ""
                    fam = family_for(model)
                    day = dt.date().isoformat()
                    sid = rec.get("sessionId")
                    if sid:
                        sessions.add(sid)
                    messages += 1
                    fields = _extract_usage_fields(usage, model)
                    _add(by_day[day][fam], fields)
                    _add(by_project[proj][fam], fields)
                    _add(totals[fam], fields)
        except OSError:
            continue

    grand_cost = sum(t["cost"] for t in totals.values())
    grand_tokens = sum(
        t["input"] + t["output"] + t["cache_5m"] + t["cache_1h"] + t["cache_read"]
        for t in totals.values()
    )

    return {
        "days": days,
        "cutoff": cutoff.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scan_ms": int((time.time() - started) * 1000),
        "files_scanned": files_scanned,
        "messages": messages,
        "parse_errors": parse_errors,
        "sessions": len(sessions),
        "grand_tokens": int(grand_tokens),
        "grand_cost": round(grand_cost, 4),
        "totals": {k: _serialize(v) for k, v in totals.items()},
        "by_day": {
            day: {fam: _serialize(b) for fam, b in fams.items()}
            for day, fams in sorted(by_day.items())
        },
        "by_project": {
            proj: {
                "totals": _proj_totals(fams),
                "families": {fam: _serialize(b) for fam, b in fams.items()},
            }
            for proj, fams in sorted(
                by_project.items(),
                key=lambda kv: -sum(b["cost"] for b in kv[1].values()),
            )
        },
    }


def _proj_totals(fams: dict) -> dict:
    out = _zero_bucket()
    for b in fams.values():
        _add(out, b)
    return _serialize(out)


def _empty_scan(days: int, started: float) -> dict:
    return {
        "days": days, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scan_ms": int((time.time() - started) * 1000),
        "files_scanned": 0, "messages": 0, "sessions": 0, "parse_errors": 0,
        "grand_tokens": 0, "grand_cost": 0.0,
        "totals": {}, "by_day": {}, "by_project": {},
    }


# ---------- general range summary (user-configurable time window) ----------

def _resolve_range(range_key: str | None,
                   custom_start: str | None = None,
                   custom_end:   str | None = None,
                   week_starts_on: str = "mon",
                   day_boundary: str = "local",
                   rolling_24h: bool = False,
                   ) -> tuple[datetime, datetime, str]:
    """Translate a UI range key into concrete (start, end, label) datetimes."""
    now = datetime.now().astimezone()
    if day_boundary == "utc":
        now_for_day = now.astimezone(timezone.utc)
        tz = timezone.utc
    else:
        now_for_day = now
        tz = now.tzinfo
    today = now_for_day.date()

    def at_midnight(d):  # noqa: ANN001
        return datetime(d.year, d.month, d.day, tzinfo=tz)

    end = at_midnight(today) + timedelta(days=1)

    rk = (range_key or "today").lower()
    if rk == "today":
        if rolling_24h:
            return now - timedelta(hours=24), now, "last 24h"
        return at_midnight(today), end, "today"
    if rk == "yesterday":
        return at_midnight(today - timedelta(days=1)), at_midnight(today), "yesterday"
    if rk == "24h":
        return now - timedelta(hours=24), now, "last 24h"
    if rk == "7d":
        return at_midnight(today - timedelta(days=6)), end, "last 7 days"
    if rk == "30d":
        return at_midnight(today - timedelta(days=29)), end, "last 30 days"
    if rk == "90d":
        return at_midnight(today - timedelta(days=89)), end, "last 90 days"
    if rk == "week":
        # current calendar week
        weekday = today.weekday()  # Mon=0..Sun=6
        if week_starts_on == "sun":
            offset = (weekday + 1) % 7
        else:
            offset = weekday
        start = at_midnight(today - timedelta(days=offset))
        return start, end, "this week"
    if rk == "month":
        start = at_midnight(today.replace(day=1))
        return start, end, "this month"
    if rk == "custom":
        try:
            s = datetime.fromisoformat(custom_start) if custom_start else at_midnight(today)
            e = datetime.fromisoformat(custom_end) if custom_end else end
            if s.tzinfo is None:
                s = s.replace(tzinfo=tz)
            if e.tzinfo is None:
                e = e.replace(tzinfo=tz)
            return s, e, f"{s.date()} → {e.date()}"
        except ValueError:
            pass
    # fallback
    return at_midnight(today), end, "today"


def range_summary(range_key: str = "today",
                  custom_start: str | None = None,
                  custom_end:   str | None = None,
                  week_starts_on: str = "mon",
                  day_boundary: str = "local",
                  rolling_24h: bool = False,
                  allowlist: list[str] | None = None,
                  denylist:  list[str] | None = None,
                  anonymize: bool = False,
                  include_weekends: bool = True,
                  ) -> dict[str, Any]:
    """Aggregate usage between (start, end) computed from `range_key`.

    Shape (additive to today_summary):
      { range: {key, label, start, end},
        totals: {cost, messages, input, output, cache_5m, cache_1h, cache_read,
                 tokens, cache_hit_ratio, sessions_today, projects_today},
        by_day: [ {date, cost, tokens, messages} ],
        by_model: [ {model, cost, messages, tokens} ],
        by_project: [ {project, cost, messages, tokens, models[]} ],
        hourly: [ {hour, cost, tokens, messages} ],          # only meaningful for ≤24h ranges
        scan_ms, files_scanned, parse_errors }
    """
    started_at = time.time()
    start_dt, end_dt, label = _resolve_range(
        range_key, custom_start, custom_end, week_starts_on,
        day_boundary, rolling_24h,
    )
    start_utc = start_dt.astimezone(timezone.utc)
    end_utc   = end_dt.astimezone(timezone.utc)

    allow = set(allowlist or [])
    deny  = set(denylist  or [])

    totals = _zero_bucket()
    by_day_acc: dict[str, dict[str, float]] = defaultdict(lambda: {
        "cost": 0.0, "messages": 0, "tokens": 0})
    by_model_acc: dict[str, dict[str, float]] = defaultdict(lambda: {
        "cost": 0.0, "messages": 0, "tokens": 0})
    by_project_acc: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "cost": 0.0, "messages": 0, "tokens": 0, "models": set()})
    hourly = [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
              for h in range(24)]
    sessions_seen: set[str] = set()

    files_scanned = 0
    parse_errors = 0

    if not CLAUDE_HOME.exists():
        return _empty_range(label, start_utc, end_utc, started_at)

    for path in CLAUDE_HOME.rglob("*.jsonl"):
        files_scanned += 1
        proj = _project_name(path)
        if allow and proj not in allow:
            continue
        if proj in deny:
            continue

        try:
            f = open(path, "r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            for raw in f:
                line = raw.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
                dt = _parse_ts(rec.get("timestamp"))
                if dt is None:
                    continue
                if dt < start_utc or dt >= end_utc:
                    continue
                if not include_weekends and dt.astimezone().weekday() >= 5:
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                model = msg.get("model") or ""
                fields = _extract_usage_fields(usage, model)
                _add(totals, fields)
                sid = rec.get("sessionId")
                if sid:
                    sessions_seen.add(sid)

                local_dt = dt.astimezone()
                day = local_dt.date().isoformat()
                by_day_acc[day]["cost"] += fields["cost"]
                by_day_acc[day]["messages"] += 1
                by_day_acc[day]["tokens"] += (
                    fields["input"] + fields["output"]
                    + fields["cache_5m"] + fields["cache_1h"]
                    + fields["cache_read"]
                )

                if model:
                    by_model_acc[model]["cost"] += fields["cost"]
                    by_model_acc[model]["messages"] += 1
                    by_model_acc[model]["tokens"] += (
                        fields["input"] + fields["output"]
                        + fields["cache_5m"] + fields["cache_1h"]
                        + fields["cache_read"]
                    )

                by_project_acc[proj]["cost"] += fields["cost"]
                by_project_acc[proj]["messages"] += 1
                by_project_acc[proj]["tokens"] += (
                    fields["input"] + fields["output"]
                    + fields["cache_5m"] + fields["cache_1h"]
                    + fields["cache_read"]
                )
                if model:
                    by_project_acc[proj]["models"].add(model)

                # hourly only meaningful for ≤24h ranges
                if (end_dt - start_dt) <= timedelta(hours=25):
                    h = local_dt.hour
                    hourly[h]["cost"] += fields["cost"]
                    hourly[h]["tokens"] += (
                        fields["input"] + fields["output"]
                        + fields["cache_5m"] + fields["cache_1h"]
                        + fields["cache_read"]
                    )
                    hourly[h]["messages"] += 1
        finally:
            f.close()

    # Fill missing days in range for nicer chart line.
    days_list = []
    d = start_dt.date()
    while d < end_dt.date():
        key = d.isoformat()
        b = by_day_acc.get(key, {"cost": 0.0, "messages": 0, "tokens": 0})
        days_list.append({
            "date": key,
            "cost": round(b["cost"], 4),
            "messages": int(b["messages"]),
            "tokens": int(b["tokens"]),
        })
        d += timedelta(days=1)

    by_model_list = sorted([
        {"model": m, "cost": round(v["cost"], 4),
         "messages": int(v["messages"]), "tokens": int(v["tokens"])}
        for m, v in by_model_acc.items()
    ], key=lambda x: -x["cost"])

    by_project_list = []
    proj_index = 0
    for p, v in sorted(by_project_acc.items(), key=lambda kv: -kv[1]["cost"]):
        proj_index += 1
        display = f"project-{proj_index}" if anonymize else p
        by_project_list.append({
            "project": display,
            "cost": round(v["cost"], 4),
            "messages": int(v["messages"]),
            "tokens": int(v["tokens"]),
            "models": sorted(list(v["models"])),
        })

    cache_read = totals["cache_read"]
    cache_hit_ratio = (cache_read / (totals["input"] + cache_read)) \
        if (totals["input"] + cache_read) > 0 else 0.0

    return {
        "range": {
            "key": range_key,
            "label": label,
            "start": start_dt.isoformat(timespec="seconds"),
            "end":   end_dt.isoformat(timespec="seconds"),
            "days":  max(1, (end_dt.date() - start_dt.date()).days),
        },
        "totals": {
            "cost": round(totals["cost"], 4),
            "messages": int(totals["messages"]),
            **{k: int(totals[k]) for k in
               ("input", "output", "cache_5m", "cache_1h", "cache_read")},
            "tokens": int(totals["input"] + totals["output"]
                          + totals["cache_5m"] + totals["cache_1h"]
                          + totals["cache_read"]),
            "cache_hit_ratio": round(cache_hit_ratio, 4),
            "sessions": len(sessions_seen),
            "projects": len(by_project_acc),
        },
        "by_day": days_list,
        "by_model": by_model_list,
        "by_project": by_project_list,
        "hourly": hourly,
        "files_scanned": files_scanned,
        "parse_errors": parse_errors,
        "scan_ms": int((time.time() - started_at) * 1000),
    }


def _empty_range(label: str, start: datetime, end: datetime, started: float) -> dict:
    return {
        "range": {"key": "today", "label": label,
                  "start": start.isoformat(), "end": end.isoformat(), "days": 1},
        "totals": {"cost": 0.0, "messages": 0, "tokens": 0,
                   "input": 0, "output": 0, "cache_5m": 0, "cache_1h": 0,
                   "cache_read": 0, "cache_hit_ratio": 0.0,
                   "sessions": 0, "projects": 0},
        "by_day": [], "by_model": [], "by_project": [],
        "hourly": [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
                   for h in range(24)],
        "files_scanned": 0, "parse_errors": 0,
        "scan_ms": int((time.time() - started) * 1000),
    }


# ---------- budgets ----------

def budgets_summary(daily_limit: float, weekly_limit: float, monthly_limit: float,
                    week_starts_on: str = "mon",
                    allowlist: list[str] | None = None,
                    denylist:  list[str] | None = None,
                    ) -> dict[str, Any]:
    """Return spent vs limit for day/week/month windows.

    Useful for budget alerts. Reuses range_summary so all filters apply.
    """
    today = range_summary("today",
                          allowlist=allowlist, denylist=denylist)
    week  = range_summary("week", week_starts_on=week_starts_on,
                          allowlist=allowlist, denylist=denylist)
    month = range_summary("month",
                          allowlist=allowlist, denylist=denylist)

    def b(spent: float, limit: float) -> dict[str, Any]:
        if limit <= 0:
            return {"spent": round(spent, 4), "limit": 0.0,
                    "pct": 0.0, "over": False, "remaining": 0.0}
        pct = (spent / limit) * 100
        return {
            "spent": round(spent, 4),
            "limit": round(limit, 4),
            "pct": round(pct, 2),
            "over": spent >= limit,
            "remaining": round(max(0.0, limit - spent), 4),
        }

    return {
        "daily":   b(today["totals"]["cost"], daily_limit),
        "weekly":  b(week["totals"]["cost"],  weekly_limit),
        "monthly": b(month["totals"]["cost"], monthly_limit),
    }
