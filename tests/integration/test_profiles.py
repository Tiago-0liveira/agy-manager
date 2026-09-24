import tempfile
import unittest
from pathlib import Path

from agym.integration.profiles import list_profiles
from agym.integration.store import Store
from agym.profiles import ProfileStore


class ProfileAdapterTests(unittest.TestCase):
    def test_readiness_has_no_home_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profiles = ProfileStore(base / "config", base / "data")
            profiles.create("ttb")
            result = list_profiles(Store(base / "data" / "integration"), profiles)
            self.assertEqual(result[0]["readiness"], "auth_required")
            self.assertEqual(set(result[0]), {"profile_id", "name", "readiness", "reason"})
