"""Headless UI smoke test: the real static pages over the real HTTP server.

Serves the app with a deterministic /api/today fixture (no scans, no network)
and asserts the flyout hero + the mini overlay render without JS errors.

Skipped automatically when Playwright (or its browsers) isn't installed; CI
installs it — see .github/workflows/ci.yml.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from playwright.sync_api import sync_playwright

from suprbar import server


def _fixture() -> dict:
    return {
        "now": "2026-09-21T15:42:10-05:00",
        "today_date": "2026-09-21",
        "scan_ms": 120,
        "files_scanned": 12,
        "files_reused": 10,
        "files_reparsed": 2,
        "files_tailed": 1,
        "parse_errors": 0,
        "today": {
            "cost": 18.74, "messages": 132,
            "input": 84120, "output": 191442,
            "cache_5m": 1200000, "cache_1h": 0, "cache_read": 9650000,
            "cache_hit_ratio": 0.41, "cache_savings_usd": 11.82,
            "tokens": 11030000,
        },
        "active": {
            "id": "s1", "project": "supr.bar", "path": "C:/fake/s1.jsonl",
            "started_at": "2026-09-21T14:05:00-05:00",
            "last_activity": "2026-09-21T15:41:58-05:00",
            "live": True, "model": "claude-opus-4-7[1m]",
            "cost_today": 9.31, "messages_today": 61,
            "burn_rate_usd_per_hour": 5.74,
            "today": {"input": 1000, "output": 2000, "cache_5m": 0,
                      "cache_1h": 0, "cache_read": 5000, "cost": 9.31,
                      "messages": 61, "tokens": 8000},
        },
        "live_sessions": [
            {
                "id": "s1", "project": "supr.bar", "path": "C:/fake/s1.jsonl",
                "started_at": "2026-09-21T14:05:00-05:00",
                "last_activity": "2026-09-21T15:41:58-05:00",
                "live": True, "model": "claude-opus-4-7[1m]",
                "cost_today": 9.31, "messages_today": 61,
                "burn_rate_usd_per_hour": 5.74,
            },
            {
                "id": "s2", "project": "tracker", "path": "C:/fake/s2.jsonl",
                "started_at": "2026-09-21T15:00:00-05:00",
                "last_activity": "2026-09-21T15:39:12-05:00",
                "live": True, "model": "claude-sonnet-4-6",
                "cost_today": 3.02, "messages_today": 40,
                "burn_rate_usd_per_hour": 1.10,
            },
        ],
        "last_session_seen": None,
        "sources": [{
            "id": "local", "label": "Claude Code · local", "ok": True,
            "cost_today": 18.74, "messages_today": 132, "live": True,
        }],
        "by_model": [
            {"model": "claude-opus-4-7[1m]", "cost": 14.10, "messages": 78,
             "tokens": 7100000, "cache_read": 6200000},
            {"model": "claude-sonnet-4-6", "cost": 4.64, "messages": 54,
             "tokens": 3300000, "cache_read": 3450000},
        ],
        "by_project": [
            {"project": "supr.bar", "cost": 9.31, "messages": 61,
             "tokens": 5200000, "models": ["claude-opus-4-7[1m]"]},
            {"project": "tracker", "cost": 6.40, "messages": 48,
             "tokens": 3100000, "models": ["claude-sonnet-4-6"]},
            {"project": "discord-bot", "cost": 3.03, "messages": 23,
             "tokens": 1100000, "models": ["claude-haiku-4-5"]},
        ],
        "hourly": [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
                   for h in range(24)],
        "insights": {
            "live_count": 2, "projected_today_cost": 41.20,
            "cost_per_message": 0.142, "cache_savings_usd": 11.82,
            "top_project_share": 0.50, "sessions_today": 4,
            "projects_today": 3, "parse_errors": 0,
        },
        "scan_source": "C:/Users/test/.claude/projects",
        "cache_meta": {"files_reused": 10, "files_reparsed": 2,
                       "files_tailed": 1, "last_scan_ms": 120,
                       "parse_errors": 0},
        "rolling_24h": {"cost": 1650.14, "messages": 3342,
                        "tokens": 12000000,
                        "since": "2026-09-20T15:42:00-05:00"},
        "sessions_today": 4,
        "projects_today": 3,
        "top_model_today": "claude-opus-4-7[1m]",
    }


def _range_fixture() -> dict:
    return {
        "range": {"key": "24h", "label": "last 24h",
                  "start": "2026-09-20T15:42:00-05:00",
                  "end": "2026-09-21T15:42:00-05:00", "days": 1},
        "totals": {"cost": 1650.14, "messages": 3342, "tokens": 12000000,
                   "input": 1000000, "output": 2000000, "cache_5m": 0,
                   "cache_1h": 0, "cache_read": 9000000,
                   "cache_hit_ratio": 0.9, "sessions": 5, "projects": 3},
        "by_day": [], "by_model": [], "by_project": [],
        "hourly": [{"hour": h, "cost": 0.0, "tokens": 0, "messages": 0}
                   for h in range(24)],
        "files_scanned": 12, "parse_errors": 0, "scan_ms": 60,
    }


def _budgets_fixture() -> dict:
    return {
        "daily": {"spent": 18.74, "limit": 25.0, "pct": 74.96, "over": False,
                  "remaining": 6.26, "alerting": False},
        "weekly": {"spent": 214.06, "limit": 0.0, "pct": 0.0, "over": False,
                   "remaining": 0.0, "alerting": False},
        "monthly": {"spent": 612.30, "limit": 750.0, "pct": 81.64,
                    "over": False, "remaining": 137.70, "alerting": True},
        "alert_pct": 80,
    }


def _prefs_fixture() -> dict:
    return {"prefs": {
        "schema_version": 4,
        "range": {"default": "today", "week_starts_on": "mon"},
        "display": {"theme": "dark", "accent": "blue", "font_scale": 1.0,
                    "cost_format": "with_cents", "animations": True},
        "budgets": {"daily_limit": 25.0, "weekly_limit": 0.0,
                    "monthly_limit": 750.0, "alert_at_pct": 80,
                    "notify": True, "tray_warn_color": True,
                    "project_limits": ["discord=50"]},
        "behavior": {"refresh_seconds": 5, "auto_hide": True,
                     "always_on_top": True, "live_threshold_seconds": 60,
                     "confirm_quit": False, "click_through": False},
        "mini": {"enabled": False, "show_burn": True, "click_through": False},
        "pricing": {"remote_url": ""},
        "projects": {"allowlist": [], "denylist": [], "anonymize": False,
                     "top_n": 10},
        "data": {"log_level": "INFO"},
        "ui": {"pinned": False, "start_on_login": False},
        "updates": {"check_on_launch": True, "last_check": "",
                    "skip_version": ""},
        "sources": {
            "local": {"enabled": True},
            "anthropic_api": {"enabled": False},
            "hermes": {"enabled": True},
            "opencode": {"enabled": True},
            "openrouter": {"enabled": False},
            "openai": {"enabled": False},
        },
    }, "schema_version": 4}


class UiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(server, "today_cached", side_effect=_fixture),
            mock.patch.object(server, "range_cached",
                              side_effect=lambda *a, **k: _range_fixture()),
            mock.patch.object(server, "_budgets_payload",
                              side_effect=_budgets_fixture),
            mock.patch.object(server, "_prefs_payload",
                              side_effect=_prefs_fixture),
        ]
        for p in cls._patches:
            p.start()
        cls.httpd, cls.port, cls.thread = server.start_in_background(0)

    @classmethod
    def tearDownClass(cls):
        for p in cls._patches:
            p.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _open(self, browser, path: str, width: int, height: int):
        page = browser.new_page(viewport={"width": width, "height": height})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"http://127.0.0.1:{self.port}{path}")
        return page, errors

    def _wait_text(self, page, selector: str, needle: str,
                   timeout: float = 10.0) -> str:
        """Poll for `needle` from Python.

        In-page wait_for_function() is eval'd and the app's CSP (intentionally)
        forbids unsafe-eval, so polling has to happen outside the page.
        """
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                last = page.text_content(selector) or ""
            except Exception:
                last = ""
            if needle in last:
                return last
            time.sleep(0.1)
        raise AssertionError(
            f"{selector} never contained {needle!r} (last: {last[:160]!r})")

    @staticmethod
    def _launch(pw):
        """Launch Chromium, or skip when the browser binary isn't installed."""
        try:
            return pw.chromium.launch()
        except Exception as e:  # playwright._impl._errors.Error
            pytest.skip(f"chromium not installed for playwright: {e}")

    def test_flyout_renders_with_fixture_data(self):
        with sync_playwright() as pw:
            browser = self._launch(pw)
            try:
                page, errors = self._open(browser, "/", 360, 480)
                self._wait_text(page, "#costWhole", "18")
                self.assertIn(".74", page.text_content("#costCents"))
                self.assertIn("Today", page.text_content("#costLabel"))
                self.assertEqual(page.locator("#rangeTabs .rt").count(), 7)
                self.assertIn("2", page.text_content("#liveCount"))
                # Frameless resize grip is present + clickable.
                self.assertTrue(page.is_visible("#resizeGrip"))
                # The schema-driven settings render the new sections too.
                page.click("#settingsBtn")
                self._wait_text(page, "#settingsSections", "Show mini overlay")
                self.assertIn(
                    "Per-project daily caps",
                    page.text_content("#settingsSections"))
                # ...and no longer render the settings trimmed in schema v4.
                settings_text = page.text_content("#settingsSections")
                self.assertNotIn("Density", settings_text)
                self.assertNotIn("Width (px)", settings_text)
                self.assertNotIn("Auto-hide delay", settings_text)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_mini_overlay_renders_with_fixture_data(self):
        with sync_playwright() as pw:
            browser = self._launch(pw)
            try:
                page, errors = self._open(browser, "/mini.html", 176, 44)
                # Defaults to the rolling 24h range (server-cached /api/range).
                self._wait_text(page, "#cCost", "1,650")
                self.assertEqual(page.text_content("#cRange").strip(), "24h")
                self.assertIn("1,650", page.text_content("#fCost"))
                self.assertIn("live", page.get_attribute("body", "class") or "")
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
