"""Cut a supr.bar release: version bumps, changelog, commit, tag.

The tag push is what triggers .github/workflows/release.yml (builds the
installer + portable zip, checksums, latest.json, and creates the GitHub
Release with the matching CHANGELOG section as its body).

Usage:
    python scripts/cut_release.py 0.14.0 --title "mini overlay + budgets"
    python scripts/cut_release.py 0.14.0 --title "..." --dry-run
    python scripts/cut_release.py 0.14.0 --title "..." --allow-dirty --push

What it touches (nothing else):
    suprbar/__init__.py   __version__
    pyproject.toml        version
    installer.iss         fallback #define MyAppVersion
    README.md             "> **Status:** vX.Y" token
    CHANGELOG.md          ensures a "## vX.Y.Z — title" section
    pricing.json          regenerated from pricing.py (hosted rate table)

Without --push everything stays local and the push commands are printed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True,
                          capture_output=True)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _write(rel: str, text: str) -> None:
    (ROOT / rel).write_text(text, encoding="utf-8")


def _sub_once(rel: str, pattern: str, repl: str, pattern_flags: int = 0) -> None:
    text = _read(rel)
    new, n = re.subn(pattern, repl, text, count=1, flags=pattern_flags)
    if n != 1:
        raise SystemExit(f"cut_release: pattern not found in {rel}: {pattern}")
    _write(rel, new)


def current_version() -> str:
    m = re.search(r'__version__ = "([^"]+)"', _read("suprbar/__init__.py"))
    if not m:
        raise SystemExit("cut_release: could not read __version__ from "
                         "suprbar/__init__.py")
    return m.group(1)


def ensure_changelog(version: str, title: str) -> bool:
    """Add a stub section when the changelog doesn't have one yet."""
    text = _read("CHANGELOG.md")
    if re.search(rf"^## v{re.escape(version)}\b", text, re.M):
        return False
    header = "# supr.bar CHANGELOG\n"
    if not text.startswith(header):
        raise SystemExit("cut_release: unexpected CHANGELOG.md header")
    stub = (f"\n## v{version} — {title}\n\n"
            "- TODO: describe this release.\n")
    _write("CHANGELOG.md", text.replace(header, header + stub, 1))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("version", help="new version, e.g. 0.14.0 (no leading v)")
    ap.add_argument("--title", default="", help="short release title")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="proceed with uncommitted changes in the tree")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change, touch nothing")
    ap.add_argument("--push", action="store_true",
                    help="push the commit + tag (triggers the release workflow)")
    args = ap.parse_args()

    version = args.version.lstrip("v")
    if not SEMVER.match(version):
        raise SystemExit(f"cut_release: '{args.version}' is not X.Y.Z")
    title = args.title or "release"
    major_minor = ".".join(version.split(".")[:2])

    old = current_version()
    if version == old:
        raise SystemExit(f"cut_release: version is already {old}")

    status = _run(["git", "status", "--porcelain"]).stdout.strip()
    if status and not args.allow_dirty and not args.dry_run:
        print(status, file=sys.stderr)
        raise SystemExit("cut_release: working tree is dirty — commit first, "
                         "or pass --allow-dirty to include it in the release commit")

    tag = f"v{version}"
    if _run(["git", "tag", "-l", tag]).stdout.strip():
        raise SystemExit(f"cut_release: tag {tag} already exists")

    print(f"cut_release: {old} -> {version}  ({tag})")
    if args.dry_run:
        has_section = bool(re.search(
            rf"^## v{re.escape(version)}\b", _read("CHANGELOG.md"), re.M))
        print("  would bump suprbar/__init__.py, pyproject.toml, installer.iss")
        print(f"  would set README status to v{major_minor}")
        print("  would ensure CHANGELOG section"
              + ("" if has_section else " (stub)"))
        print("  would regenerate pricing.json")
        print(f"  would commit + tag: v{version}: {title}")
        return 0

    _sub_once("suprbar/__init__.py", r'__version__ = "[^"]+"',
              f'__version__ = "{version}"')
    _sub_once("pyproject.toml", r'^version = "[^"]+"',
              f'version = "{version}"', re.M)
    _sub_once("installer.iss", r'#define MyAppVersion "[^"]+"',
              f'#define MyAppVersion "{version}"')
    _sub_once("README.md", r'(> \*\*Status:\*\* )v\d+\.\d+(?:\.\d+)?',
              rf'\g<1>v{major_minor}')
    added = ensure_changelog(version, title)

    subprocess.run([sys.executable, str(ROOT / "scripts" / "export_pricing.py")],
                   cwd=ROOT, check=True, text=True)

    print("changed:")
    print(_run(["git", "status", "--short"]).stdout.rstrip())
    print("  (changelog section added)" if added
          else "  (changelog section already present)")

    _run(["git", "add", "-A"])
    _run(["git", "commit", "-m", f"v{version}: {title}"])
    _run(["git", "tag", tag])
    print(f"cut_release: committed + tagged {tag}")

    push_ok = args.push
    if push_ok:
        _run(["git", "push", "origin", "HEAD"])
        _run(["git", "push", "origin", tag])
        print(f"cut_release: pushed — release workflow is building {tag}")
    else:
        print("cut_release: not pushed. To publish:")
        print("  git push origin HEAD && git push origin " + tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
