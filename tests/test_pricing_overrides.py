"""Pricing overrides + incremental scanner tail + per-project budget tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from suprbar import config, pricing, scanner


def _usage_record(ts: datetime, session: str = "s1",
                  model: str = "claude-sonnet-4-6",
                  inp: int = 1_000_000, out: int = 0) -> str:
    return json.dumps({
        "timestamp": ts.isoformat(),
        "sessionId": session,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out}},
    })


class PricingOverrideTest(unittest.TestCase):
    def test_parse_override_payload_merges_and_rejects_garbage(self):
        with mock.patch.dict(pricing.MODEL_RATES, {}, clear=False), \
                mock.patch.dict(pricing.PRICING, {}, clear=False), \
                mock.patch.object(pricing, "GENERIC_MODEL_RATES",
                                  list(pricing.GENERIC_MODEL_RATES)):
            applied = pricing.parse_override_payload({
                "family": {"zzfamily": {"input": 1.0, "output": 2.0}},
                "models": {"zz-model": {"input": 3.0, "output": 6.0,
                                        "input_1m": 4.0}},
                "generic": [["zzgen", {"input": 0.5, "output": 1.5}]],
            })
            self.assertEqual(applied, 3)
            self.assertEqual(pricing.PRICING["zzfamily"]["input"], 1.0)
            self.assertEqual(
                pricing.cost_for("zz-model", {"input_tokens": 1_000_000}),
                3.0)
            # input_1m is honored for the [1m] tier
            self.assertEqual(pricing.rate_for_model("zz-model[1m]")["input"],
                             4.0)
            self.assertEqual(
                pricing.estimate_generic_cost("zzgen-mini", 1_000_000, 0),
                0.5)

            # malformed payloads are ignored, never fatal
            self.assertEqual(pricing.parse_override_payload("nope"), 0)
            self.assertEqual(pricing.parse_override_payload(
                {"models": {"bad": {"input": -1, "output": 2}}}), 0)
            self.assertEqual(pricing.parse_override_payload(
                {"models": {"bad": {"output": 2}}}), 0)
            self.assertEqual(pricing.parse_override_payload(
                {"generic": ["not-a-pair"]}), 0)

    def test_repeated_generic_overrides_do_not_stack(self):
        with mock.patch.object(pricing, "GENERIC_MODEL_RATES",
                               list(pricing.GENERIC_MODEL_RATES)):
            base = len(pricing.GENERIC_MODEL_RATES)
            payload = {"generic": [["zzgen-dup", {"input": 0.5,
                                                  "output": 1.5}]]}
            pricing.parse_override_payload(payload)
            pricing.parse_override_payload(payload)
            self.assertEqual(len(pricing.GENERIC_MODEL_RATES), base + 1)
            self.assertEqual(
                pricing.generic_rate_for("zzgen-dup")["input"], 0.5)


class ProjectLimitParseTest(unittest.TestCase):
    def test_parses_valid_entries_and_skips_bad_ones(self):
        raw = ["discord=50", "tracker:12.5", "  spaced = 3 ",
               "no-amount", "bad=abc", "zero=0", ""]
        with mock.patch("suprbar.config.get_pref", return_value=raw):
            got = config.project_limit_map()
        self.assertEqual(got, {"discord": 50.0, "tracker": 12.5,
                               "spaced": 3.0})

    def test_non_list_is_empty(self):
        with mock.patch("suprbar.config.get_pref", return_value="nope"):
            self.assertEqual(config.project_limit_map(), {})


class ScannerTailTest(unittest.TestCase):
    def setUp(self):
        scanner._file_cache.clear()
        scanner._cache_date = None

    def test_tail_appends_without_double_counting(self):
        now = datetime.now(UTC)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "session.jsonl"
            p.write_text(_usage_record(now) + "\n" + _usage_record(now) + "\n",
                         encoding="utf-8")

            res1, off1 = scanner._scan_one_file(p, midnight)
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["sess_msgs_today"], 2)
            self.assertEqual(off1, p.stat().st_size)

            with p.open("a", encoding="utf-8") as f:
                f.write(_usage_record(now, inp=500_000) + "\n")
                f.write(_usage_record(now, inp=500_000) + "\n")

            res2, off2 = scanner._scan_one_file(
                p, midnight, start_offset=off1, base=res1)
            full, _off_full = scanner._scan_one_file(p, midnight)

            self.assertEqual(res2["sess_msgs_today"], 4)
            self.assertEqual(res2["sess_msgs_today"],
                             full["sess_msgs_today"])
            self.assertEqual(res2["today_totals"]["input"],
                             full["today_totals"]["input"])
            self.assertAlmostEqual(res2["today_totals"]["cost"],
                                   full["today_totals"]["cost"])
            self.assertEqual(off2, p.stat().st_size)

    def test_rolling_window_counts_pre_midnight_usage(self):
        now = datetime.now(UTC)
        # Synthetic midnight 1h ago: the 2h-ago record is pre-"midnight" but
        # still inside the rolling window; the 26h-ago one is outside both.
        midnight = now - timedelta(hours=1)
        cutoff = int((now.timestamp() - 25 * 3600) // 60)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "session.jsonl"
            # 2h ago, 26h ago (outside the window), and now.
            p.write_text(
                _usage_record(now - timedelta(hours=2), inp=1_000_000) + "\n"
                + _usage_record(now - timedelta(hours=26), inp=5_000_000) + "\n"
                + _usage_record(now, inp=1_000_000) + "\n",
                encoding="utf-8")
            res, _off = scanner._scan_one_file(
                p, midnight, rolling_cutoff_min=cutoff)

        self.assertEqual(res["sess_msgs_today"], 1)   # today only
        self.assertEqual(len(res["rolling"]), 2)      # 2h-ago + now buckets
        cost = sum(v[0] for v in res["rolling"].values())
        self.assertAlmostEqual(cost, 6.0)             # 2 x $3/M sonnet
        msgs = sum(v[1] for v in res["rolling"].values())
        self.assertEqual(msgs, 2)

    def test_partial_trailing_line_is_held_back(self):
        now = datetime.now(UTC)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "session.jsonl"
            p.write_text(_usage_record(now) + "\n", encoding="utf-8")
            res1, _off1 = scanner._scan_one_file(p, midnight)

            half = _usage_record(now, inp=123)[:40]
            with p.open("a", encoding="utf-8") as f:
                f.write(half)  # no newline yet

            res2, off2 = scanner._scan_one_file(
                p, midnight, start_offset=p.stat().st_size - len(half),
                base=res1)
            self.assertEqual(res2["sess_msgs_today"], 1)   # held back
            self.assertEqual(off2, p.stat().st_size - len(half))

            with p.open("a", encoding="utf-8") as f:
                f.write(_usage_record(now, inp=123)[40:] + "\n")
            res3, _off3 = scanner._scan_one_file(
                p, midnight, start_offset=off2, base=res2)
            self.assertEqual(res3["sess_msgs_today"], 2)


class ProjectBudgetTest(unittest.TestCase):
    def setUp(self):
        scanner._file_cache.clear()
        scanner._cache_date = None

    def test_project_limits_get_their_own_window(self):
        now = datetime.now(UTC)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for proj, tokens in (("alpha", 1_000_000), ("beta", 2_000_000)):
                d = root / proj
                d.mkdir()
                (d / "s.jsonl").write_text(
                    _usage_record(now, session=proj, inp=tokens) + "\n",
                    encoding="utf-8")
            with mock.patch.object(scanner, "CLAUDE_HOME", root):
                s = scanner.budgets_summary(
                    0.0, 0.0, 0.0,
                    project_limits={"alpha": 1.0, "beta": 100.0},
                )
        # sonnet: $3/M input
        self.assertAlmostEqual(s["project:alpha"]["spent"], 3.0)
        self.assertTrue(s["project:alpha"]["over"])
        self.assertAlmostEqual(s["project:beta"]["spent"], 6.0)
        self.assertFalse(s["project:beta"]["over"])
        self.assertEqual(s["project:beta"]["project"], "beta")
        # global windows still exist even with no global limits
        self.assertEqual(s["daily"]["limit"], 0.0)


class HostedTableSyncTest(unittest.TestCase):
    def test_pricing_json_matches_builtins(self):
        root = Path(__file__).resolve().parent.parent
        path = root / "pricing.json"
        self.assertTrue(path.exists(),
                        "pricing.json missing — run python scripts/export_pricing.py")
        on_disk = json.loads(path.read_text("utf-8"))
        expected = {
            "family": {k: dict(v) for k, v in pricing.PRICING.items()},
            "models": {k: dict(v) for k, v in pricing.MODEL_RATES.items()},
            "generic": [[p, dict(r)]
                        for p, r in pricing.GENERIC_MODEL_RATES],
        }
        self.assertEqual(on_disk, expected,
                         "pricing.json is stale — run python scripts/export_pricing.py")


if __name__ == "__main__":
    unittest.main()
