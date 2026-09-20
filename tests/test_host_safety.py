from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agym.profiles import ProfileStore


class HostSafetyTests(unittest.TestCase):
    def test_setup_remove_store_never_touch_host_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host_gemini = root / "host-home" / ".gemini"
            host_gemini.mkdir(parents=True)
            sentinel = host_gemini / "DO_NOT_TOUCH"
            sentinel.write_text("host-state", encoding="utf-8")

            store = ProfileStore(root / "config", root / "data")
            profile = store.create("personal")
            isolated = profile.home / ".gemini" / "antigravity-cli"
            isolated.mkdir(parents=True)
            (isolated / "state").write_text("profile-state", encoding="utf-8")
            store.remove("personal")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "host-state")
            self.assertTrue(host_gemini.is_dir())
