import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agym import cli
from agym.profiles import ProfileStore


class CliTests(unittest.TestCase):
    def test_info_bypasses_updater(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"AGYM_DATA_HOME": tmp}), \
             patch("agym.cli.maybe_prompt_startup_update", side_effect=AssertionError("updater called")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["integration", "info", "--json"]), 0)
            response = json.loads(output.getvalue())
            self.assertEqual(response["protocol"]["major"], 1)
            self.assertIn("runs.durable", response["data"]["capabilities"])

    def test_invalid_protocol_is_json_error(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["integration", "profiles", "--protocol", "2", "--json"]), 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "UNSUPPORTED_PROTOCOL")

    def test_detached_run_replay_follow_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "agy"
            fake.write_text("#!/usr/bin/env python3\nimport json,sys,time\n"
                            "print(json.dumps({'session_id':'session-test'}),flush=True)\n"
                            "print('work started',flush=True)\n"
                            "time.sleep(10 if 'long' in sys.argv[-1] else .1)\n"
                            "print('work done',flush=True)\n")
            fake.chmod(0o755)
            env = dict(os.environ, AGYM_CONFIG_HOME=str(base / "config"),
                       AGYM_DATA_HOME=str(base / "data"),
                       PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
            with patch.dict(os.environ, env):
                profile = ProfileStore().create("ttb")
                (profile.home / ".gemini").mkdir()
                (profile.home / ".gemini" / "state").write_text("ready")

            def invoke(*args, request=None):
                result = subprocess.run([sys.executable, "-m", "agym", "integration", *args],
                    input=json.dumps(request) if request else None, text=True,
                    capture_output=True, env=env, timeout=8)
                return json.loads(result.stdout)

            def request(identifier, task):
                return {"request_id": identifier, "client": "bonsai", "client_id": "c",
                        "workspace": {"key": "ws", "cwd": tmp}, "profile": "ttb",
                        "task": task, "execution": "headless", "permission_policy": "profile-default"}

            started = invoke("run", "start", "--request-json", "-", "--protocol", "1", "--json",
                             request=request("one", "short"))
            self.assertTrue(started["ok"], started)
            run_id = started["data"]["run_id"]
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                snapshot = invoke("run", "get", "--id", run_id, "--protocol", "1", "--json")["data"]
                if snapshot["status"] == "succeeded":
                    break
                time.sleep(.05)
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["session_id"], "session-test")
            page = invoke("run", "events", "--id", run_id, "--after", "1", "--protocol", "1", "--json")["data"]
            self.assertTrue(all(event["seq"] > 1 for event in page["events"]))
            follow = subprocess.run([sys.executable, "-m", "agym", "integration", "run", "events",
                "--id", run_id, "--after", "0", "--follow", "--protocol", "1", "--ndjson"],
                capture_output=True, text=True, env=env, timeout=4)
            self.assertEqual(follow.returncode, 0)
            self.assertIn("run_finished", follow.stdout)
            self.assertEqual(invoke("lease", "get", "--id", snapshot["lease_id"], "--protocol", "1", "--json")["data"]["state"], "released")

            second = invoke("run", "start", "--request-json", "-", "--protocol", "1", "--json",
                            request=request("two", "long"))["data"]
            stopped = invoke("run", "stop", "--id", second["run_id"], "--protocol", "1", "--json")["data"]
            self.assertEqual(stopped["status"], "stopped")
