"""End-to-end build: brand assets → PyInstaller → (optional) Inno Setup.

Run:
  python scripts/build_exe.py

Outputs:
  dist/suprbar/suprbar.exe                    (standalone bundle)
  dist/suprbar-setup-<version>.exe            (installer, if Inno Setup found)

Requirements (installed automatically via pip if missing):
  - pyinstaller >= 6

Optional:
  - Inno Setup 6 (iscc on PATH)  → adds the wrapped installer step
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def step(msg: str) -> None:
    print(f"\n\033[1;36m▶ {msg}\033[0m")


def run(cmd: list[str], **kw) -> int:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=ROOT, **kw).returncode


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        pass
    step("Installing PyInstaller (one-time)")
    code = run([sys.executable, "-m", "pip", "install",
                "--quiet", "pyinstaller>=6"])
    if code != 0:
        sys.exit("PyInstaller install failed")


def build_brand() -> None:
    step("Building brand assets (icon + PNGs)")
    code = run([sys.executable, "scripts/build_brand.py"])
    if code != 0:
        sys.exit("brand build failed")


def clean() -> None:
    step("Cleaning previous build/")
    for d in ("build", "dist"):
        p = ROOT / d
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            print(f"  removed {p.relative_to(ROOT)}")


def build_exe() -> Path:
    step("Running PyInstaller (this takes a minute)")
    code = run([sys.executable, "-m", "PyInstaller",
                "--clean", "--noconfirm", "suprbar.spec"])
    if code != 0:
        sys.exit("PyInstaller failed")
    out = ROOT / "dist" / "suprbar" / "suprbar.exe"
    if not out.exists():
        sys.exit("expected dist/suprbar/suprbar.exe; not found")
    print(f"  → {out.relative_to(ROOT)}")
    return out


def build_installer() -> Path | None:
    step("Looking for Inno Setup (iscc)")
    iscc = shutil.which("iscc") or shutil.which("ISCC.exe")
    if not iscc:
        for p in (r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                  r"C:\Program Files\Inno Setup 6\ISCC.exe"):
            if Path(p).exists():
                iscc = p; break
    if not iscc:
        print("  Inno Setup not found — skipping installer wrap.")
        print("  Install from https://jrsoftware.org/isdl.php and re-run.")
        return None
    print(f"  using {iscc}")
    step("Compiling installer.iss")
    code = run([iscc, "installer.iss"])
    if code != 0:
        sys.exit("Inno Setup compile failed")
    # Find the produced installer
    candidates = sorted((ROOT / "dist").glob("suprbar-setup-*.exe"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        print("  installer not found in dist/")
        return None
    print(f"  → {candidates[0].relative_to(ROOT)}")
    return candidates[0]


def main() -> None:
    ensure_pyinstaller()
    build_brand()
    clean()
    exe = build_exe()
    installer = build_installer()

    print()
    print("=" * 60)
    print("BUILD COMPLETE")
    print("  standalone bundle:  dist/suprbar/")
    print(f"  standalone exe:     {exe.relative_to(ROOT)}")
    if installer:
        print(f"  installer:          {installer.relative_to(ROOT)}")
    print()
    print("Standalone bundle is portable — copy dist/suprbar/ anywhere and")
    print("run suprbar.exe. Installer is a single-file Setup wizard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
