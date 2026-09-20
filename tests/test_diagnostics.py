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
