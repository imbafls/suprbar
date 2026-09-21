"""Regenerate the hosted rate table (repo-root pricing.json) from pricing.py.

Run after editing rates:

    python scripts/export_pricing.py

``tests/test_pricing_table.py`` fails when the file diverges from the
built-in tables, and the release workflow ships it as-is — so the hosted
table and the code stay in lockstep.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from suprbar import pricing  # noqa: E402


def build() -> dict:
    return {
        "family": {k: dict(v) for k, v in pricing.PRICING.items()},
        "models": {k: dict(v) for k, v in pricing.MODEL_RATES.items()},
        "generic": [[pat, dict(rates)]
                    for pat, rates in pricing.GENERIC_MODEL_RATES],
    }


def main() -> int:
    out = ROOT / "pricing.json"
    payload = build()
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"wrote {out} "
          f"({len(payload['family'])} families, "
          f"{len(payload['models'])} models, "
          f"{len(payload['generic'])} generic patterns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
