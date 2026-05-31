"""In-app auto-update for supr.bar — checks GitHub releases, downloads + verifies
the installer, launches it, and quits so the new version can take over.

Honest, unsigned, stdlib-only. No telemetry: the only request is an
unauthenticated GET of the public latest-release JSON, with a version-string
User-Agent and nothing about the user. Mirrors providers/anthropic_api.py for
the HTTP/retry shape.

Security posture (kept end-to-end — see download_and_apply / _download_validated):
  * HTTPS + host allowlist, re-validated on EVERY redirect hop (custom opener)
    and re-checked on the final download URL.
  * Installer asset name pinned to ^suprbar-setup-X.Y.Z.exe$ and state==uploaded;
    the name is reduced to a basename and the download path is asserted inside
    the fresh tempdir.
  * Integrity: SHA-256 vs asset.digest (primary), exact size match (fallback),
    refuse if neither is available; the file is re-hashed on disk immediately
    before launch (closes the download→launch TOCTOU window).
  * 200 MB size ceiling on both Content-Length and streamed bytes.
  * Fresh tempdir per run (no predictable shared path), exact verified file
    launched, tempdir cleaned on any failure — never a partial apply.
  * Frozen-only apply (a source checkout is updated via git, not the installer);
    also gated at the /api/update/apply route.
  * Unauthenticated: no Authorization header, no token in the binary. The
    state-changing routes are CSRF-guarded same-origin in server.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, config

log = logging.getLogger("suprbar.updater")

GITHUB_API = "https://api.github.com"
REPO = "imbafls/suprbar"
LATEST_URL = f"{GITHUB_API}/repos/{REPO}/releases/latest"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 20
DOWNLOAD_TIMEOUT = 120

# Per-attempt backoff schedule (seconds). Length determines max attempts.
_RETRY_BACKOFFS = (0.5, 1.0, 2.0)

ASSET_RE = re.compile(r"^suprbar-setup-\d+\.\d+\.\d+\.exe$")
ALLOWED_HOSTS = frozenset((
    "api.github.com", "github.com", "objects.githubusercontent.com",
))
MAX_ASSET_BYTES = 200 * 1024 * 1024   # 200 MB sanity ceiling

_lock = threading.Lock()
# Cached last status — same shape check_for_update() returns.
_last_status: dict | None = None
# Set by __main__ via set_quit_fn(shutdown_app); used by apply to quit cleanly.
_quit_fn = None


# ---------------------------------------------------------------- wiring ----

def set_quit_fn(fn) -> None:
    """Register the app's clean-shutdown function (shutdown_app from __main__).

    Called once at startup, the same place server.set_quit_callback is wired."""
    global _quit_fn
    _quit_fn = fn


# ---------------------------------------------------------------- version ----

def _parse_semver(s: str) -> tuple:
    """Parse 'v1.2.3', '1.2.3-rc1', etc. into ((1,2,3), is_final, pre)."""
    s = (s or "").strip().lstrip("vV")
    m = re.match(r"^(\d+(?:\.\d+)*)(?:[-+.]?(.*))?$", s)
    if not m:
        return ((0, 0, 0), 1, "")
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = (nums + (0, 0, 0))[:3]
    pre = m.group(2) or ""
    is_final = 1 if not pre else 0          # a final release outranks a pre-release
    return (nums, is_final, pre)


def _compare_versions(a: str, b: str) -> int:
    """Return -1/0/1 for a<b / a==b / a>b. Leading 'v' stripped; pre-release
    suffix ranks below the same bare numbers."""
    pa, pb = _parse_semver(a), _parse_semver(b)
    return (pa > pb) - (pa < pb)


# ---------------------------------------------------- host / asset checks ----

def _host_ok(url: str) -> bool:
    """True only for https URLs whose host is in the allowlist."""
    try:
        sp = urlsplit(url)
    except Exception:  # noqa: BLE001
        return False
    return sp.scheme == "https" and sp.hostname in ALLOWED_HOSTS


def _pick_installer_asset(release: dict) -> dict | None:
    """First asset whose name matches the installer regex, state==uploaded,
    https url on an allowed host."""
    for a in release.get("assets", []) or []:
        name = a.get("name", "") or ""
        url = a.get("browser_download_url", "") or ""
        if a.get("state") != "uploaded":
            continue
        if not ASSET_RE.match(name):
            continue
        if not _host_ok(url):
            continue
        return a
    return None


# ------------------------------------------------------------- HTTP layer ----

class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate https + host allowlist on EVERY redirect hop, not just the
    final URL. GitHub download URLs 302 from github.com to
    objects.githubusercontent.com (both allowed); refuse the first hop that
    escapes the allowlist instead of trusting a post-hoc geturl() check."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_ok(newurl):
            raise urllib.error.URLError(
                f"update redirect to disallowed host: {urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Opener used for every updater request so redirect hops are host-validated.
_opener = urllib.request.build_opener(_AllowlistRedirectHandler())


def _get_json_once(url: str) -> dict:
    """One attempt — raw HTTP GET of the latest-release JSON."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", API_VERSION)
    req.add_header("user-agent", f"suprbar/{__version__}")  # GitHub 403s w/o UA
    with _opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    """Retrying wrapper around ``_get_json_once``.

    Retries on URLError (network blips) and HTTP 5xx. 4xx responses (incl.
    403 rate-limit / 404 no-release) are NOT retried. Identical retry skeleton
    to anthropic_api._http_get.
    """
    last_exc: Exception | None = None
    for attempt, backoff in enumerate(_RETRY_BACKOFFS):
        try:
            return _get_json_once(url)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code < 500:
                raise            # 4xx — don't retry, surface immediately
            log.info("update check attempt %d/%d HTTP %d; backing off %.1fs",
                     attempt + 1, len(_RETRY_BACKOFFS), e.code, backoff)
        except urllib.error.URLError as e:
            last_exc = e
            log.info("update check attempt %d/%d network %s; backing off %.1fs",
                     attempt + 1, len(_RETRY_BACKOFFS), e.reason, backoff)
        except TimeoutError as e:
            last_exc = e
            log.info("update check attempt %d/%d timeout; backing off %.1fs",
                     attempt + 1, len(_RETRY_BACKOFFS), backoff)
        time.sleep(backoff)
    # Final attempt — let any exception escape to the caller
    try:
        return _get_json_once(url)
    except Exception as e:  # noqa: BLE001
        if last_exc is not None:
            raise last_exc from e
        raise


