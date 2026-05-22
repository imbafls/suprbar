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


def config_path() -> Path:
    return config_dir() / "config.json"


DEFAULTS: dict[str, Any] = {
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
            _cache = _merge_defaults(raw if isinstance(raw, dict) else {})
        except (OSError, json.JSONDecodeError) as e:
            log.warning("config load failed: %s — using defaults", e)
            _cache = json.loads(json.dumps(DEFAULTS))
        return _cache


def save(cfg: dict[str, Any]) -> None:
    with _lock:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        global _cache
        _cache = cfg


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
