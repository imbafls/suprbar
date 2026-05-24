"""PyInstaller / direct entry point — keeps suprbar/__main__.py as a package module."""

from __future__ import annotations

import sys

from suprbar.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
