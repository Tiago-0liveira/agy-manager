"""Unit tests for agym updater, release checking, and 24-hour negation logic."""

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
    fetch_latest_release,
    is_newer_version,
    load_update_state,
    maybe_prompt_startup_update,
    parse_semver,
    record_dismissal,
    run_update_cli,
    save_update_state,
    should_prompt_user,
)


class TestUpdaterVersionLogic(unittest.TestCase):
    def test_release_asset_matches_platform_architecture(self):
        release = {
            "tag_name": "v0.2.0",
            "assets": [
                {"name": "agym-darwin-arm64", "browser_download_url": "https://example/arm"},
                {"name": "agym-linux-amd64", "browser_download_url": "https://example/linux"},
                {"name": "agym-0.2.0-py3-none-any.whl", "browser_download_url": "https://example/wheel"},
            ],
        }
        response = mock.MagicMock()
        response.read.return_value = json.dumps(release).encode()
        response.__enter__.return_value = response
        with mock.patch("agym.updater.urllib.request.urlopen", return_value=response), \
             mock.patch("agym.updater.platform.system", return_value="Darwin"), \
             mock.patch("agym.updater.platform.machine", return_value="x86_64"):
            info = fetch_latest_release()
        self.assertEqual(info["standalone_url"], "")
        self.assertEqual(info["wheel_url"], "https://example/wheel")

    def test_parse_semver_standard(self):
        self.assertEqual(parse_semver("0.1.0"), (0, 1, 0))
        self.assertEqual(parse_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_semver("2.10.15"), (2, 10, 15))

    def test_parse_semver_prerelease_and_metadata(self):
        self.assertEqual(parse_semver("v0.2.0-rc1"), (0, 2, 0))
        self.assertEqual(parse_semver("1.0.0+build.42"), (1, 0, 0))

    def test_is_newer_version(self):
        self.assertTrue(is_newer_version("0.1.1", "0.1.0"))
        self.assertTrue(is_newer_version("0.2.0", "0.1.9"))
        self.assertTrue(is_newer_version("1.0.0", "0.99.99"))
        self.assertFalse(is_newer_version("0.1.0", "0.1.0"))
        self.assertFalse(is_newer_version("0.1.0", "0.1.1"))
        self.assertFalse(is_newer_version("0.0.9", "0.1.0"))


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

            # What if a brand new version arrives (0.2.1) within 1 hour of dismissing 0.2.0?
            brand_new_release = {"version": "0.2.1", "tag": "v0.2.1"}
            # It should prompt immediately because 0.2.1 was never dismissed!
            self.assertTrue(should_prompt_user(brand_new_release, now_ts=4600.0))

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

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

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

    def test_bypassed_for_statusline_and_json(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            with mock.patch("agym.updater.check_for_updates") as mock_check:
                maybe_prompt_startup_update(["statusline"])
                maybe_prompt_startup_update(["usage", "--json"])
                maybe_prompt_startup_update(["tokens", "-j"])
                maybe_prompt_startup_update(["update"])
                maybe_prompt_startup_update(["--help"])
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
            self.assertIn("A new version is available", out.getvalue())
            self.assertIn("Update postponed", out.getvalue())

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
    def test_update_check_flag(self):
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


class TestCliIntegration(unittest.TestCase):
    def test_cli_dispatches_update_subcommand(self):
        with mock.patch("agym.cli.run_update_cli", return_value=0) as mock_run:
            code = cli.main(["update", "--check"])
            self.assertEqual(code, 0)
            mock_run.assert_called_once_with(["--check"])


if __name__ == "__main__":
    unittest.main()
