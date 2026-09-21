"""Scan ~/.claude/projects/**/*.jsonl and aggregate Claude Code token usage.

Main entry points:
  * today_summary()     — today-only state for the tray flyout: active session,
                          today's cost, token mix, messages, model, started
  * range_summary(key)  — usage aggregated over a user-selected time window
  * budgets_summary()   — spent-vs-limit for day / week / month

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
from datetime import datetime, timedelta, UTC, tzinfo
from pathlib import Path
from typing import Any

from .pricing import cost_for, family_for

CLAUDE_HOME = Path.home() / ".claude" / "projects"

# A session is "live" if its JSONL was appended to within this many seconds.
LIVE_WINDOW_SECONDS = 60

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
    "files_tailed": 0,
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
        # 3.11+ fromisoformat handles the trailing "Z" suffix natively.
        return datetime.fromisoformat(s)
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


def _live_session_payload(s: dict, now: datetime) -> dict[str, Any]:
    """Public-facing summary for one session file within the live window."""
    burn = 0.0
    # Denominator = today's first activity, NOT the file's first-ever record:
    # a session that started yesterday is still alive today, but cost_today
    # only counts today, so measuring from yesterday dilutes the rate.
    burn_from = s.get("first_ts_today") or s.get("first_ts")
    if burn_from:
        secs = max((now - burn_from.astimezone()).total_seconds(), 1.0)
        burn = s["cost_today"] / (secs / 3600.0)
    return {
        "id": s["id"],
        "project": s["project"],
        "path": s["path"],
        "started_at": s["first_ts"].isoformat() if s.get("first_ts") else None,
        "last_activity": s["last_ts"].isoformat(),
        "live": True,
        "model": s["model"],
        "cost_today": round(s["cost_today"], 4),
        "messages_today": s["msgs_today"],
        "burn_rate_usd_per_hour": round(burn, 4),
    }


# ---------- per-file scan worker ----------

# A full parse and an incremental tail share the same accumulator shape; the
# tail seeds it from the cached result, so the fold logic below works for both.
_LINE_CHUNK = 1 << 20  # 1 MiB binary read chunks (bounded memory on huge files)


def _empty_acc() -> dict[str, Any]:
    return {
        "sess_first_ts": None,
        "sess_last_ts": None,
        "sess_first_ts_today": None,
        "sess_model": None,
        "sess_cost_today": 0.0,
        "sess_msgs_today": 0,
        "sess_today_fields": _zero_bucket(),
        "sess_id_in_file": None,
        "today_totals": _zero_bucket(),
        "hourly": [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
                   for h in range(24)],
        "by_model": {},
        "rolling": {},
        "parse_errors": 0,
    }


def _acc_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Seed an accumulator from a previous result to continue appending."""
    acc = _empty_acc()
    acc["sess_first_ts"] = result.get("sess_first_ts")
    acc["sess_last_ts"] = result.get("sess_last_ts")
    acc["sess_first_ts_today"] = result.get("sess_first_ts_today")
    acc["sess_model"] = result.get("sess_model")
    acc["sess_cost_today"] = float(result.get("sess_cost_today", 0.0) or 0.0)
    acc["sess_msgs_today"] = int(result.get("sess_msgs_today", 0) or 0)
    acc["sess_id_in_file"] = result.get("sess_id_in_file")
    acc["parse_errors"] = int(result.get("parse_errors", 0) or 0)
    acc["today_totals"] = dict(result.get("today_totals") or _zero_bucket())
    acc["sess_today_fields"] = dict(
        result.get("sess_today_fields") or _zero_bucket())
    hourly = result.get("hourly") or []
    if len(hourly) == 24:
        acc["hourly"] = [dict(h) for h in hourly]
    by_model = result.get("by_model") or {}
    acc["by_model"] = {m: dict(v) for m, v in by_model.items()}
    rolling = result.get("rolling") or {}
    acc["rolling"] = {int(k): list(v) for k, v in rolling.items()}
    return acc


