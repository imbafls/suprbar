"""Scanner correctness at the edges v2 relies on.

* today's totals reset at local midnight (the "stale numbers" bug class);
* whole-day ranges served from the per-day index equal an exact scan.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from suprbar import scanner

_REAL_DATETIME = scanner.datetime


def _rec(ts: datetime, session: str, model: str, inp: int, out: int = 0) -> str:
    return json.dumps({
        "timestamp": ts.isoformat(),
        "sessionId": session,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": inp // 10}},
    })


def _clock(fixed: datetime):
    """A datetime class whose now() is pinned (scanner uses datetime.now())."""
    class _Fixed(_REAL_DATETIME):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)
    return _Fixed


class _TempHome(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.home = root / "projects"
        (self.home / "C--work--app").mkdir(parents=True)
        (self.home / "C--work--site").mkdir(parents=True)
        self._patches = [
            mock.patch.object(scanner, "CLAUDE_HOME", self.home),
            mock.patch.object(scanner, "_index_path",
                              return_value=root / "scan-index.json"),
        ]
        for p in self._patches:
            p.start()
        scanner._file_cache.clear()
        scanner._cache_date = None
        scanner._index.clear()
        scanner._index_loaded = False

    def tearDown(self):
        for p in self._patches:
            p.stop()
        scanner._file_cache.clear()
        scanner._cache_date = None
        scanner._index.clear()
        scanner._index_loaded = False
        self._td.cleanup()

    def write(self, rel: str, lines: list[str], mtime: datetime) -> Path:
        p = self.home / rel
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(p, (mtime.timestamp(), mtime.timestamp()))
        return p


class MidnightRolloverTest(_TempHome):
    def test_today_resets_at_local_midnight(self):
        local_midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
        before = local_midnight - timedelta(minutes=30)   # 23:30 day D
        after = local_midnight + timedelta(minutes=30)    # 00:30 day D+1
        p = self.write("C--work--app/s1.jsonl",
                       [_rec(before, "s1", "claude-sonnet-4-6", 1_000_000)],
                       before + timedelta(minutes=5))

        with mock.patch.object(scanner, "datetime",
                               _clock(before + timedelta(minutes=15))):
            day_d = scanner.today_summary()
        self.assertEqual(day_d["today"]["messages"], 1)
        self.assertGreater(day_d["today"]["cost"], 0)

        # A new record lands after midnight; the same (cached) scanner must
        # count only it as "today" — not carry day D's usage over.
        with p.open("a", encoding="utf-8") as f:
            f.write(_rec(after, "s1", "claude-sonnet-4-6", 2_000_000) + "\n")
        os.utime(p, (after.timestamp(), after.timestamp()))
        with mock.patch.object(scanner, "datetime",
                               _clock(after + timedelta(minutes=15))):
            day_d1 = scanner.today_summary()
        self.assertEqual(day_d1["today"]["messages"], 1)
        self.assertAlmostEqual(day_d1["today"]["cost"],
                               2 * day_d["today"]["cost"], places=6)
        self.assertEqual(day_d1["today_date"],
                         (local_midnight.date()).isoformat())


class IndexMatchesExactScanTest(_TempHome):
    def test_whole_day_index_equals_exact_scan(self):
        today = datetime.now().astimezone().replace(
            hour=12, minute=0, second=0, microsecond=0)
        models = ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5")
        lines_a, lines_b = [], []
        for d in range(1, 12):  # 1..11 days ago, several models + sessions
            ts = today - timedelta(days=d, hours=d % 5)
            lines_a.append(_rec(ts, f"a{d % 3}", models[d % 3], 100_000 * d, 20_000))
            lines_b.append(_rec(ts + timedelta(minutes=7), "b1",
                                models[(d + 1) % 3], 50_000, 5_000 * d))
        self.write("C--work--app/a.jsonl", lines_a, today - timedelta(days=1))
        self.write("C--work--site/b.jsonl", lines_b, today - timedelta(days=1))

        end = today.replace(hour=0) + timedelta(days=1)       # local midnight
        start = end - timedelta(days=8)
        indexed = scanner.range_summary(
            "custom", custom_start=start.isoformat(), custom_end=end.isoformat())
        self.assertTrue(scanner._index, "whole-day range should use the index")
        # One second earlier start: not midnight-aligned, so an exact scan
        # (no record sits in that second).
        exact = scanner.range_summary(
            "custom", custom_start=(start - timedelta(seconds=1)).isoformat(),
            custom_end=end.isoformat())

        for k in ("messages", "input", "output", "cache_read", "tokens",
                  "sessions", "projects"):
            self.assertEqual(indexed["totals"][k], exact["totals"][k], k)
        self.assertAlmostEqual(indexed["totals"]["cost"],
                               exact["totals"]["cost"], places=6)
        self.assertEqual(
            {m["model"]: m["messages"] for m in indexed["by_model"]},
            {m["model"]: m["messages"] for m in exact["by_model"]})
        self.assertEqual(
            {p["project"]: round(p["cost"], 6) for p in indexed["by_project"]},
            {p["project"]: round(p["cost"], 6) for p in exact["by_project"]})
        self.assertEqual(indexed["totals"]["messages"], 14)  # 7 days x 2 files


if __name__ == "__main__":
    unittest.main()
