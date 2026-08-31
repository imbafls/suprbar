"""opencode source — reads the local opencode SQLite database.

opencode (https://opencode.ai) stores sessions/messages in
``~/.local/share/opencode/opencode.db`` (WAL-mode SQLite; honors
XDG_DATA_HOME). Schema facts this provider relies on:

  * message(id, session_id, time_created, data)  — ``data`` is JSON with
    role, modelID, providerID, tokens {input, output, reasoning,
    cache {read, write}}, cost (USD, computed by opencode via models.dev
    rates), and time {created} (epoch ms).
  * session(id, project_id, directory, title, ...)
  * project(id, worktree, name)

Only rows with role == "assistant" carry usage. ``time_created`` is epoch
millis today but treated defensively (values < 1e11 are read as seconds)
so older/newer exports don't silently break the day filter.

Token mapping to supr.bar's shape: reasoning tokens are folded into
output (providers bill reasoning as output), cache.write is tracked as
cache_1h, cache.read as cache_read. Cost is taken from opencode's own
per-message ``cost`` field — no local rate table needed for priced models.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..pricing import estimate_generic_cost

log = logging.getLogger("suprbar.opencode")

_ID = "opencode"
_LABEL = "opencode · local"


def _db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local" / "share")
    return Path(base) / "opencode" / "opencode.db"


# Diagnostics surface for self_test().
_last_fetch_ts: float = 0.0
_last_error: str | None = None

# Tiny result cache — the provider is polled every UI refresh and the
# SQLite query is cheap, but the parse of message.data JSON adds up.
_cache_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0
_CACHE_TTL = 3.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_ms(v: Any) -> int:
    """Defensive epoch normalization: seconds → millis when needed."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n * 1000 if n < 100_000_000_000 else n


def _local_midnight_ms() -> int:
    now = datetime.now().astimezone()
    midnight = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    return int(midnight.timestamp() * 1000)


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"{path.as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    return con


def _project_label(worktree: str, name: str, directory: str) -> str:
    """Prefer the user-set project name, else the worktree basename."""
    if name:
        return name
    base = (worktree or directory or "").rstrip("/\\")
    return Path(base).name if base else "opencode"


def _empty_result() -> dict[str, Any]:
    return {
        "id": _ID,
        "label": _LABEL,
        "ok": True,
        "error": None,
        "cost_today": 0.0,
        "tokens_today": {"input": 0, "output": 0,
                         "cache_5m": 0, "cache_1h": 0, "cache_read": 0},
        "messages_today": 0,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "extras": {
            "by_model": [],
            "by_project": [],
            "live_sessions": [],
            "sessions_today": 0,
            "projects_today": 0,
            "top_model_today": None,
        },
    }


def today_summary() -> dict[str, Any]:
    """Return today's opencode usage in aggregator source shape."""
    global _cache, _cache_ts

    with _cache_lock:
        if _cache is not None and (time.time() - _cache_ts) < _CACHE_TTL:
            return _cache

    out = _compute_today_summary()
    with _cache_lock:
        _cache = out
        _cache_ts = time.time()
    return out


