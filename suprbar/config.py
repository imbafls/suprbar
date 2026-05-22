"""Persistent config for supr.bar.

Stored at %APPDATA%\\suprbar\\config.json. The admin API key is DPAPI-encrypted
on Windows so it isn't sitting on disk in plaintext.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import shutil
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

log = logging.getLogger("suprbar.config")


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "suprbar"


def local_data_dir() -> Path:
    """%LOCALAPPDATA%\\suprbar on Windows; ~/.local/share/suprbar elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "suprbar"


def config_path() -> Path:
    return config_dir() / "config.json"


def window_state_path() -> Path:
    return local_data_dir() / "window-state.json"


SCHEMA_VERSION = 2

DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,

    # ---- Sources (unchanged contract) ----
    "sources": {
        "local": {"enabled": True},
        "anthropic_api": {
            "enabled": False,
            "admin_key_enc": "",        # DPAPI-encrypted blob (base64)
            "poll_seconds": 60,         # Admin API poll cadence
        },
        # How equivalent vs actual cost is computed (display hint, not auth).
        # equivalent = JSONL-derived (what an API user would have paid)
        # actual_api = only what comes from the Admin API
        # both      = sum, with breakdown shown
        "cost_mode": "equivalent",
    },

    # ---- Legacy ui keys still honored (pinned + start_on_login) ----
    "ui": {
        "pinned": False,
        "start_on_login": False,
    },

    # ---- Time range / filter prefs ----
    "range": {
        # default range applied when popup opens
        "default": "today",            # today|yesterday|24h|7d|30d|90d|custom
        "custom_start": None,          # ISO date YYYY-MM-DD or null
        "custom_end":   None,
        "week_starts_on": "mon",       # sun|mon
        "day_boundary":  "local",      # local|utc
        "rolling_24h":   False,        # true → last 24h instead of calendar day
        "include_weekends": True,
        "compare_previous": "off",     # off|prev_day|prev_week|prev_month
    },

    # ---- Display prefs ----
    "display": {
        "theme":       "dark",         # dark|light|auto
        "accent":      "violet",       # violet|blue|green|orange|pink
        "density":     "normal",       # compact|normal|spacious
        "font_scale":  1.0,            # 0.85..1.25
        "currency":    "USD",          # USD|EUR|GBP|JPY|CAD|AUD
        "cost_format": "with_cents",   # with_cents|whole
        "token_format": "compact",     # compact (1.2k) | full (1,234)
        "locale":      "en-US",
        "show_token_bar":  True,
        "show_cache_info": True,
        "show_burn_rate":  True,
        "show_model":      True,
        "show_project":    True,
        "show_sessions_today": True,
        "animations": True,            # toggle all UI animations
    },

    # ---- Budgets & alerts ----
    "budgets": {
        "daily_limit":   0.0,          # 0 = no limit
        "weekly_limit":  0.0,
        "monthly_limit": 0.0,
        "alert_at_pct":  80,           # alert when >= this % of any active limit
        "notify":        True,         # show toast alert
        "tray_warn_color": True,       # turn tray icon red on warning
        "audio_alert":   False,
        "quiet_hours":   "off",        # off|22-08|23-07|00-09|custom
        "quiet_start":   22,
        "quiet_end":     8,
    },

    # ---- Behavior ----
    "behavior": {
        "refresh_seconds":       5,    # 0=manual, 5/15/30/60/300
        "auto_hide":             True, # auto-hide popup on blur
        "auto_hide_delay_ms":    0,    # delay before auto-hide
        "always_on_top":         True,
        "show_in_taskbar":       False,
        "live_threshold_seconds": 60,  # JSONL mtime within X = "live"
        "start_minimized":       True, # tray-only on boot (no popup)
        "confirm_quit":          False,
        "click_through":         False, # popup transparent to clicks
        "single_instance":       True,
        "open_dashboard_on_click": True, # left-click tray opens popup
    },

    # ---- Project filters ----
    "projects": {
        "allowlist": [],               # if non-empty, ONLY these projects show
        "denylist":  [],               # always hidden
        "anonymize": False,            # show "project-1", "project-2" etc
        "top_n":     10,               # limit list to top N by cost
    },

    # ---- Keyboard shortcuts (in-popup; global hotkeys reserved for later) ----
    "keyboard": {
        "enable_global":    False,     # OS-wide hotkeys (not yet wired)
        "hotkey_toggle":    "Ctrl+Alt+S",
        "hotkey_refresh":   "F5",
        "hotkey_settings":  "Ctrl+,",
        "hotkey_quit":      "Alt+Q",
        "hotkey_export":    "Ctrl+E",
        "hotkey_copy_cost": "Ctrl+C",
        "vim_keys":         False,     # j/k navigation in lists
    },

    # ---- Data / privacy ----
    "data": {
        "log_level":         "INFO",   # OFF|ERROR|WARN|INFO|DEBUG
        "log_retention_days": 7,
        "anonymize_logs":    False,
        "cache_ttl_seconds": 4,
        "telemetry":         False,    # reserved; off by default
    },

    # ---- Window placement ----
    "window": {
        "anchor":   "bottom-right",   # bottom-right|top-right|bottom-left|top-left|top-center|center
        "margin_px": 12,
        "preferred_monitor": 0,        # 0 = monitor with cursor, 1..N = explicit
        "remember_position": True,
        "width":   360,
        "height":  480,
        "opacity": 1.0,                # 0.5..1.0
    },
}


