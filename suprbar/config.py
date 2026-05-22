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


SCHEMA_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "sources": {
        "local": {"enabled": True},
        "anthropic_api": {
            "enabled": False,
            # admin_key_enc holds a DPAPI-encrypted blob (base64). Never the
            # plaintext key. Use set_admin_key() / get_admin_key() to access.
            "admin_key_enc": "",
        },
    },
    "ui": {
        "pinned": False,           # if True, popup ignores auto-hide on blur
        "start_on_login": False,
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
        # CRYPTPROTECT_UI_FORBIDDEN = 0x1
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
    out = json.loads(json.dumps(DEFAULTS))  # deep copy
    for k, v in d.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            for sk, sv in v.items():
                if isinstance(sv, dict) and isinstance(out[k].get(sk), dict):
                    out[k][sk].update(sv)
                else:
                    out[k][sk] = sv
        else:
            out[k] = v
    return out


def _migrate(d: dict[str, Any]) -> dict[str, Any]:
    """Bump older configs to the current schema."""
    if not isinstance(d, dict):
        return json.loads(json.dumps(DEFAULTS))
    v = d.get("schema_version")
    if not isinstance(v, int) or v < 1:
        # Pre-versioned config: ensure shape is sane, then stamp v1.
        if "sources" not in d or not isinstance(d.get("sources"), dict):
            d["sources"] = json.loads(json.dumps(DEFAULTS["sources"]))
        if "ui" not in d or not isinstance(d.get("ui"), dict):
            d["ui"] = json.loads(json.dumps(DEFAULTS["ui"]))
        d["schema_version"] = 1
        log.info("config migrated to schema_version=1")
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
        # Backup existing config (best-effort) before atomic replace.
        if p.exists():
            try:
                shutil.copy2(p, p.with_suffix(".json.bak"))
            except OSError as e:
                log.warning("config backup failed: %s", e)
        tmp = p.with_suffix(".json.tmp")
        # Ensure schema_version is stamped.
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
        existing_key = None
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
    """Store the admin API key. Pass None or "" to clear it. Returns success."""
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


# ---------- UI prefs ----------

def is_pinned() -> bool:
    cfg = load()
    return bool(cfg.get("ui", {}).get("pinned", False))


def set_pinned(v: bool) -> None:
    cfg = load()
    cfg.setdefault("ui", {})["pinned"] = bool(v)
    save(cfg)


def start_on_login() -> bool:
    cfg = load()
    return bool(cfg.get("ui", {}).get("start_on_login", False))


def set_start_on_login(v: bool) -> None:
    cfg = load()
    cfg.setdefault("ui", {})["start_on_login"] = bool(v)
    save(cfg)


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
    # Only accept known keys + type coerce.
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
        import winreg  # local import (Windows-only stdlib)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if enable:
                if not run_bat_path:
                    return False
                # Quote the path; Windows resolves the .bat itself
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
