"""Scan ~/.claude/projects/**/*.jsonl and aggregate Claude Code token usage.

Two main entry points:
  * scan(days)          — historical aggregate (used by future dashboard)
  * today_summary()     — today-only state for the MVP flyout: active session,
                          today's cost, token mix, messages, model, started
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
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


def _extract_usage_fields(usage: dict, fam: str) -> dict:
    cache_create = usage.get("cache_creation") or {}
    c5 = cache_create.get("ephemeral_5m_input_tokens", 0) or 0
    c1 = cache_create.get("ephemeral_1h_input_tokens", 0) or 0
    if not (c5 or c1):
        c5 = usage.get("cache_creation_input_tokens", 0) or 0
    return {
        "input":      usage.get("input_tokens", 0) or 0,
        "output":     usage.get("output_tokens", 0) or 0,
        "cache_5m":   c5,
        "cache_1h":   c1,
        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
        "cost":       cost_for(fam, usage),
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


# ---------- main: today-only summary (MVP) ----------

def today_summary() -> dict[str, Any]:
    """Return today's usage state for the tray flyout.

    Active session = the JSONL with the most-recent modified time, IF that
    mtime is within LIVE_WINDOW_SECONDS. Otherwise the flyout is "idle" and
    we report when we last saw a session.
    """
    started_at = time.time()
    now = datetime.now().astimezone()
    today = now.date()
    midnight_local = datetime(today.year, today.month, today.day,
                              tzinfo=now.tzinfo)
    midnight_utc = midnight_local.astimezone(timezone.utc)

    today_totals = _zero_bucket()
    # session_id -> dict of session info
    sessions: dict[str, dict] = {}
    files_scanned = 0
    last_file_seen_ts: float = 0.0

    if not CLAUDE_HOME.exists():
        return _empty_today(started_at, files_scanned)

    for path in CLAUDE_HOME.rglob("*.jsonl"):
        files_scanned += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        last_file_seen_ts = max(last_file_seen_ts, mtime)

        sid_from_name = path.stem  # filename usually IS the session uuid
        proj = _project_name(path)

        # First pass: gather usage and per-session timestamps
        sess_first_ts: datetime | None = None
        sess_last_ts: datetime | None = None
        sess_model: str | None = None
        sess_cost_today = 0.0
        sess_msgs_today = 0
        sess_today_fields = _zero_bucket()
        sess_id_in_file: str | None = None

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
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

                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    if dt < midnight_utc:
                        continue
                    fam = family_for(msg.get("model", ""))
                    fields = _extract_usage_fields(usage, fam)
                    _add(today_totals, fields)
                    _add(sess_today_fields, fields)
                    sess_cost_today += fields["cost"]
                    sess_msgs_today += 1
                    if msg.get("model"):
                        sess_model = msg["model"]
        except OSError:
            continue

        if sess_last_ts is None:
            continue

        sid = sess_id_in_file or sid_from_name
        # Take the latest entry per session id if a session spans files
        prior = sessions.get(sid)
        if prior and prior["last_ts"] > sess_last_ts:
            continue
        sessions[sid] = {
            "id": sid,
            "project": proj,
            "path": str(path),
            "mtime": mtime,
            "first_ts": sess_first_ts,
            "last_ts": sess_last_ts,
            "model": sess_model,
            "cost_today": sess_cost_today,
            "msgs_today": sess_msgs_today,
            "today": _serialize(sess_today_fields),
        }

    # Find the active session: most-recent mtime + within live window
    active = None
    if sessions:
        latest = max(sessions.values(), key=lambda s: s["mtime"])
        age = time.time() - latest["mtime"]
        if age <= LIVE_WINDOW_SECONDS:
            active = latest

    # If no live session, find the most recently active one (for "last seen")
    last_seen_session = None
    if not active and sessions:
        last_seen_session = max(sessions.values(), key=lambda s: s["mtime"])

    out: dict[str, Any] = {
        "now": now.isoformat(timespec="seconds"),
        "today_date": today.isoformat(),
        "scan_ms": int((time.time() - started_at) * 1000),
        "files_scanned": files_scanned,
        "today": _serialize(today_totals),
        "active": None,
        "last_session_seen": None,
    }

    if active:
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
            "today": active["today"],
        }
    elif last_seen_session:
        out["last_session_seen"] = {
            "last_activity": last_seen_session["last_ts"].isoformat(),
            "model": last_seen_session["model"],
            "project": last_seen_session["project"],
        }

    return out


def _empty_today(started_at: float, files_scanned: int) -> dict[str, Any]:
    return {
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
        "today_date": datetime.now().date().isoformat(),
        "scan_ms": int((time.time() - started_at) * 1000),
        "files_scanned": files_scanned,
        "today": _serialize(_zero_bucket()),
        "active": None,
        "last_session_seen": None,
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

    if not CLAUDE_HOME.exists():
        return _empty_scan(days, started)

    for path in CLAUDE_HOME.rglob("*.jsonl"):
        files_scanned += 1
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                proj = _project_name(path)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    dt = _parse_ts(rec.get("timestamp"))
                    if not dt or dt < cutoff:
                        continue
                    fam = family_for(msg.get("model", ""))
                    day = dt.date().isoformat()
                    sid = rec.get("sessionId")
                    if sid:
                        sessions.add(sid)
                    messages += 1
                    fields = _extract_usage_fields(usage, fam)
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
        "files_scanned": 0, "messages": 0, "sessions": 0,
        "grand_tokens": 0, "grand_cost": 0.0,
        "totals": {}, "by_day": {}, "by_project": {},
    }