# ---------------------------------------------------------------- status ----

def _empty_status(error: str | None = None) -> dict:
    """A status dict with the documented shape; available=False, optional error."""
    return {
        "current": __version__,
        "latest": None,
        "available": False,
        "asset_name": None,
        "asset_url": None,
        "size": None,
        "notes_url": None,
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def check_for_update() -> dict:
    """Hit the latest-release API and return a status dict (never raises).

    On any network/parse failure returns a status with ``error`` set and
    ``available=False``. Persists ``updates.last_check`` and caches the result
    into ``_last_status``.
    """
    global _last_status
    try:
        release = _get_json(LATEST_URL)
    except urllib.error.HTTPError as e:
        st = _empty_status(f"github http {e.code}")
    except Exception as e:  # noqa: BLE001 — network/timeout/parse
        st = _empty_status(f"check failed: {e}")
    else:
        tag = str(release.get("tag_name") or "").strip()
        notes = release.get("html_url") or f"https://github.com/{REPO}/releases"
        asset = _pick_installer_asset(release)
        newer = bool(tag) and _compare_versions(tag, __version__) > 0
        skip = config.get_pref("updates.skip_version", "") or ""
        st = _empty_status()
        st["latest"] = tag.lstrip("vV") or None
        st["notes_url"] = notes
        if newer and asset:
            st["available"] = (st["latest"] != (skip.lstrip("vV") or None))
            st["asset_name"] = asset.get("name")
            st["asset_url"] = asset.get("browser_download_url")
            st["size"] = int(asset.get("size") or 0) or None
            st["_digest"] = asset.get("digest")   # "sha256:…" or None (internal)
    # Persist throttle/state; never let a config error mask the result.
    try:
        config.set_many({"updates.last_check": st["checked_at"]})
    except Exception:  # noqa: BLE001
        log.debug("could not persist updates.last_check", exc_info=True)
    with _lock:
        _last_status = st
    return st


def cached_status() -> dict | None:
    """Return a copy of the last check result, or None if none yet."""
    with _lock:
        return dict(_last_status) if _last_status else None


# ----------------------------------------------------- frozen / updatable ----

def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_updatable() -> bool:
    """Only frozen (installed/portable) builds can swap the exe. Running from
    source (pythonw -m suprbar) is a dev checkout — never auto-update."""
    return _is_frozen()


# ---------------------------------------------------------------- apply ----

def _download_validated(url, dest: Path, size: int, digest) -> tuple[bool, str]:
    """Stream the asset to ``dest``, validating host (post-redirect), size
    ceiling and integrity. Returns (ok, why)."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("user-agent", f"suprbar/{__version__}")
    # _opener validates https + host on EVERY redirect hop (github.com →
    # objects.githubusercontent.com); we re-check the final URL too as a backstop.
    with _opener.open(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        final_url = resp.geturl()
        if not _host_ok(final_url):
            return (False, "redirect escaped host allowlist")
        clen = resp.headers.get("Content-Length")
        if clen and int(clen) > MAX_ASSET_BYTES:
            return (False, "asset too large")
        h = hashlib.sha256()
        written = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ASSET_BYTES:
                    return (False, "asset exceeded size ceiling")
                h.update(chunk)
                f.write(chunk)
    # Primary integrity check: SHA-256 vs asset.digest when present.
    if digest and isinstance(digest, str) and digest.startswith("sha256:"):
        if h.hexdigest().lower() != digest.split("sha256:", 1)[1].lower():
            return (False, "sha256 mismatch")
    elif size:
        # Fallback (weak) when GitHub has no digest. GitHub now publishes a
        # sha256 digest for new assets, so this path is rarely hit; HTTPS + the
        # host allowlist remain the channel-integrity boundary either way.
        log.warning("no sha256 digest on release asset; falling back to size match")
        if written != size:
            return (False, "size mismatch")
    else:
        return (False, "no integrity reference (no digest, no size)")
    return (True, "")


def _file_matches_digest(path: Path, digest) -> bool:
    """Re-hash the on-disk file vs a sha256 digest. Returns True when there is
    no sha256 digest to check against (the size-only path already validated)."""
    if not (digest and isinstance(digest, str) and digest.startswith("sha256:")):
        return True
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest().lower() == digest.split("sha256:", 1)[1].lower()


def _launch_installer(path: Path) -> None:
    """Spawn the Inno installer detached so it outlives this process."""
    DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    args = [str(path),
            "/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS",
            "/RESTARTAPPLICATIONS", "/NORESTART"]
    subprocess.Popen(args, creationflags=DETACHED, close_fds=True,
                     cwd=str(path.parent))


def _delayed_quit(fn) -> None:
    """Quit after a short delay so the HTTP response flushes + Popen settles."""
    time.sleep(0.6)
    try:
        if fn:
            fn()
    except Exception:  # noqa: BLE001
        log.exception("quit during update failed")


def cleanup_stale_downloads(max_age_hours: int = 24) -> None:
    """Best-effort sweep of leftover ``suprbar_upd_*`` temp dirs from prior
    updates. The apply path intentionally keeps its tempdir (the installer needs
    the file), so a completed or interrupted update can leave one behind; clean
    anything older than ``max_age_hours`` on launch. Never raises."""
    try:
        base = Path(tempfile.gettempdir())
        cutoff = time.time() - max_age_hours * 3600
        for p in base.glob("suprbar_upd_*"):
            try:
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        log.debug("stale-download cleanup failed", exc_info=True)


def download_and_apply(quit_fn=None) -> dict:
    """Download → validate (host+name+size[+digest]) → launch installer →
    clean-quit. Returns {ok, message?, error?}. Runs ON A WORKER THREAD
    (spawned by the server route / tray menu); it is allowed to block."""
    st = cached_status() or check_for_update()
    if st.get("error"):
        return {"ok": False, "error": st["error"]}
    if not st.get("available"):
        return {"ok": False, "error": "no update available"}
    if not is_updatable():
        return {"ok": False,
                "error": "running from source — update via git, not the installer"}

    url = st.get("asset_url") or ""
    name = Path(st.get("asset_name") or "").name   # basename — defense in depth
    size = int(st.get("size") or 0)
    digest = st.get("_digest")  # may be absent if status came over HTTP; see note

    if not (ASSET_RE.match(name) and _host_ok(url)):
        return {"ok": False, "error": "asset failed validation"}

    tmpdir = Path(tempfile.mkdtemp(prefix="suprbar_upd_"))
    dest = tmpdir / name
    # Defense in depth: the basename + anchored ASSET_RE already forbid path
    # separators, but assert the join stayed inside the fresh tempdir.
    if dest.parent != tmpdir or dest.name != name:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"ok": False, "error": "asset name failed path check"}
    try:
        ok, why = _download_validated(url, dest, size, digest)
        if not ok:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return {"ok": False, "error": why}
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"ok": False, "error": f"download failed: {e}"}

    # Re-verify the bytes on disk immediately before launch — closes the
    # download→launch TOCTOU window when a sha256 digest is available.
    if not _file_matches_digest(dest, digest):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"ok": False, "error": "installer changed after verification"}

    try:
        _launch_installer(dest)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not launch installer: {e}"}

    # Quit so files unlock; installer (/CLOSEAPPLICATIONS /RESTARTAPPLICATIONS)
    # relaunches the new exe. Quit on a short-delay daemon thread so the HTTP
    # response (already sent by the route) and this function both return first.
    fn = quit_fn or _quit_fn
    threading.Thread(target=_delayed_quit, args=(fn,), daemon=True).start()
    return {"ok": True, "message": f"updating to v{st.get('latest')} — restarting…"}
