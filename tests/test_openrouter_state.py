import unittest

from suprbar.providers.openrouter import advance_state


class OpenRouterStateTest(unittest.TestCase):
    def test_first_poll_establishes_baseline_at_zero_cost(self):
        state, cost = advance_state({}, "2026-08-31", 12.50)
        self.assertEqual(state, {"day": "2026-08-31", "usage_at_start": 12.50})
        self.assertEqual(cost, 0.0)

    def test_same_day_delta_accumulates(self):
        state = {"day": "2026-08-31", "usage_at_start": 10.0}
        state, cost = advance_state(state, "2026-08-31", 13.25)
        self.assertEqual(state["day"], "2026-08-31")
        self.assertEqual(state["usage_at_start"], 10.0)
        self.assertAlmostEqual(cost, 3.25)

    def test_day_rollover_resets_baseline(self):
        state = {"day": "2026-08-30", "usage_at_start": 10.0}
        state, cost = advance_state(state, "2026-08-31", 27.0)
        self.assertEqual(state, {"day": "2026-08-31", "usage_at_start": 27.0})
        self.assertEqual(cost, 0.0)  # overnight spend not retro-attributed

    def test_delta_never_negative_on_usage_reset(self):
        state = {"day": "2026-08-31", "usage_at_start": 10.0}
        _state, cost = advance_state(state, "2026-08-31", 5.0)
        self.assertEqual(cost, 0.0)

    def test_missing_baseline_defaults_to_zero(self):
        state = {"day": "2026-08-31"}
        _state, cost = advance_state(state, "2026-08-31", 4.0)
        self.assertAlmostEqual(cost, 4.0)


if __name__ == "__main__":
    unittest.main()