def _fold_lines(lines, midnight_utc: datetime, acc: dict[str, Any],
                rolling_cutoff_min: int | None = None) -> None:
    """Fold JSONL text lines into *acc* (mutates it). Shared by full + tail.

    When ``rolling_cutoff_min`` is set, usage in the last ~25h is also bucketed
    per minute (epoch-minute key) so a rolling-24h window can be summed from
    cache without re-reading files.
    """
    by_model: dict[str, dict[str, float]] = acc["by_model"]
    rolling: dict[int, list] = acc["rolling"]
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        # Cheap pre-filter — usage-bearing records always contain the literal
        # "usage" key. Saves a json.loads on most lines (tool calls / user
        # messages).
        has_usage_key = '"usage"' in line
        if not has_usage_key and '"sessionId"' not in line \
                and '"timestamp"' not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            acc["parse_errors"] += 1
            continue
        dt = _parse_ts(rec.get("timestamp"))
        if dt is None:
            continue
        if acc["sess_first_ts"] is None or dt < acc["sess_first_ts"]:
            acc["sess_first_ts"] = dt
        if acc["sess_last_ts"] is None or dt > acc["sess_last_ts"]:
            acc["sess_last_ts"] = dt
        if dt >= midnight_utc and (acc["sess_first_ts_today"] is None
                                   or dt < acc["sess_first_ts_today"]):
            acc["sess_first_ts_today"] = dt
        sid_in_rec = rec.get("sessionId")
        if sid_in_rec and not acc["sess_id_in_file"]:
            acc["sess_id_in_file"] = sid_in_rec

        if not has_usage_key:
            continue
        msg = rec.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        minute = int(dt.timestamp()) // 60
        in_rolling = (rolling_cutoff_min is not None
                      and minute >= rolling_cutoff_min)
        is_today = dt >= midnight_utc
        if not is_today and not in_rolling:
            continue
        model = msg.get("model") or ""
        fields = _extract_usage_fields(usage, model)
        # Rolling window includes the pre-midnight tail of the last 24h.
        if in_rolling:
            rb = rolling.get(minute)
            if rb is None:
                rb = [0.0, 0, 0]
                rolling[minute] = rb
            rb[0] += fields["cost"]
            rb[1] += 1
            rb[2] += (fields["input"] + fields["output"]
                      + fields["cache_5m"] + fields["cache_1h"]
                      + fields["cache_read"])
        if not is_today:
            continue
        _add(acc["today_totals"], fields)
        _add(acc["sess_today_fields"], fields)
        acc["sess_cost_today"] += fields["cost"]
        acc["sess_msgs_today"] += 1
        if model:
            acc["sess_model"] = model
            agg = by_model.get(model)
            if agg is None:
                agg = {"cost": 0.0, "messages": 0, "tokens": 0, "cache_read": 0}
                by_model[model] = agg
            agg["cost"] += fields["cost"]
            agg["messages"] += 1
            agg["cache_read"] += fields["cache_read"]
            agg["tokens"] += (
                fields["input"] + fields["output"]
                + fields["cache_5m"] + fields["cache_1h"]
                + fields["cache_read"]
            )
        # local hour bucket
        hourly = acc["hourly"]
        h = dt.astimezone().hour
        hourly[h]["cost"] += fields["cost"]
        hourly[h]["tokens"] += (
            fields["input"] + fields["output"]
            + fields["cache_5m"] + fields["cache_1h"]
            + fields["cache_read"]
        )
        hourly[h]["messages"] += 1


