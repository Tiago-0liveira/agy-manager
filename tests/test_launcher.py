from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.launcher import (
    Spinner,
    build_agy_args,
    build_profile_env,
    build_stage1_prompt,
    build_stage2_prompt,
    exec_agy_interactive,
    resolve_agy,
    run_agy,
    run_agy_capture,
    run_auto_prompt,
)
from agym.profiles import Profile, ProfileSettings


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
        # Safe default: no --dangerously-skip-permissions
        self.assertEqual(args[0], ["/usr/bin/agy", "-p", "review this"])
        self.assertIsNone(kwargs["cwd"])
        self.assertNotIn("stdin", kwargs)
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)

    @mock.patch("agym.launcher.subprocess.run")
    def test_direct_profile_args_passthrough(self, sp_run: mock.Mock) -> None:
        sp_run.return_value.returncode = 0
        profile = Profile("personal", self.home_a, "now")
        run_agy(Path("/usr/bin/agy"), profile, ["-p", "review this"], replace_process=False)
        self.assertEqual(sp_run.call_args.args[0], ["/usr/bin/agy", "-p", "review this"])

    def test_build_agy_args_safe_defaults(self) -> None:
        profile = Profile("personal", self.home_a, "now")
        args = build_agy_args(profile, passthrough_args=["-p", "hello"])
        self.assertEqual(args, ["-p", "hello"])
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--model", args)

    def test_build_agy_args_with_model(self) -> None:
        profile = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(model="gemini-2.5-flash"),
        )
        args = build_agy_args(profile, passthrough_args=["-p", "hello"])
        self.assertEqual(args, ["--model", "gemini-2.5-flash", "-p", "hello"])

    def test_build_agy_args_model_precedence(self) -> None:
        profile = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(model="profile-default-model"),
        )
        # Explicit invocation --model overrides profile default
        args = build_agy_args(profile, passthrough_args=["--model", "explicit-model", "-p", "hello"])
        self.assertEqual(args, ["--model", "explicit-model", "-p", "hello"])
        self.assertEqual(args.count("--model"), 1)

        # Explicit invocation --model=... overrides profile default
        args2 = build_agy_args(profile, passthrough_args=["--model=explicit-model", "-p", "hello"])
        self.assertEqual(args2, ["--model=explicit-model", "-p", "hello"])
        self.assertNotIn("--model", args2)

    def test_build_agy_args_dangerous_permissions(self) -> None:
        profile = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(dangerously_skip_permissions=True),
        )
        args = build_agy_args(profile, passthrough_args=["-p", "hello"])
        self.assertEqual(args, ["--dangerously-skip-permissions", "-p", "hello"])

        # Do not duplicate if already passed in passthrough
        args_dup = build_agy_args(
            profile, passthrough_args=["--dangerously-skip-permissions", "-p", "hello"]
        )
        self.assertEqual(args_dup, ["--dangerously-skip-permissions", "-p", "hello"])
        self.assertEqual(args_dup.count("--dangerously-skip-permissions"), 1)

    def test_build_agy_args_permission_aliases(self) -> None:
        profile = Profile("personal", self.home_a, "now")
        for alias in ["-y", "--yes", "--dsp", "--skip-perms", "--dangerously-skip-permission"]:
            args = build_agy_args(profile, passthrough_args=[alias, "-p", "hello"])
            self.assertEqual(
                args,
                ["--dangerously-skip-permissions", "-p", "hello"],
                f"Failed for alias {alias}",
            )
            self.assertNotIn(alias, args)

    def test_build_agy_args_negative_permission_aliases(self) -> None:
        profile = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(dangerously_skip_permissions=True),
        )
        for neg_alias in [
            "--no-dangerously-skip-permissions",
            "--no-dangerously-skip-permission",
            "--no-dsp",
            "--no-skip-perms",
        ]:
            args = build_agy_args(profile, passthrough_args=[neg_alias, "-p", "hello"])
            self.assertEqual(args, ["-p", "hello"], f"Failed for {neg_alias}")
            self.assertNotIn("--dangerously-skip-permissions", args)
            self.assertNotIn(neg_alias, args)

    def test_build_agy_args_environment_variables(self) -> None:
        profile = Profile("personal", self.home_a, "now")
        for var in ["DANGEROUSLY_SKIP_PERMISSIONS", "DSP"]:
            for truthy in ["1", "true", "True", "yes", "YES", "y", "on"]:
                args = build_agy_args(profile, passthrough_args=["-p", "hello"], env={var: truthy})
                self.assertEqual(
                    args,
                    ["--dangerously-skip-permissions", "-p", "hello"],
                    f"Failed for {var}={truthy}",
                )

        profile_enabled = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(dangerously_skip_permissions=True),
        )
        for var in ["DANGEROUSLY_SKIP_PERMISSIONS", "DSP"]:
            for falsy in ["0", "false", "no", "off"]:
                args = build_agy_args(profile_enabled, passthrough_args=["-p", "hello"], env={var: falsy})
                self.assertEqual(args, ["-p", "hello"], f"Failed for {var}={falsy}")

    def test_build_agy_args_precedence_hierarchy(self) -> None:
        profile_enabled = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(dangerously_skip_permissions=True),
        )
        profile_disabled = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(dangerously_skip_permissions=False),
        )

        # 1. CLI flag beats env var and profile setting
        # CLI negative beats env truthy and profile True
        args = build_agy_args(profile_enabled, passthrough_args=["--no-dsp", "-p", "hi"], env={"DSP": "1"})
        self.assertEqual(args, ["-p", "hi"])

        # CLI positive beats env falsy and profile False
        args = build_agy_args(profile_disabled, passthrough_args=["-y", "-p", "hi"], env={"DSP": "0"})
        self.assertEqual(args, ["--dangerously-skip-permissions", "-p", "hi"])

        # 2. Env var beats profile setting
        # Env truthy beats profile False
        args = build_agy_args(profile_disabled, passthrough_args=["-p", "hi"], env={"DSP": "1"})
        self.assertEqual(args, ["--dangerously-skip-permissions", "-p", "hi"])

        # Env falsy beats profile True
        args = build_agy_args(profile_enabled, passthrough_args=["-p", "hi"], env={"DSP": "0"})
        self.assertEqual(args, ["-p", "hi"])

        # 3. Profile setting beats default
        args = build_agy_args(profile_enabled, passthrough_args=["-p", "hi"], env={})
        self.assertEqual(args, ["--dangerously-skip-permissions", "-p", "hi"])

        args = build_agy_args(profile_disabled, passthrough_args=["-p", "hi"], env={})
        self.assertEqual(args, ["-p", "hi"])

    def test_build_agy_args_with_agy_path(self) -> None:
        profile = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(model="m", dangerously_skip_permissions=True),
        )
        args = build_agy_args(profile, passthrough_args=["-p", "hi"], agy_path=Path("/bin/agy"))
        self.assertEqual(
            args,
            ["/bin/agy", "--model", "m", "--dangerously-skip-permissions", "-p", "hi"],
        )

    @mock.patch("agym.launcher.exec_agy_interactive")
    @mock.patch("agym.launcher.run_agy_capture")
    def test_auto_prompt_two_stages_and_arguments(
        self, mock_capture: mock.Mock, mock_interactive: mock.Mock
    ) -> None:
        mock_capture.return_value = mock.Mock(returncode=0, stdout="Mocked raw response", stderr="")
        mock_interactive.return_value = 0
        profile = Profile(
            "personal",
            self.home_a,
            "now",
            settings=ProfileSettings(model="test-model", dangerously_skip_permissions=True),
        )
        agy_path = Path("/usr/bin/agy")
        user_prompt = "Plan feature A to B"

        code = run_auto_prompt(agy_path, profile, user_prompt, replace_process=False)
        self.assertEqual(code, 0)

        # Stage 1 verification
        mock_capture.assert_called_once()
        c_kwargs = mock_capture.call_args.kwargs
        self.assertEqual(c_kwargs["agy_path"], agy_path)
        self.assertEqual(
            c_kwargs["args"],
            [
                "--model",
                "test-model",
                "--dangerously-skip-permissions",
                "--prompt",
                build_stage1_prompt(user_prompt),
            ],
        )
        self.assertEqual(c_kwargs["env"]["HOME"], str(self.home_a.resolve()))

        # Stage 2 verification
        mock_interactive.assert_called_once()
        i_kwargs = mock_interactive.call_args.kwargs
        self.assertEqual(i_kwargs["agy_path"], agy_path)
        self.assertEqual(
            i_kwargs["args"],
            [
                "--model",
                "test-model",
                "--dangerously-skip-permissions",
                "--prompt-interactive",
                build_stage2_prompt("Mocked raw response"),
            ],
        )
        self.assertEqual(i_kwargs["env"]["HOME"], str(self.home_a.resolve()))
        self.assertFalse(i_kwargs["replace_process"])

    @mock.patch("agym.launcher.exec_agy_interactive")
    @mock.patch("agym.launcher.run_agy_capture")
    def test_auto_prompt_complex_raw_response_handling(
        self, mock_capture: mock.Mock, mock_interactive: mock.Mock
    ) -> None:
        complex_response = (
            "# Implementation Plan\n\n"
            "Step 1: Edit `main.py`\n"
            "```python\n"
            "import os\n"
            "print('Quotes: \"double\" and \\'single\\'')\n"
            "var = f\"$HOME and `backticks`\"\n"
            "```\n"
            "Shell test: ; && | > < $VAR\n"
            "Unicode test: \u2705 \U0001f680 \u00e9\u00e0\u00fc\u4e16\u754c\n"
        )
        mock_capture.return_value = mock.Mock(returncode=0, stdout=complex_response, stderr="")
        mock_interactive.return_value = 0
        profile = Profile("personal", self.home_a, "now")
        agy_path = Path("/usr/bin/agy")

        code = run_auto_prompt(agy_path, profile, "prompt with special chars", replace_process=False)
        self.assertEqual(code, 0)

        mock_interactive.assert_called_once()
        args = mock_interactive.call_args.kwargs["args"]
        expected_stage2_prompt = build_stage2_prompt(complex_response)
        self.assertEqual(args, ["--prompt-interactive", expected_stage2_prompt])
        # Ensure it is passed as a single item in args list
        self.assertEqual(len(args), 2)
        self.assertEqual(args[1], expected_stage2_prompt)

    def test_prompt_builders(self) -> None:
        raw_user = "refactor auth storage"
        s1 = build_stage1_prompt(raw_user)
        self.assertIn("refactor auth storage", s1)
        self.assertIn("implementation plan", s1.lower())
        self.assertIn("plain text", s1.lower())

        plan = "1. Delete old storage\n2. Add new storage"
        s2 = build_stage2_prompt(plan)
        self.assertIn(plan, s2)
        self.assertIn("implement", s2.lower())

    def test_spinner_lifecycle(self) -> None:
        import io
        buf = io.StringIO()
        spinner = Spinner("Testing spinner", stream=buf)
        with spinner:
            pass
        self.assertIn("Testing spinner", buf.getvalue())

    @mock.patch("agym.launcher.exec_agy_interactive")
    @mock.patch("agym.launcher.run_agy_capture")
    def test_auto_prompt_stage1_failure(
        self, mock_capture: mock.Mock, mock_interactive: mock.Mock
    ) -> None:
        mock_capture.return_value = mock.Mock(
            returncode=42, stdout="partial output", stderr="network error"
        )
        profile = Profile("personal", self.home_a, "now")
        code = run_auto_prompt(Path("/usr/bin/agy"), profile, "test prompt")
        self.assertEqual(code, 42)
        mock_interactive.assert_not_called()

    @mock.patch("agym.launcher.exec_agy_interactive")
    @mock.patch("agym.launcher.run_agy_capture")
    def test_auto_prompt_empty_stdout(
        self, mock_capture: mock.Mock, mock_interactive: mock.Mock
    ) -> None:
        mock_capture.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        profile = Profile("personal", self.home_a, "now")
        code = run_auto_prompt(Path("/usr/bin/agy"), profile, "test prompt")
        self.assertNotEqual(code, 0)
        mock_interactive.assert_not_called()

    @mock.patch("agym.launcher.exec_agy_interactive")
    @mock.patch("agym.launcher.run_agy_capture")
    def test_auto_prompt_whitespace_only_stdout(
        self, mock_capture: mock.Mock, mock_interactive: mock.Mock
    ) -> None:
        mock_capture.return_value = mock.Mock(returncode=0, stdout="  \n\t  \n", stderr="")
        profile = Profile("personal", self.home_a, "now")
        code = run_auto_prompt(Path("/usr/bin/agy"), profile, "test prompt")
        self.assertNotEqual(code, 0)
        mock_interactive.assert_not_called()

    @unittest.skipIf(os.name != "posix", "POSIX exec test")
    @mock.patch("agym.launcher.os.execve")
    @mock.patch("agym.launcher.run_agy_capture")
    def test_auto_prompt_posix_exec(
        self, mock_capture: mock.Mock, mock_execve: mock.Mock
    ) -> None:
        mock_capture.return_value = mock.Mock(returncode=0, stdout="Ready", stderr="")
        profile = Profile("personal", self.home_a, "now")
        agy_path = Path("/usr/bin/agy")
        code = run_auto_prompt(agy_path, profile, "prompt", replace_process=True)
        self.assertEqual(code, 0)
        mock_execve.assert_called_once()
        call_agy, call_argv, call_env = mock_execve.call_args.args
        self.assertEqual(call_agy, str(agy_path))
        self.assertEqual(call_argv, [str(agy_path), "--prompt-interactive", build_stage2_prompt("Ready")])
        self.assertEqual(call_env["HOME"], str(self.home_a.resolve()))
