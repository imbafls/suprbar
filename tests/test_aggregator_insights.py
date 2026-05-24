import unittest

from suprbar.aggregator import _build_insights


class AggregatorInsightsTest(unittest.TestCase):
    def test_builds_operator_friendly_today_insights(self):
        now_iso = "2026-05-24T10:00:00-05:00"
        today = {"cost": 12.0, "messages": 6, "cache_savings_usd": 3.5}
        active = {"burn_rate_usd_per_hour": 2.0}
        live_sessions = [{"id": "a"}, {"id": "b"}]
        by_project = [
            {"project": "alpha", "cost": 9.0},
            {"project": "beta", "cost": 3.0},
        ]

        insights = _build_insights(
            now_iso=now_iso,
            today=today,
            active=active,
            live_sessions=live_sessions,
            by_project=by_project,
            parse_errors=1,
        )

        self.assertEqual(insights["live_count"], 2)
        self.assertEqual(insights["cost_per_message"], 2.0)
        self.assertEqual(insights["cache_savings_usd"], 3.5)
        self.assertEqual(insights["top_project_share"], 0.75)
        self.assertEqual(insights["parse_errors"], 1)
        self.assertAlmostEqual(insights["projected_today_cost"], 40.0)


if __name__ == "__main__":
    unittest.main()
