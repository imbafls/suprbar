"""/api/settings — the only settings surface in v2 — against a real server."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from suprbar import config, server


def _fake_protect(b: bytes) -> bytes:
    return b"enc:" + base64.b64encode(b)


def _fake_unprotect(b: bytes) -> bytes | None:
    return base64.b64decode(b[4:]) if b.startswith(b"enc:") else None


class SettingsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cfg_path = Path(cls._td.name) / "config.json"
        cls.startup_calls: list[bool] = []
        cls.changed: list[dict] = []
        cls._patches = [
            mock.patch.object(config, "config_path", return_value=cfg_path),
            # DPAPI is Windows-only; a reversible stand-in keeps CI portable.
            mock.patch.object(config, "_dpapi_protect", side_effect=_fake_protect),
            mock.patch.object(config, "_dpapi_unprotect",
                              side_effect=_fake_unprotect),
            mock.patch.object(config, "startup_registered",
                              side_effect=lambda default=False: default),
            mock.patch.object(config, "apply_startup_setting",
                              side_effect=lambda v, *_a: cls.startup_calls.append(v)
                              or True),
        ]
        for p in cls._patches:
            p.start()
        config._cache = None
        server.set_settings_callback(cls.changed.append)
        cls.httpd, cls.port, _ = server.start_in_background(47890)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.set_settings_callback(None)
        for p in cls._patches:
            p.stop()
        config._cache = None
        cls._td.cleanup()

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_get_shape_and_no_plaintext_keys(self):
        code, body = self._req("GET", "/api/settings")
        self.assertEqual(code, 200)
        self.assertEqual(set(body["settings"]), {
            "sources.local.enabled", "sources.anthropic_api.enabled",
            "sources.hermes.enabled", "sources.opencode.enabled",
            "sources.openrouter.enabled", "sources.openai.enabled",
            "mini.enabled", "ui.pinned", "ui.start_on_login",
            "updates.check_on_launch"})
        self.assertEqual(set(body["keys"]),
                         {"anthropic_api", "openrouter", "openai"})
        self.assertIn("version", body)

    def test_each_setting_applies_and_persists(self):
        for path in ("sources.hermes.enabled", "mini.enabled", "ui.pinned",
                     "ui.start_on_login", "updates.check_on_launch"):
            before = self._req("GET", "/api/settings")[1]["settings"][path]
            code, body = self._req("POST", "/api/settings",
                                   {"settings": {path: not before}})
            self.assertEqual(code, 200, body)
            self.assertIs(body["settings"][path], not before)
            self.assertIs(config.get_pref(path), not before)
        self.assertTrue(self.startup_calls)       # login applied immediately
        self.assertTrue(any("mini.enabled" in c for c in self.changed))

    def test_unknown_internal_and_badly_typed_paths_are_rejected(self):
        for settings in ({"display.theme": "dark"},
                         {"updates.skip_version": "9.9.9"},
                         {"ui.pinned": "yes"}):
            code, _ = self._req("POST", "/api/settings",
                                {"settings": settings})
            self.assertEqual(code, 400, settings)

    def test_key_set_and_clear(self):
        code, body = self._req("POST", "/api/settings",
                               {"keys": {"openrouter": "sk-or-v1-abcdef123456"}})
        self.assertEqual(code, 200, body)
        self.assertNotIn("abcdef123456", json.dumps(body))  # fingerprint only
        self.assertTrue(body["keys"]["openrouter"])
        self.assertEqual(config.get_source_key("openrouter"),
                         "sk-or-v1-abcdef123456")
        code, body = self._req("POST", "/api/settings",
                               {"keys": {"openrouter": ""}})
        self.assertEqual(body["keys"]["openrouter"], "")
        code, _ = self._req("POST", "/api/settings", {"keys": {"nope": "x"}})
        self.assertEqual(code, 400)

    def test_test_key_validates_source(self):
        code, _ = self._req("POST", "/api/settings/test-key",
                            {"source": "nope", "key": "x"})
        self.assertEqual(code, 400)
        with mock.patch.object(server.p_openrouter, "test_connection",
                               return_value=(False, "invalid key")):
            code, body = self._req("POST", "/api/settings/test-key",
                                   {"source": "openrouter", "key": "bad"})
        self.assertEqual((code, body["ok"]), (200, False))

    def test_removed_routes_are_gone(self):
        for method, path in (("GET", "/api/config"), ("GET", "/api/prefs"),
                             ("GET", "/api/prefs/schema"),
                             ("GET", "/api/config/export"),
                             ("GET", "/api/health"), ("GET", "/api/diagnostics"),
                             ("GET", "/api/budgets"),
                             ("POST", "/api/config"), ("POST", "/api/prefs"),
                             ("POST", "/api/config/import"),
                             ("POST", "/api/config/reset"),
                             ("POST", "/api/config/test-key"),
                             ("POST", "/api/open-path")):
            code, _ = self._req(method, path, {} if method == "POST" else None)
            self.assertEqual(code, 404, f"{method} {path}")


if __name__ == "__main__":
    unittest.main()
