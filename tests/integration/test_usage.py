import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agym.integration.usage import list_usage
from agym.profiles import ProfileStore
from agym.usage import AccountUsage, UsageBucket, UsageGroup


class UsageAdapterTests(unittest.TestCase):
    def test_quota_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            profiles.create("ttb")
            quota = AccountUsage("ttb", "success", [UsageGroup("Gemini", None,
                [UsageBucket("gemini_5h", "5h", "5h", .72, None)])])
            with patch("agym.integration.usage.fetch_and_cache_usage", return_value=[quota]) as fetch:
                result = list_usage("ttb", True, profiles)
            self.assertEqual(result[0]["windows"][0]["remaining"], .72)
            self.assertTrue(fetch.call_args.kwargs["force"])
