from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.diagnostics import doctor_lines
from agym.profiles import ProfileStore


class DiagnosticsTests(unittest.TestCase):
    def test_doctor_never_emits_credential_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            profile = store.create("personal")
            auth = profile.home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            auth.parent.mkdir(parents=True)
            secret = "SUPER_SECRET_REFRESH_TOKEN"
            auth.write_text('{"refresh_token":"%s"}' % secret, encoding="utf-8")
            with mock.patch("agym.diagnostics.resolve_agy", side_effect=Exception("skip")):
                # Patch a non-domain error carefully by bypassing the agy block through a real
                # AgyNotFound in the second invocation below.
                pass
            from agym.launcher import AgyNotFound
            with mock.patch("agym.diagnostics.resolve_agy", side_effect=AgyNotFound("missing")):
                output = "\n".join(doctor_lines(store))
            self.assertIn("credential state file: present", output)
            self.assertNotIn(secret, output)
            self.assertNotIn("refresh_token", output)

    def test_doctor_reports_settings_and_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            store.create("personal")
            from agym.launcher import AgyNotFound
            with mock.patch("agym.diagnostics.resolve_agy", side_effect=AgyNotFound("missing")):
                output = "\n".join(doctor_lines(store))
            self.assertIn("model: default", output)
            self.assertIn("dangerously_skip_permissions: false", output)
            self.assertNotIn("warning: --dangerously-skip-permissions is enabled", output)

    def test_doctor_warns_on_dangerous_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            store.create("personal")
            from agym.profiles import ProfileSettings
            store.update_settings("personal", ProfileSettings(dangerously_skip_permissions=True))
            from agym.launcher import AgyNotFound
            with mock.patch("agym.diagnostics.resolve_agy", side_effect=AgyNotFound("missing")):
                output = "\n".join(doctor_lines(store, "personal"))
            self.assertIn("dangerously_skip_permissions: true", output)
            self.assertIn("warning: --dangerously-skip-permissions is enabled for this profile", output)

    def test_doctor_reports_malformed_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import json
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            data = {
                "version": 1,
                "profiles": {
                    "broken": {
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "home": str((root / "data" / "profiles" / "broken" / "home").resolve()),
                        "settings": {
                            "model": 999,
                            "dangerously_skip_permissions": "not-a-bool",
                        },
                    }
                },
            }
            store.config_path.parent.mkdir(parents=True, exist_ok=True)
            store.config_path.write_text(json.dumps(data), encoding="utf-8")
            from agym.launcher import AgyNotFound
            with mock.patch("agym.diagnostics.resolve_agy", side_effect=AgyNotFound("missing")):
                output = "\n".join(doctor_lines(store, "broken"))
            self.assertIn("invalid settings: model must be null or a string", output)
            self.assertIn("invalid settings: dangerously_skip_permissions must be a boolean", output)
