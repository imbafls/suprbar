"""Config schema migration tests (→ v5, the v2.0 trim to five settings)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from suprbar import config

# A realistic v4 file: every source with key blobs, plus the settings v2 drops.
V4 = {
    "schema_version": 4,
    "sources": {
        "local": {"enabled": True},
        "anthropic_api": {"enabled": True, "admin_key_enc": "QUJDREVG"},
        "hermes": {"enabled": False},
        "opencode": {"enabled": True},
        "openrouter": {"enabled": True, "key_enc": "T1BFTlJPVVRFUg=="},
        "openai": {"enabled": False, "key_enc": "T1BFTkFJ"},
    },
    "ui": {"pinned": False, "start_on_login": True},
    "range": {"default": "7d", "week_starts_on": "sun"},
    "display": {"theme": "light", "accent": "green", "density": "compact",
                "font_scale": 1.1, "cost_format": "whole", "animations": False},
    "budgets": {"daily_limit": 25.0, "project_limits": ["discord=50"]},
    "updates": {"check_on_launch": False, "last_check": "2026-09-27T08:09:59",
                "skip_version": "0.15.0"},
    "behavior": {"refresh_seconds": 10, "auto_hide": True,
                 "always_on_top": False, "live_threshold_seconds": 120,
                 "confirm_quit": True, "click_through": True},
    "mini": {"enabled": True, "show_burn": False, "click_through": True},
    "pricing": {"remote_url": "https://example.com/p.json"},
    "projects": {"allowlist": ["a"], "denylist": ["b"], "anonymize": True,
                 "top_n": 3},
    "data": {"log_level": "DEBUG"},
}


def _load(raw: dict) -> dict:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        with mock.patch.object(config, "config_path", return_value=p):
            config._cache = None
            return config.load(force=True)


class SchemaV5MigrationTest(unittest.TestCase):
    def tearDown(self):
        # Never leak a temp config into other tests.
        config._cache = None

    def test_v4_keeps_sources_keys_and_the_five_settings(self):
        cfg = _load(json.loads(json.dumps(V4)))
        self.assertEqual(cfg["schema_version"], 5)
        self.assertEqual(cfg["sources"], V4["sources"])  # keys byte-identical
        self.assertIs(cfg["ui"]["start_on_login"], True)
        self.assertIs(cfg["mini"]["enabled"], True)
        self.assertEqual(cfg["updates"], V4["updates"])

    def test_v4_drops_every_removed_setting(self):
        cfg = _load(json.loads(json.dumps(V4)))
        self.assertEqual(set(cfg), {"schema_version", "sources", "ui", "mini",
                                    "updates"})
        self.assertEqual(set(cfg["ui"]), {"pinned", "start_on_login"})
        self.assertEqual(set(cfg["mini"]), {"enabled"})

    def test_auto_hide_off_becomes_pinned(self):
        raw = json.loads(json.dumps(V4))
        raw["behavior"]["auto_hide"] = False
        self.assertIs(_load(raw)["ui"]["pinned"], True)
        raw["behavior"]["auto_hide"] = True
        self.assertIs(_load(raw)["ui"]["pinned"], False)
        raw["ui"]["pinned"] = True
        self.assertIs(_load(raw)["ui"]["pinned"], True)

    def test_v3_and_unversioned_configs_migrate_too(self):
        cfg = _load({"schema_version": 3, "mini": {"enabled": True},
                     "window": {"width": 500}})
        self.assertEqual(cfg["schema_version"], 5)
        self.assertIs(cfg["mini"]["enabled"], True)
        self.assertNotIn("window", cfg)
        cfg = _load({"ui": {"pinned": True}})
        self.assertEqual(cfg["schema_version"], 5)
        self.assertIs(cfg["ui"]["pinned"], True)
        self.assertIs(cfg["sources"]["local"]["enabled"], True)

    def test_v5_round_trips_and_tolerates_unknown_keys(self):
        v5 = {"schema_version": 5,
              "sources": {"local": {"enabled": False}},
              "ui": {"pinned": True, "start_on_login": False},
              "mini": {"enabled": False},
              "updates": {"check_on_launch": True, "last_check": "",
                          "skip_version": ""},
              "from_the_future": {"x": 1}}
        cfg = _load(v5)
        self.assertIs(cfg["sources"]["local"]["enabled"], False)
        self.assertIs(cfg["sources"]["opencode"]["enabled"], True)  # default
        self.assertIs(cfg["ui"]["pinned"], True)

    def test_schema_lists_only_the_v5_settings(self):
        self.assertEqual(set(config.SCHEMA), {
            "sources.local.enabled", "sources.anthropic_api.enabled",
            "sources.hermes.enabled", "sources.opencode.enabled",
            "sources.openrouter.enabled", "sources.openai.enabled",
            "mini.enabled", "ui.pinned", "ui.start_on_login",
            "updates.check_on_launch", "updates.last_check",
            "updates.skip_version",
        })
        with self.assertRaises(ValueError):
            config._coerce("display.theme", "dark")


if __name__ == "__main__":
    unittest.main()
