"""Config schema migration tests (v3 → v4 settings trim)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from suprbar import config


class SchemaV4MigrationTest(unittest.TestCase):
    def tearDown(self):
        # Never leak a temp config into other tests.
        config._cache = None

    def test_v3_config_prunes_removed_settings_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({
                "schema_version": 3,
                "display": {
                    "theme": "light", "density": "compact",
                    "token_format": "full", "show_burn_rate": False,
                    "font_scale": 1.1,
                },
                "range": {
                    "default": "7d", "day_boundary": "utc",
                    "include_weekends": False,
                },
                "behavior": {"auto_hide_delay_ms": 500, "refresh_seconds": 10},
                "window": {"width": 500, "height": 700, "opacity": 0.9},
                "budgets": {"daily_limit": 5.0},
            }), encoding="utf-8")
            with mock.patch.object(config, "config_path", return_value=p):
                config._cache = None
                cfg = config.load(force=True)

        self.assertEqual(cfg["schema_version"], 4)
        # kept values survive the migration
        self.assertEqual(cfg["display"]["theme"], "light")
        self.assertEqual(cfg["display"]["font_scale"], 1.1)
        self.assertEqual(cfg["range"]["default"], "7d")
        self.assertEqual(cfg["behavior"]["refresh_seconds"], 10)
        self.assertEqual(cfg["budgets"]["daily_limit"], 5.0)
        # removed keys are pruned
        for key in ("density", "token_format", "show_burn_rate",
                    "show_token_bar", "show_cache_info", "show_model",
                    "show_project", "show_sessions_today"):
            self.assertNotIn(key, cfg["display"])
        self.assertNotIn("day_boundary", cfg["range"])
        self.assertNotIn("rolling_24h", cfg["range"])
        self.assertNotIn("include_weekends", cfg["range"])
        self.assertNotIn("auto_hide_delay_ms", cfg["behavior"])
        self.assertNotIn("window", cfg)
        # and the schema no longer advertises them
        for path in ("display.density", "display.token_format",
                     "range.day_boundary", "window.width",
                     "behavior.auto_hide_delay_ms"):
            self.assertNotIn(path, config.SCHEMA)


if __name__ == "__main__":
    unittest.main()
