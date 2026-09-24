import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agym.integration.errors import IntegrationError
from agym.integration import runs
from agym.integration.store import Store
from agym.profiles import Profile, ProfileSettings


class RunTests(unittest.TestCase):
    def test_idempotency_and_workspace_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "integration")
            request = {"request_id": "one", "client": "bonsai", "client_id": "client",
                       "workspace": {"key": "ws", "cwd": tmp}, "profile": "ttb",
                       "task": "hello", "execution": "headless", "permission_policy": "profile-default"}
            profile = Profile("ttb", Path(tmp), "now", ProfileSettings())
            with patch("agym.integration.runs.profiles.select", return_value=profile), \
                 patch("agym.integration.runs._launch") as launch:
                launch.return_value.pid = 12345
                first = runs.start(request, store)
                again = runs.start(request, store)
                self.assertEqual(first["run_id"], again["run_id"])
                self.assertEqual(launch.call_count, 1)
                with self.assertRaises(IntegrationError) as conflict:
                    runs.start(dict(request, task="different"), store)
                self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
                with self.assertRaises(IntegrationError) as busy:
                    runs.start(dict(request, request_id="two"), store)
                self.assertEqual(busy.exception.code, "WORKSPACE_BUSY")