def _scan_one_file(path: Path, midnight_utc: datetime,
                   start_offset: int = 0,
                   base: dict[str, Any] | None = None,
                   rolling_cutoff_min: int | None = None,
                   ) -> tuple[dict[str, Any], int]:
    """Parse one JSONL (from ``start_offset``, or the whole file) for today.

    Streaming binary read: memory stays bounded on huge sessions and we get an
    exact byte offset back. The offset excludes a trailing partial line (a
    half-written append) so the next pass re-reads it intact.

    Returns ``(result, end_offset)``. ``result`` carries everything
    ``today_summary`` needs from this file. Pure function — thread-safe.
    """
    acc = _acc_from_result(base) if base else _empty_acc()
    total = max(0, int(start_offset))
    buf = b""
    try:
        with open(path, "rb") as f:
            if total:
                f.seek(total)
            while True:
                chunk = f.read(_LINE_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                if parts:
                    _fold_lines((p.decode("utf-8", "ignore") for p in parts),
                                midnight_utc, acc, rolling_cutoff_min)
    except OSError:
        return {"ok": False, "parse_errors": acc["parse_errors"]}, start_offset

    acc["ok"] = True
    return acc, total - len(buf)


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
    midnight_utc = midnight_local.astimezone(UTC)

    today_totals = _zero_bucket()
    hourly = [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
              for h in range(24)]
    by_model_global: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost": 0.0, "messages": 0, "tokens": 0, "cache_read": 0})
    # session_id -> dict of session info
    sessions: dict[str, dict] = {}
    # Rolling-window minute buckets (epoch minute -> [cost, messages, tokens])
    # summed across files; used for the overlay's "last 24h" number without a
    # range rescan. Keep 25h of history so pruning can't clip the window.
    rolling_global: dict[int, list] = {}
    rolling_cutoff_min = int((now.timestamp() - 25 * 3600) // 60)
    now_minute = int(now.timestamp()) // 60
    # project name -> aggregate
    by_project: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cost": 0.0, "messages": 0, "tokens": 0,
                 "models": set()})
    files_scanned = 0
    files_reused = 0
    files_reparsed = 0
    files_tailed = 0
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

    candidates: list[tuple[Path, float, int, dict | None, bool]] = []
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
        fresh = bool(cached and cached.get("mtime") == st.st_mtime
                     and cached.get("size") == st.st_size)
        candidates.append((path, st.st_mtime, st.st_size, cached, fresh))

    # Parse anything without a fresh cache hit in a small thread pool.
    # Files that only grew since their last parse get a tail read from the
    # stored byte offset (O(new bytes) instead of O(file)) — the common case
    # while a session is actively appending. Everything else is a full parse.
    to_parse = [(p, m, sz, c) for (p, m, sz, c, f) in candidates if not f]
    parsed_results: dict[str, tuple[dict[str, Any], int]] = {}
    tail_keys: set[str] = set()
    if to_parse:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futs = {}
            for (p, _m, sz, cached_entry) in to_parse:
                start = 0
                base = None
                if cached_entry:
                    prev_off = int(cached_entry.get("end_offset", 0) or 0)
                    if prev_off and sz > prev_off:
                        start = prev_off
                        base = cached_entry.get("result")
                        tail_keys.add(str(p))
                futs[ex.submit(_scan_one_file, p, midnight_utc,
                               start, base, rolling_cutoff_min)] = p
            for fut, p in futs.items():
                try:
                    parsed_results[str(p)] = fut.result()
                except Exception:
                    parsed_results[str(p)] = (
                        {"ok": False, "parse_errors": 0}, 0)

    # Walk candidates in order, attaching the freshly-parsed result where
    # needed, and update the cache atomically.
    with _scan_lock:
        for (path, mtime, size, cached, fresh) in candidates:
            key = str(path)
            if fresh and cached:
                files_reused += 1
                result = cached["result"]
                end_offset = int(cached.get("end_offset", size) or size)
            else:
                result, end_offset = parsed_results.get(
                    key, ({"ok": False, "parse_errors": 0}, 0))
                files_reparsed += 1
                if key in tail_keys:
                    files_tailed += 1
                if result.get("ok"):
                    _file_cache[key] = {
                        "mtime": mtime, "size": size,
                        "end_offset": end_offset,
                        "result": result,
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
                by_model_global[m]["cache_read"] += agg.get("cache_read", 0)

            # Rolling minutes: prune aged buckets from the cached result (bounds
            # memory on a long-running process) and fold the rest globally.
            rb = result.get("rolling") or {}
            if rb:
                for stale in [k for k in rb if k < rolling_cutoff_min]:
                    del rb[stale]
                for minute, v in rb.items():
                    g = rolling_global.get(minute)
                    if g is None:
                        rolling_global[minute] = [v[0], v[1], v[2]]
                    else:
                        g[0] += v[0]
                        g[1] += v[1]
                        g[2] += v[2]

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
                "first_ts_today": result["sess_first_ts_today"],
                "last_ts": sess_last_ts,
                "model": result["sess_model"],
                "cost_today": result["sess_cost_today"],
                "msgs_today": result["sess_msgs_today"],
                "today": _serialize(result["sess_today_fields"]),
            }

    # Find live sessions: any JSONL touched within the live window.
    try:
        from . import config as _cfg
        live_window = _cfg.live_threshold_seconds()
    except Exception:
        live_window = LIVE_WINDOW_SECONDS

    now_ts = time.time()
    live_sessions: list[dict[str, Any]] = []
    for s in sessions.values():
        if now_ts - s["mtime"] > live_window:
            continue
        if s["msgs_today"] <= 0:
            continue
        live_sessions.append(_live_session_payload(s, now))
    live_sessions.sort(key=lambda x: -x["cost_today"])

    active = None
    if live_sessions:
        head = live_sessions[0]
        raw = sessions.get(head["id"])
        active = {**head, "today": raw["today"]} if raw else head

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
        _last_scan_meta["files_tailed"] = files_tailed
        _last_scan_meta["last_scan_ms"] = elapsed_ms
        _last_scan_meta["parse_errors"] = parse_errors_total

    # Rolling 24h: sum minute buckets inside the window (minute-aligned, so
    # it's exact to the minute — no separate range scan needed).
    window_min = now_minute - 24 * 60
    r_cost = 0.0
    r_msgs = 0
    r_tokens = 0
    for minute, v in rolling_global.items():
        if minute >= window_min:
            r_cost += v[0]
            r_msgs += int(v[1])
            r_tokens += int(v[2])

    out: dict[str, Any] = {
        "now": now.isoformat(timespec="seconds"),
        "today_date": today.isoformat(),
        "scan_ms": elapsed_ms,
        "files_scanned": files_scanned,
        "files_reused": files_reused,
        "files_reparsed": files_reparsed,
        "files_tailed": files_tailed,
        "parse_errors": parse_errors_total,
        "rolling_24h": {
            "cost": round(r_cost, 4),
            "messages": r_msgs,
            "tokens": r_tokens,
            "since": datetime.fromtimestamp(
                window_min * 60, tz=now.tzinfo).isoformat(timespec="seconds"),
        },
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
                "cache_read": int(agg["cache_read"]),
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
        "live_sessions": live_sessions,
    }

    if active:
        out["active"] = active
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
        "files_tailed": 0,
        "parse_errors": 0,
        "rolling_24h": {
            "cost": 0.0,
            "messages": 0,
            "tokens": 0,
            "since": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
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
        "live_sessions": [],
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
    tz: tzinfo | None
    if day_boundary == "utc":
        now_for_day = now.astimezone(UTC)
        tz = UTC
    else:
        now_for_day = now
        tz = now.tzinfo
    today = now_for_day.date()

    def at_midnight(d):
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


def _range_scan_one_file(path: Path, proj: str,
                         start_utc: datetime, end_utc: datetime,
                         include_weekends: bool,
                         want_hourly: bool) -> dict[str, Any] | None:
    """Parse one JSONL against a (start, end) window; partial aggregate.

    Returns None when the file can't be opened. Pure function — thread-safe.
    """
    totals = _zero_bucket()
    by_day: dict[str, list] = {}
    by_model: dict[str, list] = {}
    p_cost = 0.0
    p_msgs = 0
    p_tokens = 0
    p_models: set[str] = set()
    hourly = [[0.0, 0, 0] for _ in range(24)] if want_hourly else None
    sessions: set[str] = set()
    parse_errors = 0

    try:
        f = open(path, "r", encoding="utf-8", errors="ignore")  # noqa: SIM115
    except OSError:
        return None
    with f:
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
                sessions.add(sid)

            local_dt = dt.astimezone()
            tokens = (fields["input"] + fields["output"]
                      + fields["cache_5m"] + fields["cache_1h"]
                      + fields["cache_read"])
            day = local_dt.date().isoformat()
            d = by_day.get(day)
            if d is None:
                by_day[day] = [fields["cost"], 1, tokens]
            else:
                d[0] += fields["cost"]
                d[1] += 1
                d[2] += tokens

            if model:
                m = by_model.get(model)
                if m is None:
                    by_model[model] = [fields["cost"], 1, tokens]
                else:
                    m[0] += fields["cost"]
                    m[1] += 1
                    m[2] += tokens

            p_cost += fields["cost"]
            p_msgs += 1
            p_tokens += tokens
            if model:
                p_models.add(model)

            if hourly is not None:
                h = local_dt.hour
                hourly[h][0] += fields["cost"]   # [cost, tokens, messages]
                hourly[h][1] += tokens
                hourly[h][2] += 1

    return {
        "totals": totals,
        "by_day": by_day,
        "by_model": by_model,
        "project": {"name": proj, "cost": p_cost, "messages": p_msgs,
                    "tokens": p_tokens, "models": p_models},
        "hourly": hourly,
        "sessions": sessions,
        "parse_errors": parse_errors,
    }


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
    start_utc = start_dt.astimezone(UTC)
    end_utc   = end_dt.astimezone(UTC)

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

    # Range scans re-read every file, so fan out across the same worker pool
    # the today scan uses. Workers return partial aggregates; merging is
    # order-independent (sums/sets).
    paths: list[tuple[Path, str]] = []
    for path in CLAUDE_HOME.rglob("*.jsonl"):
        files_scanned += 1
        proj = _project_name(path)
        if allow and proj not in allow:
            continue
        if proj in deny:
            continue
        paths.append((path, proj))

    want_hourly = (end_dt - start_dt) <= timedelta(hours=25)

    def _merge_partial(res: dict[str, Any]) -> None:
        nonlocal parse_errors
        _add(totals, res["totals"])
        parse_errors += res["parse_errors"]
        if res["sessions"]:
            sessions_seen.update(res["sessions"])
        for day, v in res["by_day"].items():
            b = by_day_acc[day]
            b["cost"] += v[0]
            b["messages"] += v[1]
            b["tokens"] += v[2]
        for model, v in res["by_model"].items():
            b = by_model_acc[model]
            b["cost"] += v[0]
            b["messages"] += v[1]
            b["tokens"] += v[2]
        pv = res["project"]
        pb: dict[str, Any] = by_project_acc[pv["name"]]
        pb["cost"] += pv["cost"]
        pb["messages"] += pv["messages"]
        pb["tokens"] += pv["tokens"]
        pb["models"].update(pv["models"])
        rh = res["hourly"]
        if rh is not None:
            for h in range(24):
                hourly[h]["cost"] += rh[h][0]
                hourly[h]["tokens"] += rh[h][1]
                hourly[h]["messages"] += rh[h][2]

    if paths:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futs = [ex.submit(_range_scan_one_file, p, proj,
                              start_utc, end_utc, include_weekends,
                              want_hourly)
                    for (p, proj) in paths]
            for fut in futs:
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res:
                    _merge_partial(res)

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

    model_rows: list[dict[str, Any]] = [
        {"model": m, "cost": round(v["cost"], 4),
         "messages": int(v["messages"]), "tokens": int(v["tokens"])}
        for m, v in by_model_acc.items()
    ]
    by_model_list = sorted(model_rows, key=lambda x: -float(x["cost"]))

    by_project_list = []
    for proj_index, (p, v) in enumerate(
            sorted(by_project_acc.items(), key=lambda kv: -kv[1]["cost"]), 1):
        display = f"project-{proj_index}" if anonymize else p
        by_project_list.append({
            "project": display,
            "cost": round(v["cost"], 4),
            "messages": int(v["messages"]),
            "tokens": int(v["tokens"]),
            "models": sorted(v["models"]),
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
                    project_limits: dict[str, float] | None = None,
                    ) -> dict[str, Any]:
    """Return spent vs limit for day/week/month windows.

    Useful for budget alerts. Reuses range_summary so all filters apply.
    ``project_limits`` adds per-project daily windows keyed
    ``"project:<name>"`` — one runaway repo shouldn't be invisible just
    because the global cap isn't hit yet.
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

    out: dict[str, Any] = {
        "daily":   b(today["totals"]["cost"], daily_limit),
        "weekly":  b(week["totals"]["cost"],  weekly_limit),
        "monthly": b(month["totals"]["cost"], monthly_limit),
    }
    if project_limits:
        spent_by_project = {
            p["project"]: float(p["cost"]) for p in today["by_project"]
        }
        for name, limit in project_limits.items():
            entry = b(spent_by_project.get(name, 0.0), float(limit))
            entry["project"] = name
            out[f"project:{name}"] = entry
    return out