_lock = threading.Lock()
_cache: dict[str, Any] | None = None


# ---------- DPAPI ----------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_protect(plaintext: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        in_blob = _DATA_BLOB(len(plaintext),
                             ctypes.cast(ctypes.c_char_p(plaintext),
                                         ctypes.POINTER(ctypes.c_byte)))
        out_blob = _DATA_BLOB()
        if not crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None, 0x1,
            ctypes.byref(out_blob),
        ):
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except OSError:
        return None


def _dpapi_unprotect(ciphertext: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        in_blob = _DATA_BLOB(len(ciphertext),
                             ctypes.cast(ctypes.c_char_p(ciphertext),
                                         ctypes.POINTER(ctypes.c_byte)))
        out_blob = _DATA_BLOB()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0x1,
            ctypes.byref(out_blob),
        ):
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except OSError:
        return None


# ---------- load/save ----------

def _merge_defaults(d: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge user config on top of DEFAULTS; user values win, but
    missing keys (e.g. newly added settings) get the default."""
    out = json.loads(json.dumps(DEFAULTS))
    _deep_merge(out, d)
    return out


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def _migrate(d: dict[str, Any]) -> dict[str, Any]:
    """Bump older configs to the current schema."""
    if not isinstance(d, dict):
        return json.loads(json.dumps(DEFAULTS))
    v = d.get("schema_version")
    if not isinstance(v, int) or v < 1:
        if "sources" not in d or not isinstance(d.get("sources"), dict):
            d["sources"] = json.loads(json.dumps(DEFAULTS["sources"]))
        if "ui" not in d or not isinstance(d.get("ui"), dict):
            d["ui"] = json.loads(json.dumps(DEFAULTS["ui"]))
        d["schema_version"] = 1
        log.info("config migrated to schema_version=1")
    if d.get("schema_version") == 1:
        # v1 → v2: introduce range/display/budgets/behavior/projects/keyboard/data/window
        # We don't have to write defaults here — _merge_defaults handles that.
        d["schema_version"] = 2
        log.info("config migrated to schema_version=2")
    return d


def load(force: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        p = config_path()
        if not p.exists():
            _cache = json.loads(json.dumps(DEFAULTS))
            return _cache
        try:
            raw = json.loads(p.read_text("utf-8"))
            migrated = _migrate(raw if isinstance(raw, dict) else {})
            _cache = _merge_defaults(migrated)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("config load failed: %s — using defaults", e)
            _cache = json.loads(json.dumps(DEFAULTS))
        return _cache


def save(cfg: dict[str, Any]) -> None:
    with _lock:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            try:
                shutil.copy2(p, p.with_suffix(".json.bak"))
            except OSError as e:
                log.warning("config backup failed: %s", e)
        tmp = p.with_suffix(".json.tmp")
        if "schema_version" not in cfg:
            cfg["schema_version"] = SCHEMA_VERSION
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        global _cache
        _cache = cfg


def reset(reset_key: bool = False) -> dict[str, Any]:
    """Reset config to defaults. If reset_key=False, preserve the admin key."""
    existing_key = None
    if not reset_key:
        try:
            cur = load()
            existing_key = cur.get("sources", {}).get("anthropic_api", {}).get("admin_key_enc", "")
        except Exception:
            existing_key = None

    fresh = json.loads(json.dumps(DEFAULTS))
    if not reset_key and existing_key:
        fresh.setdefault("sources", {}).setdefault("anthropic_api", {})["admin_key_enc"] = existing_key
    save(fresh)
    return fresh


# ---------- generic dotted-path access ----------

def get_pref(path: str, default: Any = None) -> Any:
    """Read a nested setting by dotted path, e.g. 'display.theme'."""
    cur: Any = load()
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_pref(path: str, value: Any) -> Any:
    """Write a nested setting by dotted path. Returns the stored value
    after coercion, or raises ValueError if validation fails."""
    coerced = _coerce(path, value)
    cfg = load()
    parts = path.split(".")
    cur = cfg
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = coerced
    save(cfg)
    return coerced


# ---------- coercion / validation ----------

# (key path) -> (type, allowed values | (lo, hi) | None)
SCHEMA: dict[str, tuple[str, Any]] = {
    # range
    "range.default":          ("enum", ("today", "yesterday", "24h", "7d", "30d", "90d", "custom")),
    "range.custom_start":     ("date_or_null", None),
    "range.custom_end":       ("date_or_null", None),
    "range.week_starts_on":   ("enum", ("sun", "mon")),
    "range.day_boundary":     ("enum", ("local", "utc")),
    "range.rolling_24h":      ("bool", None),
    "range.include_weekends": ("bool", None),
    "range.compare_previous": ("enum", ("off", "prev_day", "prev_week", "prev_month")),

    # display
    "display.theme":          ("enum", ("dark", "light", "auto")),
    "display.accent":         ("enum", ("violet", "blue", "green", "orange", "pink")),
    "display.density":        ("enum", ("compact", "normal", "spacious")),
    "display.font_scale":     ("float", (0.85, 1.25)),
    "display.currency":       ("enum", ("USD", "EUR", "GBP", "JPY", "CAD", "AUD")),
    "display.cost_format":    ("enum", ("with_cents", "whole")),
    "display.token_format":   ("enum", ("compact", "full")),
    "display.locale":         ("str", None),
    "display.show_token_bar":     ("bool", None),
    "display.show_cache_info":    ("bool", None),
    "display.show_burn_rate":     ("bool", None),
    "display.show_model":         ("bool", None),
    "display.show_project":       ("bool", None),
    "display.show_sessions_today": ("bool", None),
    "display.animations":         ("bool", None),

    # budgets
    "budgets.daily_limit":    ("float", (0.0, 1e9)),
    "budgets.weekly_limit":   ("float", (0.0, 1e9)),
    "budgets.monthly_limit":  ("float", (0.0, 1e9)),
    "budgets.alert_at_pct":   ("int", (1, 100)),
    "budgets.notify":         ("bool", None),
    "budgets.tray_warn_color": ("bool", None),
    "budgets.audio_alert":    ("bool", None),
    "budgets.quiet_hours":    ("enum", ("off", "22-08", "23-07", "00-09", "custom")),
    "budgets.quiet_start":    ("int", (0, 23)),
    "budgets.quiet_end":      ("int", (0, 23)),

    # behavior
    "behavior.refresh_seconds":      ("int", (0, 3600)),
    "behavior.auto_hide":            ("bool", None),
    "behavior.auto_hide_delay_ms":   ("int", (0, 10000)),
    "behavior.always_on_top":        ("bool", None),
    "behavior.show_in_taskbar":      ("bool", None),
    "behavior.live_threshold_seconds": ("int", (5, 600)),
    "behavior.start_minimized":      ("bool", None),
    "behavior.confirm_quit":         ("bool", None),
    "behavior.click_through":        ("bool", None),
    "behavior.single_instance":      ("bool", None),
    "behavior.open_dashboard_on_click": ("bool", None),

    # projects
    "projects.allowlist":     ("list_str", None),
    "projects.denylist":      ("list_str", None),
    "projects.anonymize":     ("bool", None),
    "projects.top_n":         ("int", (1, 100)),

    # keyboard
    "keyboard.enable_global":    ("bool", None),
    "keyboard.hotkey_toggle":    ("str", None),
    "keyboard.hotkey_refresh":   ("str", None),
    "keyboard.hotkey_settings":  ("str", None),
    "keyboard.hotkey_quit":      ("str", None),
    "keyboard.hotkey_export":    ("str", None),
    "keyboard.hotkey_copy_cost": ("str", None),
    "keyboard.vim_keys":         ("bool", None),

    # data
    "data.log_level":          ("enum", ("OFF", "ERROR", "WARN", "INFO", "DEBUG")),
    "data.log_retention_days": ("int", (1, 365)),
    "data.anonymize_logs":     ("bool", None),
    "data.cache_ttl_seconds":  ("int", (1, 60)),
    "data.telemetry":          ("bool", None),

    # window
    "window.anchor":            ("enum", ("bottom-right", "top-right",
                                          "bottom-left", "top-left",
                                          "top-center", "center")),
    "window.margin_px":         ("int", (0, 200)),
    "window.preferred_monitor": ("int", (0, 16)),
    "window.remember_position": ("bool", None),
    "window.width":             ("int", (260, 800)),
    "window.height":            ("int", (320, 1200)),
    "window.opacity":           ("float", (0.5, 1.0)),

    # legacy ui (still accepted for back-compat)
    "ui.pinned":         ("bool", None),
    "ui.start_on_login": ("bool", None),

    # sources sub-fields
    "sources.local.enabled":               ("bool", None),
    "sources.anthropic_api.enabled":       ("bool", None),
    "sources.anthropic_api.poll_seconds":  ("int", (10, 3600)),
    "sources.cost_mode":                   ("enum", ("equivalent", "actual_api", "both")),
}


def _coerce(path: str, value: Any) -> Any:
    spec = SCHEMA.get(path)
    if spec is None:
        raise ValueError(f"unknown setting: {path}")
    typ, arg = spec
    if typ == "bool":
        return bool(value)
    if typ == "int":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{path} expects integer, got {value!r}")
        if arg is not None:
            lo, hi = arg
            if not (lo <= n <= hi):
                raise ValueError(f"{path} must be in [{lo}, {hi}], got {n}")
        return n
    if typ == "float":
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{path} expects number, got {value!r}")
        if arg is not None:
            lo, hi = arg
            if not (lo <= f <= hi):
                raise ValueError(f"{path} must be in [{lo}, {hi}], got {f}")
        return f
    if typ == "enum":
        if value not in arg:
            raise ValueError(f"{path} must be one of {arg}, got {value!r}")
        return value
    if typ == "str":
        if not isinstance(value, str):
            raise ValueError(f"{path} expects string, got {value!r}")
        return value
    if typ == "list_str":
        if not isinstance(value, list):
            raise ValueError(f"{path} expects list, got {value!r}")
        return [str(x) for x in value]
    if typ == "date_or_null":
        if value in (None, ""):
            return None
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError(f"{path} expects YYYY-MM-DD or null, got {value!r}")
        return value
    raise ValueError(f"unhandled schema type for {path}: {typ}")


def set_many(updates: dict[str, Any]) -> dict[str, Any]:
    """Apply many `dotted.path -> value` updates atomically. Returns a dict
    mapping each path to the coerced stored value. Raises ValueError if
    any single update fails — nothing is saved in that case."""
    coerced: dict[str, Any] = {}
    for path, val in updates.items():
        coerced[path] = _coerce(path, val)
    # All validated — now apply to a fresh cfg copy + save once.
    cfg = json.loads(json.dumps(load()))
    for path, val in coerced.items():
        parts = path.split(".")
        cur = cfg
        for p in parts[:-1]:
            if not isinstance(cur.get(p), dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = val
    save(cfg)
    return coerced


# ---------- helpers for the admin key ----------

def get_admin_key() -> str | None:
    cfg = load()
    enc = cfg.get("sources", {}).get("anthropic_api", {}).get("admin_key_enc", "")
    if not enc:
        return None
    try:
        ct = base64.b64decode(enc)
    except ValueError:
        return None
    pt = _dpapi_unprotect(ct)
    if pt is None:
        return None
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError:
        return None


def set_admin_key(plaintext: str | None) -> bool:
    cfg = load()
    src = cfg.setdefault("sources", {}).setdefault("anthropic_api", {})
    if not plaintext:
        src["admin_key_enc"] = ""
        save(cfg)
        return True
    ct = _dpapi_protect(plaintext.encode("utf-8"))
    if ct is None:
        return False
    src["admin_key_enc"] = base64.b64encode(ct).decode("ascii")
    save(cfg)
    return True


def set_anthropic_enabled(enabled: bool) -> None:
    cfg = load()
    cfg.setdefault("sources", {}).setdefault("anthropic_api", {})["enabled"] = bool(enabled)
    save(cfg)


def has_admin_key() -> bool:
    return get_admin_key() is not None


def anthropic_enabled() -> bool:
    cfg = load()
    return bool(cfg.get("sources", {}).get("anthropic_api", {}).get("enabled"))


# ---------- UI prefs (legacy convenience wrappers) ----------

def is_pinned() -> bool:
    return bool(get_pref("ui.pinned", False))


def set_pinned(v: bool) -> None:
    cfg = load()
    cfg.setdefault("ui", {})["pinned"] = bool(v)
    save(cfg)


def start_on_login() -> bool:
    return bool(get_pref("ui.start_on_login", False))


def set_start_on_login(v: bool) -> None:
    cfg = load()
    cfg.setdefault("ui", {})["start_on_login"] = bool(v)
    save(cfg)


# ---------- Behavior accessors used by other modules ----------

def live_threshold_seconds() -> int:
    return int(get_pref("behavior.live_threshold_seconds", 60))


def always_on_top() -> bool:
    return bool(get_pref("behavior.always_on_top", True))


def auto_hide_enabled() -> bool:
    return bool(get_pref("behavior.auto_hide", True))


def click_through() -> bool:
    return bool(get_pref("behavior.click_through", False))


def project_allowlist() -> list[str]:
    v = get_pref("projects.allowlist", [])
    return v if isinstance(v, list) else []


def project_denylist() -> list[str]:
    v = get_pref("projects.denylist", [])
    return v if isinstance(v, list) else []


def anonymize_projects() -> bool:
    return bool(get_pref("projects.anonymize", False))


# ---------- Window state (popup pos/size/pinned) ----------

_WINDOW_DEFAULT: dict[str, Any] = {
    "x": None, "y": None, "w": None, "h": None, "pinned": False,
}


def load_window_state() -> dict[str, Any]:
    p = window_state_path()
    if not p.exists():
        return dict(_WINDOW_DEFAULT)
    try:
        raw = json.loads(p.read_text("utf-8"))
        if not isinstance(raw, dict):
            return dict(_WINDOW_DEFAULT)
        out = dict(_WINDOW_DEFAULT)
        for k in _WINDOW_DEFAULT.keys():
            if k in raw:
                out[k] = raw[k]
        return out
    except (OSError, json.JSONDecodeError):
        return dict(_WINDOW_DEFAULT)


def save_window_state(state: dict[str, Any]) -> dict[str, Any]:
    p = window_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = load_window_state()
    for k in _WINDOW_DEFAULT.keys():
        if k in state:
            v = state[k]
            if k == "pinned":
                cur[k] = bool(v)
            else:
                try:
                    cur[k] = int(v) if v is not None else None
                except (TypeError, ValueError):
                    pass
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return cur


# ---------- Windows "Run on login" registry helper ----------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "suprbar"


def apply_startup_setting(enable: bool, run_bat_path: str | None = None) -> bool:
    """Sync the HKCU Run registry value to the desired state."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if enable:
                if not run_bat_path:
                    return False
                winreg.SetValueEx(
                    key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                    f'"{run_bat_path}"',
                )
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        log.warning("registry write failed: %s", e)
        return False
