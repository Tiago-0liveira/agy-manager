"""Unit tests for agym updater, release checking, verification, rollback, and platform updates."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agym import __version__
from agym import cli
from agym import updater
from agym.updater import (
    COOLDOWN_SECONDS,
    check_for_updates,
    do_update,
    execute_binary_update,
    execute_python_update,
    fetch_latest_release,
    is_newer_version,
    is_running_in_repo,
    load_update_state,
    maybe_prompt_startup_update,
    parse_semver,
    record_dismissal,
    run_update_cli,
    safe_replace_posix,
    save_update_state,
    should_prompt_user,
    spawn_windows_deferred_replacement,
    verify_binary,
)


class TestUpdaterVersionLogic(unittest.TestCase):
    def test_parse_semver_standard(self):
        self.assertEqual(parse_semver("0.1.0"), (0, 1, 0))
        self.assertEqual(parse_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_semver("2.10.15"), (2, 10, 15))

    def test_parse_semver_prerelease_and_metadata(self):
        self.assertEqual(parse_semver("v0.2.0-rc1"), (0, 2, 0))
        self.assertEqual(parse_semver("1.0.0+build.42"), (1, 0, 0))
        self.assertEqual(parse_semver("agym 0.1.2"), (0, 1, 2))

    def test_is_newer_version(self):
        self.assertTrue(is_newer_version("0.1.1", "0.1.0"))
        self.assertTrue(is_newer_version("0.2.0", "0.1.9"))
        self.assertTrue(is_newer_version("1.0.0", "0.99.99"))
        self.assertFalse(is_newer_version("0.1.0", "0.1.0"))
        self.assertFalse(is_newer_version("0.1.0", "0.1.1"))
        self.assertFalse(is_newer_version("0.0.9", "0.1.0"))

    def test_correct_platform_assets(self):
        release = {
            "tag_name": "v0.2.0",
            "assets": [
                {"name": "agym-windows-amd64.exe", "browser_download_url": "https://example/win"},
                {"name": "agym-darwin-arm64", "browser_download_url": "https://example/darwin-arm"},
                {"name": "agym-darwin-amd64", "browser_download_url": "https://example/darwin-intel"},
                {"name": "agym-linux-amd64", "browser_download_url": "https://example/linux"},
                {"name": "agym-0.2.0-py3-none-any.whl", "browser_download_url": "https://example/wheel"},
            ],
        }
        response = mock.MagicMock()
        response.read.return_value = json.dumps(release).encode()
        response.__enter__.return_value = response

        # Test Windows AMD64
        with mock.patch("agym.updater.urllib.request.urlopen", return_value=response), \
             mock.patch("agym.updater.platform.system", return_value="Windows"), \
             mock.patch("agym.updater.platform.machine", return_value="AMD64"):
            info = fetch_latest_release()
            self.assertEqual(info["standalone_url"], "https://example/win")
            self.assertEqual(info["wheel_url"], "https://example/wheel")

        # Test Linux AMD64
        with mock.patch("agym.updater.urllib.request.urlopen", return_value=response), \
             mock.patch("agym.updater.platform.system", return_value="Linux"), \
             mock.patch("agym.updater.platform.machine", return_value="x86_64"):
            info = fetch_latest_release()
            self.assertEqual(info["standalone_url"], "https://example/linux")

        # Test macOS ARM64
        with mock.patch("agym.updater.urllib.request.urlopen", return_value=response), \
             mock.patch("agym.updater.platform.system", return_value="Darwin"), \
             mock.patch("agym.updater.platform.machine", return_value="arm64"):
            info = fetch_latest_release()
            self.assertEqual(info["standalone_url"], "https://example/darwin-arm")

        # Test macOS Intel
        with mock.patch("agym.updater.urllib.request.urlopen", return_value=response), \
             mock.patch("agym.updater.platform.system", return_value="Darwin"), \
             mock.patch("agym.updater.platform.machine", return_value="x86_64"):
            info = fetch_latest_release()
            self.assertEqual(info["standalone_url"], "https://example/darwin-intel")


class TestUpdaterStateAndCooldown(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_updater_")
        self.cache_dir = Path(self.temp_dir) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "updater.json"

        self._patcher = mock.patch("agym.updater.get_update_cache_file", return_value=self.cache_file)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_empty_state(self):
        state = load_update_state()
        self.assertEqual(state, {})

    def test_save_and_reload_state(self):
        payload = {"latest_version": "0.2.0", "last_check_ts": 12345.0}
        save_update_state(payload)
        reloaded = load_update_state()
        self.assertEqual(reloaded["latest_version"], "0.2.0")
        self.assertEqual(reloaded["last_check_ts"], 12345.0)

    def test_24_hour_negation_cooldown(self):
        release_info = {"version": "0.2.0", "tag": "v0.2.0"}

        # Fresh state with newer version -> should prompt
        with mock.patch("agym.updater.__version__", "0.1.0"):
            self.assertTrue(should_prompt_user(release_info, now_ts=1000.0))

            # User negates at t=1000
            record_dismissal("0.2.0", now_ts=1000.0)

            # 1 hour later (t=4600) -> should NOT prompt (suppressed)
            self.assertFalse(should_prompt_user(release_info, now_ts=4600.0))

            # 23 hours later (t=1000 + 82800) -> should still NOT prompt
            self.assertFalse(should_prompt_user(release_info, now_ts=1000.0 + 82800))

            # 25 hours later (t=1000 + 90000) -> 24h passed, should prompt again!
            self.assertTrue(should_prompt_user(release_info, now_ts=1000.0 + 90000))

    def test_newer_version_bypasses_dismissal(self):
        release_info = {"version": "0.2.0", "tag": "v0.2.0"}
        with mock.patch("agym.updater.__version__", "0.1.0"):
            # User dismisses 0.2.0 at t=1000
            record_dismissal("0.2.0", now_ts=1000.0)
            self.assertFalse(should_prompt_user(release_info, now_ts=4600.0))

            # A newer version 0.2.1 arrives within 1 hour: should prompt immediately!
            newer_release = {"version": "0.2.1", "tag": "v0.2.1"}
            self.assertTrue(should_prompt_user(newer_release, now_ts=4600.0))

    def test_should_not_prompt_if_same_or_older_version(self):
        release_info = {"version": "0.1.0", "tag": "v0.1.0"}
        with mock.patch("agym.updater.__version__", "0.1.0"):
            self.assertFalse(should_prompt_user(release_info))

        older_release = {"version": "0.0.9", "tag": "v0.0.9"}
        with mock.patch("agym.updater.__version__", "0.1.0"):
            self.assertFalse(should_prompt_user(older_release))


class TestStartupPromptFiltering(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_prompt_")
        self.cache_file = Path(self.temp_dir) / "updater.json"
        self._patcher = mock.patch("agym.updater.get_update_cache_file", return_value=self.cache_file)
        self._patcher.start()
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("AGYM_NO_UPDATE_CHECK", None)
        self._repo_patch = mock.patch("agym.updater.is_running_in_repo", return_value=False)
        self._repo_patch.start()

    def tearDown(self):
        self._repo_patch.stop()
        self._env_patch.stop()
        self._patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bypassed_when_running_in_repo(self):
        with mock.patch("agym.updater.is_running_in_repo", return_value=True):
            with mock.patch("agym.updater.check_for_updates") as mock_check:
                maybe_prompt_startup_update(["list"])
                mock_check.assert_not_called()

    def test_bypassed_when_env_var_set(self):
        with mock.patch.dict(os.environ, {"AGYM_NO_UPDATE_CHECK": "1"}):
            with mock.patch("agym.updater.check_for_updates") as mock_check:
                maybe_prompt_startup_update(["list"])
                mock_check.assert_not_called()

    def test_bypassed_when_not_a_tty(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch("agym.updater.check_for_updates") as mock_check:
                maybe_prompt_startup_update(["list"])
                mock_check.assert_not_called()

    def test_bypassed_for_help_version_statusline_update_and_json(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            with mock.patch("agym.updater.check_for_updates") as mock_check:
                # --help / -h / help
                maybe_prompt_startup_update(["--help"])
                maybe_prompt_startup_update(["personal", "-h"])
                maybe_prompt_startup_update(["help"])

                # --version / -v
                maybe_prompt_startup_update(["--version"])
                maybe_prompt_startup_update(["-v"])

                # update
                maybe_prompt_startup_update(["update"])
                maybe_prompt_startup_update(["update", "--check"])

                # statusline
                maybe_prompt_startup_update(["statusline"])
                maybe_prompt_startup_update(["--statusline-render"])

                # --json / -j
                maybe_prompt_startup_update(["usage", "--json"])
                maybe_prompt_startup_update(["tokens", "-j"])

                mock_check.assert_not_called()

    def test_interactive_prompt_user_says_no(self):
        fake_release = {
            "version": "0.9.0",
            "tag": "v0.9.0",
            "html_url": "https://github.com/example/release",
        }
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("agym.updater.check_for_updates", return_value=(fake_release, True)), \
             mock.patch("sys.stdin.readline", return_value="n\n"), \
             mock.patch("agym.updater.do_update") as mock_do_update:
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                maybe_prompt_startup_update(["personal"])

            mock_do_update.assert_not_called()
            output = out.getvalue()
            self.assertIn("[agym] Update available: ", output)
            self.assertIn("Update now? [y/N]:", output)
            self.assertIn("Update postponed.", output)

            # Verify dismissal was recorded
            state = load_update_state()
            self.assertEqual(state.get("dismissed_version"), "0.9.0")

    def test_interactive_prompt_user_says_yes(self):
        fake_release = {
            "version": "0.9.0",
            "tag": "v0.9.0",
            "html_url": "https://github.com/example/release",
        }
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("agym.updater.check_for_updates", return_value=(fake_release, True)), \
             mock.patch("sys.stdin.readline", return_value="y\n"), \
             mock.patch("agym.updater.do_update", return_value=0) as mock_do_update:
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                maybe_prompt_startup_update(["personal"])

            mock_do_update.assert_called_once_with(fake_release)
            self.assertIn("Update complete!", out.getvalue())


class TestUpdateCliCommand(unittest.TestCase):
    def test_update_check_flag_when_newer(self):
        fake_release = {
            "version": "0.2.0",
            "tag": "v0.2.0",
            "html_url": "https://github.com/example",
        }
        with mock.patch("agym.updater.check_for_updates", return_value=(fake_release, True)), \
             mock.patch("agym.updater.__version__", "0.1.0"):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = run_update_cli(["--check"])
            self.assertEqual(code, 0)
            self.assertIn("Current version: 0.1.0", out.getvalue())
            self.assertIn("Latest version:  0.2.0", out.getvalue())
            self.assertIn("A new version (0.2.0) is available", out.getvalue())

    def test_update_check_already_up_to_date(self):
        fake_release = {
            "version": "0.1.0",
            "tag": "v0.1.0",
            "html_url": "https://github.com/example",
        }
        with mock.patch("agym.updater.check_for_updates", return_value=(fake_release, False)), \
             mock.patch("agym.updater.__version__", "0.1.0"):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = run_update_cli(["--check"])
            self.assertEqual(code, 0)
            self.assertIn("agym is up to date.", out.getvalue())

    def test_update_run_already_up_to_date_no_force(self):
        fake_release = {
            "version": "0.1.0",
            "tag": "v0.1.0",
            "html_url": "https://github.com/example",
        }
        with mock.patch("agym.updater.check_for_updates", return_value=(fake_release, False)), \
             mock.patch("agym.updater.__version__", "0.1.0"):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = run_update_cli([])
            self.assertEqual(code, 0)
            self.assertIn("already up to date", out.getvalue())

    def test_update_run_force_flag_reinstalls(self):
        fake_release = {
            "version": "0.1.0",
            "tag": "v0.1.0",
            "html_url": "https://github.com/example",
        }
        with mock.patch("agym.updater.check_for_updates", return_value=(fake_release, False)), \
             mock.patch("agym.updater.__version__", "0.1.0"), \
             mock.patch("agym.updater.do_update", return_value=0) as mock_do:
            code = run_update_cli(["--force"])
            self.assertEqual(code, 0)
            mock_do.assert_called_once_with(fake_release)

    def test_update_run_calls_do_update_when_newer(self):
        fake_release = {
            "version": "0.2.0",
            "tag": "v0.2.0",
            "html_url": "https://github.com/example",
        }
        with mock.patch("agym.updater.check_for_updates", return_value=(fake_release, True)), \
             mock.patch("agym.updater.__version__", "0.1.0"), \
             mock.patch("agym.updater.do_update", return_value=0) as mock_do:
            code = run_update_cli([])
            self.assertEqual(code, 0)
            mock_do.assert_called_once_with(fake_release)

    def test_offline_github_failure(self):
        # Offline network failure in check_for_updates(force_network=True)
        with mock.patch("agym.updater.fetch_latest_release", return_value=None):
            release_info, is_newer = check_for_updates(force_network=True)
            self.assertIsNone(release_info)
            self.assertFalse(is_newer)

            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = run_update_cli([])
            self.assertEqual(code, 1)
            self.assertIn("Failed to reach GitHub Releases API", err.getvalue())


class TestBinaryVerification(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_verify_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_verify_binary_missing_or_empty(self):
        non_existent = Path(self.temp_dir) / "agym_missing"
        with self.assertRaises(RuntimeError) as ctx:
            verify_binary(non_existent, "0.2.0")
        self.assertIn("missing or empty", str(ctx.exception))

        empty_file = Path(self.temp_dir) / "agym_empty"
        empty_file.touch()
        with self.assertRaises(RuntimeError) as ctx:
            verify_binary(empty_file, "0.2.0")
        self.assertIn("missing or empty", str(ctx.exception))

    def test_verify_binary_execution_failure(self):
        binary = Path(self.temp_dir) / "agym_mock"
        binary.write_text("#!/bin/sh\nexit 1\n")
        completed = mock.MagicMock(returncode=127, stdout="", stderr="command failed")
        with mock.patch("agym.updater.subprocess.run", return_value=completed):
            with self.assertRaises(RuntimeError) as ctx:
                verify_binary(binary, "0.2.0")
            self.assertIn("exit code 127", str(ctx.exception))

    def test_verify_binary_version_mismatch(self):
        binary = Path(self.temp_dir) / "agym_mock"
        binary.write_text("#!/bin/sh\n")
        completed = mock.MagicMock(returncode=0, stdout="0.1.9\n", stderr="")
        with mock.patch("agym.updater.subprocess.run", return_value=completed):
            with self.assertRaises(RuntimeError) as ctx:
                verify_binary(binary, "0.2.0")
            self.assertIn("version mismatch", str(ctx.exception))

    def test_verify_binary_success(self):
        binary = Path(self.temp_dir) / "agym_mock"
        binary.write_text("#!/bin/sh\n")
        completed = mock.MagicMock(returncode=0, stdout="0.2.0\n", stderr="")
        with mock.patch("agym.updater.subprocess.run", return_value=completed):
            # Should not raise
            verify_binary(binary, "0.2.0")
            verify_binary(binary, "v0.2.0")


class TestBinaryReplacementRollback(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_replace_")
        self.current_exe = Path(self.temp_dir) / "agym"
        self.new_exe = Path(self.temp_dir) / "agym.new"
        self.old_exe = Path(self.temp_dir) / "agym.old"
        self.current_exe.write_text("ORIGINAL_BINARY_CONTENT")
        self.new_exe.write_text("NEW_BINARY_CONTENT")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_posix_replacement_success(self):
        safe_replace_posix(self.current_exe, self.new_exe)
        # current_exe now has new content
        self.assertEqual(self.current_exe.read_text(), "NEW_BINARY_CONTENT")
        # agym.new and agym.old are gone
        self.assertFalse(self.new_exe.exists())
        self.assertFalse(self.current_exe.with_name("agym.old").exists())

    def test_failed_binary_replacement_rollback(self):
        orig_rename = Path.rename
        call_count = [0]

        def mock_rename(target, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                # Fail on Step 2 (moving agym.new -> agym)
                raise OSError("Simulated disk error during replacement")
            # Step 1 (agym -> agym.old) and Step 3 (rollback agym.old -> agym) succeed
            # In unittest mock, call the unpatched orig_rename
            return orig_rename(self.old_exe if call_count[0] == 3 else self.current_exe, target)

        with mock.patch.object(Path, "rename", side_effect=mock_rename):
            with self.assertRaises(RuntimeError) as ctx:
                safe_replace_posix(self.current_exe, self.new_exe)

            self.assertIn("Restored original executable", str(ctx.exception))

        # Verification: original binary was restored via rollback!
        self.assertTrue(self.current_exe.exists())
        self.assertEqual(self.current_exe.read_text(), "ORIGINAL_BINARY_CONTENT")


class TestWindowsDeferredReplacement(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_win_")
        self.current_exe = Path(self.temp_dir) / "agym.exe"
        self.new_exe = Path(self.temp_dir) / "agym.exe.new"
        self.old_exe = Path(self.temp_dir) / "agym.exe.old"
        self.current_exe.write_text("WIN_ORIGINAL")
        self.new_exe.write_text("WIN_NEW")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_windows_deferred_replacement_spawns_helper_and_exits(self):
        with mock.patch("agym.updater.subprocess.Popen") as mock_popen, \
             mock.patch("sys.exit") as mock_exit:
            spawn_windows_deferred_replacement(
                self.current_exe,
                self.new_exe,
                self.old_exe,
                pid=12345,
            )

            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            cmd = args[0]
            self.assertEqual(cmd[0], "cmd.exe")
            self.assertEqual(cmd[1], "/c")
            bat_path = Path(cmd[2])
            self.assertTrue(bat_path.exists())

            bat_content = bat_path.read_text()
            self.assertIn("set PID=12345", bat_content)
            self.assertIn(str(self.current_exe), bat_content)
            self.assertIn(str(self.old_exe), bat_content)
            self.assertIn(str(self.new_exe), bat_content)
            self.assertIn("move /y", bat_content)

            mock_exit.assert_called_once_with(0)

            # Cleanup helper script
            try:
                bat_path.unlink()
            except OSError:
                pass


class TestPythonWheelUpdate(unittest.TestCase):
    def test_python_wheel_update_success(self):
        proc = mock.MagicMock(returncode=0)
        with mock.patch("agym.updater.subprocess.run", return_value=proc) as mock_run, \
             mock.patch("agym.updater.shutil.which", return_value=None):
            execute_python_update("https://github.com/example/agym-0.2.0-py3-none-any.whl")
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], sys.executable)
            self.assertEqual(cmd[1:4], ["-m", "pip", "install"])
            self.assertIn("--upgrade", cmd)
            self.assertEqual(cmd[-1], "https://github.com/example/agym-0.2.0-py3-none-any.whl")

    def test_python_wheel_update_no_wheel_raises_error(self):
        # Must NOT install from main / git repository if wheel is missing
        with mock.patch("agym.updater.subprocess.run") as mock_run:
            with self.assertRaises(RuntimeError) as ctx:
                execute_python_update(None)
            self.assertIn("No wheel (.whl) asset found", str(ctx.exception))
            mock_run.assert_not_called()

    def test_python_wheel_update_pip_failure(self):
        proc = mock.MagicMock(returncode=1)
        with mock.patch("agym.updater.subprocess.run", return_value=proc), \
             mock.patch("agym.updater.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                execute_python_update("https://example/agym.whl")
            self.assertIn("failed with exit code 1", str(ctx.exception))

    def test_python_pipx_update_success(self):
        proc = mock.MagicMock(returncode=0)
        with mock.patch("agym.updater.subprocess.run", return_value=proc) as mock_run, \
             mock.patch("agym.updater.shutil.which", return_value="/usr/bin/pipx"), \
             mock.patch.object(sys, "prefix", "/home/user/.local/pipx/venvs/agym"):
            execute_python_update("https://github.com/example/agym-0.2.0-py3-none-any.whl")
            mock_run.assert_called_once_with([
                "/usr/bin/pipx", "install", "--force", "https://github.com/example/agym-0.2.0-py3-none-any.whl"
            ])


class TestCliIntegration(unittest.TestCase):
    def test_cli_dispatches_update_subcommand(self):
        with mock.patch("agym.cli.run_update_cli", return_value=0) as mock_run:
            code = cli.main(["update", "--check"])
            self.assertEqual(code, 0)
            mock_run.assert_called_once_with(["--check"])


class TestRunningInRepoDetection(unittest.TestCase):
    def test_is_running_in_repo_in_current_repo(self):
        self.assertTrue(is_running_in_repo())

    def test_is_running_in_repo_false_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_pkg_file = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "agym" / "updater.py"
            with mock.patch("agym.updater.Path.cwd", return_value=tmp_path), \
                 mock.patch.object(updater, "__file__", str(fake_pkg_file)):
                self.assertFalse(is_running_in_repo())

    def test_is_running_in_repo_true_when_cwd_in_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_dir = tmp_path / "my_agy_repo"
            repo_git = repo_dir / ".git"
            repo_git.mkdir(parents=True)
            repo_agym = repo_dir / "agym"
            repo_agym.mkdir(parents=True)
            (repo_agym / "cli.py").touch()

            subdir = repo_dir / "some" / "subdir"
            subdir.mkdir(parents=True)

            fake_pkg_file = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "agym" / "updater.py"
            with mock.patch("agym.updater.Path.cwd", return_value=subdir), \
                 mock.patch.object(updater, "__file__", str(fake_pkg_file)):
                self.assertTrue(is_running_in_repo())


if __name__ == "__main__":
    unittest.main()
