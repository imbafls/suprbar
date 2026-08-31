import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from suprbar.providers import opencode

SCHEMA = """
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    data TEXT
);
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    directory TEXT,
    title TEXT,
    project_id TEXT
);
CREATE TABLE project (
    id TEXT PRIMARY KEY,
    worktree TEXT,
    name TEXT
);
"""


def _ms(dt_seconds_ago: float = 0.0) -> int:
    return int((time.time() - dt_seconds_ago) * 1000)


def _msg(mid: str, sid: str, ts_ms: int, role: str = "assistant",
         model: str = "gpt-4o", cost: float = 0.05,
         inp: int = 100, out: int = 50, reasoning: int = 10,
         cache_read: int = 200, cache_write: int = 30) -> tuple:
    data = {
        "role": role,
        "modelID": model,
        "providerID": "openrouter",
        "tokens": {
            "input": inp, "output": out, "reasoning": reasoning,
            "cache": {"read": cache_read, "write": cache_write},
        },
        "cost": cost,
        "time": {"created": ts_ms},
    }
    import json
    return (mid, sid, ts_ms, ts_ms, json.dumps(data))


class OpencodeProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "opencode.db"
        con = sqlite3.connect(db)
        con.executescript(SCHEMA)
        con.execute("INSERT INTO project VALUES ('p1', 'C:/depot/_projects/supr.bar', '')")
        con.execute("INSERT INTO session VALUES ('s1', 'C:/depot/_projects/supr.bar', 'test session', 'p1')")
        con.execute("INSERT INTO session VALUES ('s2', 'C:/depot/_projects/wiki', 'other', 'px')")
        rows = [
            # today, priced, live (now-ish)
            _msg("m1", "s1", _ms(5)),
            # today, UNpriced but known generic model → estimated
            _msg("m2", "s1", _ms(40), model="openai/gpt-4o", cost=0.0,
                 inp=1_000_000, out=0, reasoning=0, cache_read=0, cache_write=0),
            # today, unpriced AND unknown model → stays 0 (tokens still count);
            # 90s old → outside the 60s live window, keeps the live test unambiguous
            _msg("m3", "s2", _ms(90), model="totally-unknown-9000", cost=0.0,
                 inp=100, out=0, reasoning=0, cache_read=0, cache_write=0),
            # non-assistant → ignored
            _msg("m4", "s1", _ms(20), role="user"),
            # yesterday → excluded by the midnight filter
            _msg("m5", "s1", _ms(26 * 3600)),
        ]
        con.executemany("INSERT INTO message VALUES (?,?,?,?,?)", rows)
        con.commit()
        con.close()

        self._orig_db_path = opencode._db_path
        self._orig_cache = opencode._cache
        self._orig_cache_ts = opencode._cache_ts
        opencode._db_path = lambda: db  # type: ignore[assignment]
        opencode._cache = None
        opencode._cache_ts = 0.0

    def tearDown(self):
        opencode._db_path = self._orig_db_path  # type: ignore[assignment]
        opencode._cache = self._orig_cache
        opencode._cache_ts = self._orig_cache_ts
        self.tmp.cleanup()

    def test_today_summary_shape_and_totals(self):
        out = opencode.today_summary()
        self.assertTrue(out["ok"])
        self.assertEqual(out["id"], "opencode")
        # m1 ($0.05) + m2 (gpt-4o 1M input = $2.50 estimated) — m3 unknown → 0
        self.assertAlmostEqual(out["cost_today"], 0.05 + 2.50, places=3)
        self.assertEqual(out["messages_today"], 3)  # m1, m2, m3
        # reasoning (10) folded into output (50)
        # m1 (100) + m2 (1M) + m3 (100) — m4 user-role and m5 yesterday excluded
        self.assertEqual(out["tokens_today"]["input"], 1_000_200)
        self.assertEqual(out["tokens_today"]["output"], 60)
        self.assertEqual(out["tokens_today"]["cache_read"], 200)
        self.assertEqual(out["tokens_today"]["cache_1h"], 30)
        self.assertEqual(out["extras"]["estimated_messages"], 1)
        # yesterday's message must not leak in (m5 has 100 in / 50 out)
        self.assertLess(out["tokens_today"]["output"], 200)

    def test_by_model_and_project(self):
        out = opencode.today_summary()
        models = {m["model"]: m for m in out["extras"]["by_model"]}
        self.assertIn("gpt-4o", models)
        self.assertIn("totally-unknown-9000", models)
        # unknown model contributed zero cost
        self.assertEqual(models["totally-unknown-9000"]["cost"], 0.0)
        projects = {p["project"]: p for p in out["extras"]["by_project"]}
        self.assertIn("supr.bar", projects)  # name falls back to worktree basename
        self.assertEqual(projects["supr.bar"]["messages"], 2)

    def test_live_session_detected(self):
        out = opencode.today_summary()
        live = out["extras"]["live_sessions"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["id"], "s1")
        self.assertGreater(live[0]["burn_rate_usd_per_hour"], 0)

    def test_missing_db_returns_empty_ok(self):
        opencode._db_path = lambda: Path(self.tmp.name) / "nope.db"  # type: ignore[assignment]
        opencode._cache = None
        out = opencode.today_summary()
        self.assertTrue(out["ok"])
        self.assertEqual(out["cost_today"], 0.0)
        st = opencode.self_test()
        self.assertFalse(st["ok"])  # diagnostics: db really missing

    def test_estimate_generic_cost_known_and_unknown(self):
        c = opencode.estimate_generic_cost("gpt-4o", 1_000_000, 0)
        self.assertAlmostEqual(c or 0, 2.50, places=6)
        self.assertIsNone(opencode.estimate_generic_cost("mystery-x", 100, 100))


if __name__ == "__main__":
    unittest.main()
