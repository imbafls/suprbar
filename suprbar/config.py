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
from pathlib import Path
from typing import Any

# wintypes is Windows-only at import time on some runners; the DPAPI helpers
# below are win32-gated, so a missing wintypes must not break module import.
try:
    from ctypes import wintypes
except (ImportError, ValueError):  # pragma: no cover - non-Windows runners
    wintypes = None  # type: ignore[assignment]

log = logging.getLogger("suprbar.config")


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "suprbar"


def config_path() -> Path:
    return config_dir() / "config.json"


SCHEMA_VERSION = 5

# v2.0 keeps five settings: sources (+ their API keys), the mini overlay,
# pin, start on login and the launch update check. Everything else that used
# to be a setting is fixed behaviour now (see the v2 design spec).
DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,

    "sources": {
        "local": {"enabled": True},
        "anthropic_api": {
            "enabled": False,
            "admin_key_enc": "",        # DPAPI-encrypted blob (base64)
        },
        "hermes": {"enabled": True},    # Hermes agent sessions.json
        "opencode": {"enabled": True},  # opencode SQLite sessions
        "openrouter": {                 # OpenRouter account usage (optional)
            "enabled": False,
            "key_enc": "",              # DPAPI-encrypted blob (base64)
        },
        "openai": {                     # OpenAI org costs via admin key (optional)
            "enabled": False,
            "key_enc": "",              # DPAPI-encrypted blob (base64)
        },
    },

    "ui": {
        "pinned": False,                # flyout stays open when focus leaves
        "start_on_login": False,
    },

    "mini": {
        "enabled": False,               # always-on-top rolling-24h chip
    },

    "updates": {
        "check_on_launch": True,        # launch + 6-hourly release check
        "last_check":      "",          # internal: ISO-8601 of last check
        "skip_version":    "",          # internal: release the user skipped
    },
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
# (mtime_ns, size) of config.json when _cache was read. The flyout and mini
# overlay run in their own processes and only read config; the tray process
# writes it. Re-reading on change keeps every process in step.
_cache_sig: tuple[int, int] | None = None


# ---------- DPAPI ----------

if wintypes is not None:  # Windows-only; the helpers below never run elsewhere

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
    """Project any pre-v5 config onto the v5 schema.

    Sources (enabled flags and encrypted key blobs) are kept verbatim, as are
    the mini overlay, start-on-login and update state. Turning auto-hide off
    used to mean "keep the flyout open", which is what pin means now, so it
    carries over as pin. Every other key is dropped.
    """
    if not isinstance(d, dict):
        return json.loads(json.dumps(DEFAULTS))
    v = d.get("schema_version")
    if isinstance(v, int) and v >= SCHEMA_VERSION:
        return d

    def section(name: str) -> dict[str, Any]:
        sec = d.get(name)
        return sec if isinstance(sec, dict) else {}

    ui, behavior = section("ui"), section("behavior")
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    sources = section("sources")
    if sources:
        out["sources"] = sources
    out["ui"] = {
        "pinned": bool(ui.get("pinned")) or behavior.get("auto_hide") is False,
        "start_on_login": bool(ui.get("start_on_login", False)),
    }
    if "enabled" in section("mini"):
        out["mini"] = {"enabled": bool(section("mini")["enabled"])}
    updates = {k: section("updates")[k]
               for k in ("check_on_launch", "last_check", "skip_version")
               if k in section("updates")}
    if updates:
        out["updates"] = updates
    log.info("config migrated to schema_version=%d", SCHEMA_VERSION)
    return out


def _file_sig(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def load(force: bool = False) -> dict[str, Any]:
    global _cache, _cache_sig
    with _lock:
        p = config_path()
        sig = _file_sig(p)
        if _cache is not None and not force and sig == _cache_sig:
            return _cache
        _cache_sig = sig
        if sig is None:
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
        global _cache, _cache_sig
        _cache = cfg
        _cache_sig = _file_sig(p)




# ---------- generic dotted-path access ----------

def get_pref(path: str, default: Any = None) -> Any:
    """Read a nested setting by dotted path, e.g. 'mini.enabled'."""
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

# (key path) -> type. The only settings there are.
SCHEMA: dict[str, str] = {
    "sources.local.enabled":         "bool",
    "sources.anthropic_api.enabled": "bool",
    "sources.hermes.enabled":        "bool",
    "sources.opencode.enabled":      "bool",
    "sources.openrouter.enabled":    "bool",
    "sources.openai.enabled":        "bool",
    "mini.enabled":                  "bool",
    "ui.pinned":                     "bool",
    "ui.start_on_login":             "bool",
    "updates.check_on_launch":       "bool",
    "updates.last_check":            "str",
    "updates.skip_version":          "str",
}


def _coerce(path: str, value: Any) -> Any:
    typ = SCHEMA.get(path)
    if typ is None:
        raise ValueError(f"unknown setting: {path}")
    # ValueError (not TypeError) throughout: callers turn it into a 400.
    if typ == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{path} expects true/false, got {value!r}")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{path} expects string, got {value!r}")  # noqa: TRY004
    return value


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






def get_source_key(source: str) -> str | None:
    """Read a DPAPI-encrypted source key (``sources.<source>.key_enc``)."""
    cfg = load()
    enc = cfg.get("sources", {}).get(source, {}).get("key_enc", "")
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


def set_source_key(source: str, plaintext: str | None) -> bool:
    """Encrypt + store a source key. Empty/None clears it."""
    cfg = load()
    src = cfg.setdefault("sources", {}).setdefault(source, {})
    if not plaintext:
        src["key_enc"] = ""
        save(cfg)
        return True
    ct = _dpapi_protect(plaintext.encode("utf-8"))
    if ct is None:
        return False
    src["key_enc"] = base64.b64encode(ct).decode("ascii")
    save(cfg)
    return True


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




# ---------- Mini overlay ----------

def mini_enabled() -> bool:
    return bool(get_pref("mini.enabled", False))


# ---------- Windows "Run on login" registry helper ----------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "suprbar"


def startup_registered(default: bool = False) -> bool:
    """Whether the HKCU Run value exists (the installer may have set it)."""
    if sys.platform != "win32":
        return default
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return default


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