def _compute_today_summary() -> dict[str, Any]:
    global _last_fetch_ts, _last_error

    db = _db_path()
    if not db.exists():
        # Not an error — opencode simply isn't installed/used on this box.
        _last_error = None
        return _empty_result()

    midnight_ms = _local_midnight_ms()
    now_ms = _now_ms()

    tokens_today = {"input": 0, "output": 0,
                    "cache_5m": 0, "cache_1h": 0, "cache_read": 0}
    cost_today = 0.0
    messages_today = 0
    estimated_msgs = 0
    # model_id -> {cost, messages, tokens, cache_read}
    by_model: dict[str, dict[str, float]] = {}
    # project label -> {cost, messages, tokens, models set}
    by_project: dict[str, dict[str, Any]] = {}
    # session_id -> {first_ts, last_ts, cost, messages, model, title, project}
    sessions: dict[str, dict[str, Any]] = {}

    try:
        con = _connect_ro(db)
    except sqlite3.Error as e:
        _last_error = f"sqlite connect: {e!s:.160}"
        log.warning("opencode db connect failed: %s", _last_error)
        out = _empty_result()
        out["ok"] = False
        out["error"] = _last_error
        return out

    try:
        cur = con.cursor()
        rows = cur.execute(
            "SELECT m.session_id, m.time_created, m.data "
            "FROM message m WHERE m.time_created >= ?",
            (midnight_ms,),
        ).fetchall()

        sess_meta: dict[str, tuple[str, str, str]] = {}
        for sid, _tc, data in rows:
            if not sid or sid in sess_meta:
                continue
            sess_meta[sid] = ("", "", "")
        if sess_meta:
            qmarks = ",".join("?" * len(sess_meta))
            for sid, directory, title, project_id in cur.execute(
                f"SELECT id, directory, title, project_id "
                f"FROM session WHERE id IN ({qmarks})",
                tuple(sess_meta.keys()),
            ):
                sess_meta[sid] = (directory or "", title or "", project_id or "")
            pids = {v[2] for v in sess_meta.values() if v[2]}
            pmap: dict[str, tuple[str, str]] = {}
            if pids:
                qm = ",".join("?" * len(pids))
                for pid, worktree, name in cur.execute(
                    f"SELECT id, worktree, name FROM project "
                    f"WHERE id IN ({qm})",
                    tuple(pids),
                ):
                    pmap[pid] = (worktree or "", name or "")
            for sid, (directory, title, project_id) in list(sess_meta.items()):
                worktree, name = pmap.get(project_id, ("", ""))
                sess_meta[sid] = (directory, title, _project_label(
                    worktree, name, directory))
    except sqlite3.Error as e:
        _last_error = f"sqlite query: {e!s:.160}"
        log.warning("opencode db query failed: %s", _last_error)
        try:
            con.close()
        except sqlite3.Error:
            pass
        out = _empty_result()
        out["ok"] = False
        out["error"] = _last_error
        return out

    for sid, tc, data in rows:
        try:
            rec = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rec, dict) or rec.get("role") != "assistant":
            continue

        ts_ms = _norm_ms(tc)
        if ts_ms < midnight_ms:
            continue

        tok = rec.get("tokens") or {}
        if not isinstance(tok, dict):
            tok = {}
        inp = int(tok.get("input", 0) or 0)
        out_n = int(tok.get("output", 0) or 0)
        reasoning = int(tok.get("reasoning", 0) or 0)
        cache = tok.get("cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        cr = int(cache.get("read", 0) or 0)
        cw = int(cache.get("write", 0) or 0)
        cost = float(rec.get("cost", 0.0) or 0.0)
        model = str(rec.get("modelID") or "")

        # opencode prices most models itself (models.dev rates); for any
        # message it leaves at $0 with real tokens, fall back to our generic
        # table so the flyout doesn't silently under-report spend.
        if cost == 0.0 and (inp or out_n or cr or cw):
            est = estimate_generic_cost(model, inp, out_n, cr, cw)
            if est is not None:
                cost = est
                estimated_msgs += 1

        out_total = out_n + reasoning  # reasoning is billed as output
        msgs = 1
        tokens_sum = inp + out_total + cr + cw

        cost_today += cost
        messages_today += msgs
        tokens_today["input"] += inp
        tokens_today["output"] += out_total
        tokens_today["cache_read"] += cr
        tokens_today["cache_1h"] += cw

        if model:
            m = by_model.setdefault(
                model, {"cost": 0.0, "messages": 0, "tokens": 0,
                        "cache_read": 0})
            m["cost"] += cost
            m["messages"] += msgs
            m["tokens"] += tokens_sum
            m["cache_read"] += cr

        label = (sess_meta.get(sid) or ("", "", "opencode"))[2]
        p = by_project.setdefault(
            label, {"cost": 0.0, "messages": 0, "tokens": 0,
                    "models": set()})
        p["cost"] += cost
        p["messages"] += msgs
        p["tokens"] += tokens_sum
        if model:
            p["models"].add(model)

        s = sessions.setdefault(sid, {
            "first_ts": ts_ms, "last_ts": ts_ms, "cost": 0.0,
            "messages": 0, "model": model, "title": "", "project": label,
        })
        s["first_ts"] = min(s["first_ts"], ts_ms)
        s["last_ts"] = max(s["last_ts"], ts_ms)
        s["cost"] += cost
        s["messages"] += msgs
        if model:
            s["model"] = model

    try:
        con.close()
    except sqlite3.Error:
        pass

    # Live sessions: activity within the configured threshold.
    try:
        from .. import config as _cfg
        live_window_ms = _cfg.live_threshold_seconds() * 1000
    except Exception:
        live_window_ms = 60_000

    live_sessions: list[dict[str, Any]] = []
    for sid, s in sessions.items():
        if s["messages"] <= 0:
            continue
        if now_ms - s["last_ts"] > live_window_ms:
            continue
        # Burn rate over TODAY's window only (first message today → now),
        # so sessions that started before midnight don't dilute the rate.
        secs = max((now_ms - s["first_ts"]) / 1000.0, 1.0)
        burn = s["cost"] / (secs / 3600.0)
        live_sessions.append({
            "id": sid,
            "project": s["project"],
            "path": str(db),
            "started_at": datetime.fromtimestamp(
                s["first_ts"] / 1000.0).astimezone().isoformat(
                timespec="seconds"),
            "last_activity": datetime.fromtimestamp(
                s["last_ts"] / 1000.0).astimezone().isoformat(
                timespec="seconds"),
            "live": True,
            "model": s["model"],
            "cost_today": round(s["cost"], 4),
            "messages_today": s["messages"],
            "burn_rate_usd_per_hour": round(burn, 4),
        })
    live_sessions.sort(key=lambda x: -x["cost_today"])

    model_rows: list[dict[str, Any]] = [
        {"model": m, "cost": round(v["cost"], 4),
         "messages": int(v["messages"]), "tokens": int(v["tokens"]),
         "cache_read": int(v["cache_read"])}
        for m, v in by_model.items()]
    by_model_list = sorted(model_rows, key=lambda r: -float(r["cost"]))
    project_rows: list[dict[str, Any]] = [
        {"project": p, "cost": round(v["cost"], 4),
         "messages": int(v["messages"]), "tokens": int(v["tokens"]),
         "models": sorted(v["models"])}
        for p, v in by_project.items() if v["messages"] > 0]
    by_project_list = sorted(project_rows, key=lambda r: -float(r["cost"]))

    _last_fetch_ts = time.time()
    _last_error = None

    out = _empty_result()
    out["cost_today"] = round(cost_today, 4)
    out["messages_today"] = messages_today
    out["tokens_today"] = tokens_today
    out["extras"] = {
        "by_model": by_model_list,
        "by_project": by_project_list,
        "live_sessions": live_sessions,
        "sessions_today": sum(1 for s in sessions.values()
                              if s["messages"] > 0),
        "projects_today": len(by_project_list),
        "top_model_today": (by_model_list[0]["model"]
                            if by_model_list else None),
        "estimated_messages": estimated_msgs,
    }
    return out


def self_test() -> dict[str, Any]:
    """Diagnostics surface for /api/diagnostics."""
    age: float | None = None
    if _last_fetch_ts:
        age = round(time.time() - _last_fetch_ts, 3)
    return {
        "ok": _last_error is None and _db_path().exists(),
        "last_fetch_age_seconds": age,
        "last_error": _last_error,
        "fingerprint": str(_db_path()),
    }
