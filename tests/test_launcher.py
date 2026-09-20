from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.launcher import build_profile_env, resolve_agy, run_agy
from agym.profiles import Profile


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home_a = self.root / "a" / "home"
        self.home_b = self.root / "b" / "home"
        self.home_a.mkdir(parents=True)
        self.home_b.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_environment_construction_unix(self) -> None:
        env = build_profile_env(
            self.home_a,
            {"HOME": "/host/home", "PATH": "/bin", "SSH_AUTH_SOCK": "/tmp/agent.sock"},
            system="Linux",
        )
        self.assertEqual(env["HOME"], str(self.home_a.resolve()))
        self.assertEqual(env["GEMINI_FORCE_FILE_STORAGE"], "true")
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["SSH_AUTH_SOCK"], "/tmp/agent.sock")

    def test_environment_construction_windows(self) -> None:
        env = build_profile_env(
            self.home_a,
            {"USERPROFILE": r"C:\\Users\\host", "PATH": "X"},
            system="Windows",
        )
        self.assertEqual(env["USERPROFILE"], str(self.home_a.resolve()))
        self.assertEqual(env["HOME"], str(self.home_a.resolve()))
        self.assertEqual(env["GEMINI_FORCE_FILE_STORAGE"], "true")

    def test_concurrent_profiles_get_different_auth_data_paths(self) -> None:
        a = build_profile_env(self.home_a, {"HOME": "/host", "PATH": "/bin"}, system="Linux")
        b = build_profile_env(self.home_b, {"HOME": "/host", "PATH": "/bin"}, system="Linux")
        self.assertNotEqual(a["HOME"], b["HOME"])
        self.assertNotEqual(
            Path(a["HOME"]) / ".gemini" / "antigravity-cli",
            Path(b["HOME"]) / ".gemini" / "antigravity-cli",
        )

    def test_resolve_agy_uses_host_path(self) -> None:
        fake = self.root / "bin" / "agy"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        resolved = resolve_agy({"PATH": str(fake.parent), "HOME": "/host"})
        self.assertEqual(resolved, fake.resolve())

    @mock.patch("agym.launcher.subprocess.run")
    def test_cwd_preserved_and_args_passthrough(self, sp_run: mock.Mock) -> None:
        sp_run.return_value.returncode = 17
        profile = Profile("personal", self.home_a, "now")
        result = run_agy(Path("/usr/bin/agy"), profile, ["--", "-p", "review this"], replace_process=False)
        self.assertEqual(result, 17)
        args, kwargs = sp_run.call_args
        self.assertEqual(args[0], ["/usr/bin/agy", "--dangerously-skip-permissions", "-p", "review this"])
        self.assertIsNone(kwargs["cwd"])
        self.assertNotIn("stdin", kwargs)
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)

    @mock.patch("agym.launcher.subprocess.run")
    def test_direct_profile_args_passthrough(self, sp_run: mock.Mock) -> None:
        sp_run.return_value.returncode = 0
        profile = Profile("personal", self.home_a, "now")
        run_agy(Path("/usr/bin/agy"), profile, ["-p", "review this"], replace_process=False)
        self.assertEqual(sp_run.call_args.args[0], ["/usr/bin/agy", "--dangerously-skip-permissions", "-p", "review this"])
