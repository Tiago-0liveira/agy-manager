from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from agym.cli import _statusline, main as cli_main
from agym.profiles import ProfileSettings, ProfileStore
from agym.statusline import (
    _get_short_path,
    AccountQuotaInfo,
    BucketQuota,
    extract_payload_quota,
    format_mini_bar,
    format_short_reset_duration,
    format_short_tokens,
    format_vcs_tag,
    get_profile_settings_path,
    get_rank_color,
    get_statusline_command,
    get_statusline_script_path,
    get_statusline_status,
    install_statusline_script,
    load_cache_quota,
    main as statusline_main,
    parse_git_head,
    render_statusline,
    resolve_account_name,
    resolve_context_window,
    resolve_git_vcs,
    resolve_model_display,
    resolve_python_executable,
    resolve_quota,
    sync_all_profiles,
    sync_profile_statusline,
    VCSInfo,
)


class StatuslineFormattingTests(unittest.TestCase):
    def test_rank_colors(self) -> None:
        self.assertEqual(get_rank_color(95), "\033[38;5;48m")
        self.assertEqual(get_rank_color(90), "\033[38;5;48m")
        self.assertEqual(get_rank_color(80), "\033[38;5;40m")
        self.assertEqual(get_rank_color(60), "\033[38;5;184m")
        self.assertEqual(get_rank_color(30), "\033[38;5;214m")
        self.assertEqual(get_rank_color(15), "\033[38;5;208m")
        self.assertEqual(get_rank_color(5), "\033[38;5;196m")
        self.assertEqual(get_rank_color(0), "\033[38;5;196m")
        self.assertEqual(get_rank_color(90, no_color=True), "")
        self.assertEqual(get_rank_color(None), "")

    def test_mini_bar(self) -> None:
        bar = format_mini_bar(100, width=6, no_color=True)
        self.assertEqual(bar, "[██████]")
        bar_half = format_mini_bar(50, width=6, no_color=True)
        self.assertEqual(bar_half, "[███░░░]")
        bar_zero = format_mini_bar(0, width=6, no_color=True)
        self.assertEqual(bar_zero, "[░░░░░░]")
        self.assertEqual(format_mini_bar(None), "")

    def test_short_tokens(self) -> None:
        self.assertEqual(format_short_tokens(500), "500")
        self.assertEqual(format_short_tokens(24000), "24k")
        self.assertEqual(format_short_tokens(125000), "125k")
        self.assertEqual(format_short_tokens(1500000), "1.5M")
        self.assertEqual(format_short_tokens(None), "-")

    def test_short_reset_duration(self) -> None:
        self.assertEqual(format_short_reset_duration(seconds_remaining=0), "now")
        self.assertEqual(format_short_reset_duration(seconds_remaining=-10), "now")
        self.assertEqual(format_short_reset_duration(seconds_remaining=45), "<1m")
        self.assertEqual(format_short_reset_duration(seconds_remaining=300), "5m")
        self.assertEqual(format_short_reset_duration(seconds_remaining=4680), "1h18m")
        self.assertEqual(format_short_reset_duration(seconds_remaining=14400), "4h")
        self.assertEqual(format_short_reset_duration(seconds_remaining=14500), "4h+")
        self.assertEqual(format_short_reset_duration(seconds_remaining=172800), "2d")
        self.assertEqual(format_short_reset_duration(seconds_remaining=180000), "2d+")
        self.assertEqual(format_short_reset_duration(), "-")


class StatuslineResolutionTests(unittest.TestCase):
    def test_resolve_account_name(self) -> None:
        # 1. From AGYM_PROFILE
        with patch.dict(os.environ, {"AGYM_PROFILE": "work_profile"}):
            self.assertEqual(resolve_account_name(), "work_profile")

        # 2. From HOME path
        with patch.dict(os.environ, {"AGYM_PROFILE": "", "HOME": "/tmp/agym/profiles/personal/home"}):
            self.assertEqual(resolve_account_name(), "personal")

        # 3. From payload
        with patch.dict(os.environ, {"AGYM_PROFILE": "", "HOME": "/home/user"}):
            self.assertEqual(resolve_account_name({"account": "custom_acc"}), "custom_acc")

        # 4. Fallback
        with patch.dict(os.environ, {"AGYM_PROFILE": "", "HOME": "/home/user"}):
            self.assertEqual(resolve_account_name(), "default")

    def test_resolve_model_display(self) -> None:
        payload = {"model": {"display_name": "Gemini 3.8 Flash (High)", "id": "gemini-3.8-flash"}}
        self.assertEqual(resolve_model_display(payload), "Gemini 3.8 Flash")

        payload_id_only = {"model": {"id": "gemini-2.5-flash"}}
        self.assertEqual(resolve_model_display(payload_id_only), "gemini-2.5-flash")

        payload_str = {"model": "Claude 3.7 Sonnet (Thinking)"}
        self.assertEqual(resolve_model_display(payload_str), "Claude 3.7 Sonnet")

        self.assertIsNone(resolve_model_display({}))
        self.assertIsNone(resolve_model_display(None))

    def test_resolve_context_window(self) -> None:
        payload = {
            "context_window": {
                "used_percentage": 15.2,
                "current_usage": 30400,
                "context_window_size": 200000,
            }
        }
        ctx = resolve_context_window(payload)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.used_percentage, 15)
        self.assertEqual(ctx.current_tokens, 30400)
        self.assertEqual(ctx.total_tokens, 200000)

        # Calculates percentage if omitted
        payload2 = {
            "context_window": {
                "current_usage": 50000,
                "context_window_size": 100000,
            }
        }
        ctx2 = resolve_context_window(payload2)
        self.assertIsNotNone(ctx2)
        self.assertEqual(ctx2.used_percentage, 50)

        self.assertIsNone(resolve_context_window({}))

    def test_extract_payload_quota(self) -> None:
        quota_dict = {
            "gemini-5h": {
                "remaining_fraction": 0.85,
                "reset_in_seconds": 3600,
            },
            "gemini-weekly": {
                "remaining_fraction": 0.95,
                "reset_in_seconds": 86400,
            },
            "3p-5h": {
                "remaining_fraction": 1.0,
                "reset_in_seconds": 7200,
            },
        }
        info = extract_payload_quota(quota_dict)
        self.assertIsNotNone(info)
        self.assertEqual(info.gemini_5h.percentage, 85)
        self.assertEqual(info.gemini_weekly.percentage, 95)
        self.assertEqual(info.claude_5h.percentage, 100)
        self.assertIsNone(info.claude_weekly)

    def test_load_cache_quota(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            cache_dir = tmp_dir / "cache" / "usage"
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / "test_acc.json"
            cache_data = {
                "parsed_data": {
                    "subscription": {"date": "2027-05-15"},
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-5h",
                                    "percentage": 88,
                                    "remaining_fraction": 0.88,
                                    "reset_time": "2026-09-21T18:00:00Z",
                                },
                                {
                                    "id": "gemini-weekly",
                                    "percentage": 92,
                                    "remaining_fraction": 0.92,
                                    "reset_time": "2026-09-28T00:00:00Z",
                                },
                            ],
                        }
                    ],
                }
            }
            cache_file.write_text(json.dumps(cache_data))

            info = load_cache_quota("test_acc", data_root=tmp_dir)
            self.assertIsNotNone(info)
            self.assertEqual(info.gemini_5h.percentage, 88)
            self.assertEqual(info.gemini_weekly.percentage, 92)
            self.assertEqual(info.subscription_date, "2027-05-15")

            # Nonexistent profile
            self.assertIsNone(load_cache_quota("missing_acc", data_root=tmp_dir))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_resolve_quota_precedence(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            cache_dir = tmp_dir / "cache" / "usage"
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / "test_acc.json"
            cache_data = {
                "parsed_data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-5h",
                                    "percentage": 50,
                                    "remaining_fraction": 0.50,
                                },
                                {
                                    "id": "gemini-weekly",
                                    "percentage": 70,
                                    "remaining_fraction": 0.70,
                                },
                            ],
                        }
                    ],
                }
            }
            cache_file.write_text(json.dumps(cache_data))

            # Live quota overrides 5h but weekly falls back to cache
            payload = {
                "quota": {
                    "gemini-5h": {
                        "remaining_fraction": 0.90,
                        "reset_in_seconds": 1800,
                    }
                }
            }
            resolved = resolve_quota("test_acc", payload, data_root=tmp_dir)
            self.assertEqual(resolved.gemini_5h.percentage, 90)
            self.assertEqual(resolved.gemini_weekly.percentage, 70)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class StatuslineRenderingTests(unittest.TestCase):
    def test_render_full_statusline_no_color(self) -> None:
        payload = {
            "model": {"display_name": "Gemini 3.8 Flash"},
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.90, "reset_in_seconds": 4680},
                "gemini-weekly": {"remaining_fraction": 0.95, "reset_in_seconds": 86400 * 6},
                "3p-5h": {"remaining_fraction": 1.0, "reset_in_seconds": 7200},
            },
            "context_window": {
                "used_percentage": 12,
                "current_usage": 24000,
                "context_window_size": 200000,
            },
        }
        line = render_statusline(
            payload,
            profile_name="tiagoliv",
            terminal_width=110,
            no_color=True,
        )
        self.assertIn("👤 tiagoliv", line)
        self.assertIn("⚡ Gemini 3.8 Flash", line)
        self.assertIn("5h: [█████░] 90% (1h18m)", line)
        self.assertIn("Wk: 95% (6d)", line)
        self.assertIn("Claude: 100%", line)
        self.assertIn("Ctx: 12% (24k/200k)", line)

    def test_render_standard_width_no_color(self) -> None:
        payload = {
            "model": {"display_name": "Gemini 3.8 Flash"},
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.90, "reset_in_seconds": 4680},
                "gemini-weekly": {"remaining_fraction": 0.95, "reset_in_seconds": 86400 * 6},
            },
            "context_window": {"used_percentage": 15},
        }
        line = render_statusline(
            payload,
            profile_name="tiagoliv",
            terminal_width=80,
            no_color=True,
        )
        self.assertIn("👤 tiagoliv", line)
        self.assertIn("5h: 90% (1h18m)", line)
        self.assertIn("Wk: 95% (6d)", line)
        self.assertIn("Ctx: 15%", line)
        # Claude is omitted at 80 cols to keep statusline uncluttered
        self.assertNotIn("Claude:", line)

    def test_render_compact_width(self) -> None:
        payload = {
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.75, "reset_in_seconds": 1800},
            },
            "context_window": {"used_percentage": 20},
        }
        line = render_statusline(
            payload,
            profile_name="work",
            terminal_width=60,
            no_color=True,
        )
        self.assertIn("👤 work", line)
        self.assertIn("5h: 75% (30m)", line)
        self.assertIn("Ctx: 20%", line)

    def test_render_ultra_compact_width(self) -> None:
        payload = {
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.50, "reset_in_seconds": 1800},
            },
        }
        line = render_statusline(
            payload,
            profile_name="work",
            terminal_width=50,
            no_color=True,
        )
        self.assertEqual(line, "👤 work │ 5h: 50% (30m)")

    def test_render_empty_payload(self) -> None:
        line = render_statusline(
            {},
            profile_name="empty_profile",
            terminal_width=80,
            no_color=True,
        )
        self.assertIn("👤 empty_profile", line)
        self.assertIn("5h: -", line)


class InstallationAndSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.config_root = self.tmp_dir / "config"
        self.data_root = self.tmp_dir / "data"
        self.store = ProfileStore(config_root=self.config_root, data_root=self.data_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_install_statusline_script(self) -> None:
        script_path = install_statusline_script(self.data_root)
        self.assertTrue(script_path.is_file())
        if sys.platform != "win32" and os.name != "nt":
            mode = script_path.stat().st_mode
            self.assertTrue(bool(mode & 0o111), "Script should be executable")
            content = script_path.read_text()
            self.assertIn("from agym.statusline import main", content)
        else:
            py_target = script_path.parent / "statusline.py"
            self.assertTrue(py_target.is_file())
            self.assertIn("from agym.statusline import main", py_target.read_text())

    def test_sync_profile_statusline_preserves_existing_settings(self) -> None:
        profile = self.store.create("work")
        settings_file = get_profile_settings_path(profile.home)
        settings_file.parent.mkdir(parents=True, exist_ok=True)

        existing_data = {
            "model": "Gemini 3.8 Flash (High)",
            "trustedWorkspaces": ["/path/to/project"],
        }
        settings_file.write_text(json.dumps(existing_data, indent=2))

        script_path = install_statusline_script(self.data_root)
        updated = sync_profile_statusline(profile.home, script_path, enabled=True)
        self.assertTrue(updated)

        with settings_file.open("r") as f:
            new_data = json.load(f)

        # Check existing settings were preserved
        self.assertEqual(new_data["model"], "Gemini 3.8 Flash (High)")
        self.assertEqual(new_data["trustedWorkspaces"], ["/path/to/project"])
        # Check statusLine was added
        self.assertIn("statusLine", new_data)
        self.assertEqual(new_data["statusLine"]["type"], "command")
        if sys.platform == "win32":
            expected_cmd = str(_get_short_path(script_path))
        else:
            expected_cmd = str(script_path.resolve())
        self.assertEqual(new_data["statusLine"]["command"], expected_cmd)
        self.assertTrue(new_data["statusLine"]["enabled"])

        # Subsequent sync without changes should return False (idempotent)
        updated2 = sync_profile_statusline(profile.home, script_path, enabled=True)
        self.assertFalse(updated2)

    def test_resolve_python_executable_windows_gui(self) -> None:
        with patch("sys.platform", "win32"), patch("os.name", "nt"):
            with patch("pathlib.Path.is_file", return_value=True):
                resolved = resolve_python_executable(gui=True)
                self.assertEqual(resolved.name.lower(), "pythonw.exe")

    def test_resolve_python_executable_windows_console(self) -> None:
        with patch("sys.platform", "win32"), patch("os.name", "nt"):
            resolved = resolve_python_executable(gui=False)
            self.assertEqual(resolved, Path(sys.executable).resolve())

    def test_resolve_python_executable_posix(self) -> None:
        with patch("sys.platform", "linux"):
            resolved = resolve_python_executable(gui=True)
            self.assertEqual(resolved, Path(sys.executable).resolve())

    def test_get_statusline_command_windows(self) -> None:
        with patch("sys.platform", "win32"):
            cmd = get_statusline_command(self.data_root)
            self.assertTrue(cmd.endswith("statusline.cmd") or cmd.endswith("STATUS~1.CMD"))
            self.assertNotIn('"', cmd)

    def test_get_statusline_command_posix(self) -> None:
        with patch("sys.platform", "linux"):
            cmd = get_statusline_command(self.data_root)
            expected = str((self.data_root / "bin" / "statusline").resolve())
            self.assertEqual(cmd, expected)

    def test_sync_all_profiles(self) -> None:
        p1 = self.store.create("acc1")
        p2 = self.store.create("acc2")
        p3 = self.store.create("acc3")

        synced = sync_all_profiles(self.store, enabled=True)
        self.assertEqual(sorted(synced), ["acc1", "acc2", "acc3"])

        status = get_statusline_status(self.store)
        self.assertTrue(status["installed"])
        for p in (p1, p2, p3):
            self.assertTrue(status["profiles"][p.name]["configured"])
            self.assertTrue(status["profiles"][p.name]["enabled"])

        # Disable all profiles
        synced_disabled = sync_all_profiles(self.store, enabled=False)
        self.assertEqual(len(synced_disabled), 3)

        status_disabled = get_statusline_status(self.store)
        for p in (p1, p2, p3):
            self.assertTrue(status_disabled["profiles"][p.name]["configured"])
            self.assertFalse(status_disabled["profiles"][p.name]["enabled"])

    def test_windows_script_path_and_install(self) -> None:
        with patch("sys.platform", "win32"):
            script_path = get_statusline_script_path(self.data_root)
            self.assertTrue(str(script_path).endswith("statusline.cmd"))
            installed = install_statusline_script(self.data_root)
            self.assertEqual(installed, script_path)
            self.assertTrue(installed.is_file())
            cmd_content = installed.read_text()
            self.assertIn("statusline.py", cmd_content)

            py_script = self.data_root / "bin" / "statusline.py"
            self.assertTrue(py_script.is_file())
            self.assertIn("from agym.statusline import main", py_script.read_text())

    def test_detect_profile_escape_roots_windows(self) -> None:
        from agym.profiles import _detect_profile_escape_roots
        win_profile_home = r"C:\Users\testuser\AppData\Local\agym\profiles\myprof\home"
        with patch.dict(os.environ, {"USERPROFILE": win_profile_home, "HOME": ""}, clear=True):
            with patch("platform.system", return_value="Windows"):
                cfg, data = _detect_profile_escape_roots()
                self.assertIsNotNone(cfg)
                self.assertIsNotNone(data)
                self.assertEqual(cfg, data)
                self.assertTrue(str(data).endswith("agym"))



class CliStatuslineCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.config_root = self.tmp_dir / "config"
        self.data_root = self.tmp_dir / "data"
        self.store = ProfileStore(config_root=self.config_root, data_root=self.data_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cli_statusline_preview(self) -> None:
        self.store.create("personal")
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = _statusline(["--preview", "personal"], self.store)
        self.assertEqual(code, 0)
        self.assertIn("Statusline preview for 'personal':", out.getvalue())
        self.assertIn("👤 personal", out.getvalue())

    def test_cli_statusline_sync(self) -> None:
        self.store.create("alpha")
        self.store.create("beta")
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = _statusline(["--sync"], self.store)
        self.assertEqual(code, 0)
        self.assertIn("Synchronized statusline across 2 profile(s)", out.getvalue())

    def test_cli_statusline_enable_disable(self) -> None:
        self.store.create("alpha")
        out = io.StringIO()
        with patch("sys.stdout", out):
            code_dis = _statusline(["--disable"], self.store)
        self.assertEqual(code_dis, 0)
        self.assertIn("Disabled statusline across 1 profile(s)", out.getvalue())

        out2 = io.StringIO()
        with patch("sys.stdout", out2):
            code_en = _statusline(["--enable"], self.store)
        self.assertEqual(code_en, 0)
        self.assertIn("Enabled statusline across 1 profile(s)", out2.getvalue())

    def test_cli_statusline_status_empty(self) -> None:
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = _statusline(["--status"], self.store)
        self.assertEqual(code, 0)
        self.assertIn("No profiles configured", out.getvalue())

    def test_cli_main_dispatch_statusline(self) -> None:
        with patch("agym.cli.ProfileStore", return_value=self.store):
            out = io.StringIO()
            with patch("sys.stdout", out):
                code = cli_main(["statusline", "--status"])
            self.assertEqual(code, 0)
            self.assertIn("Runner script:", out.getvalue())

    def test_statusline_main_reads_stdin(self) -> None:
        payload = {
            "model": {"display_name": "Flash 2.5"},
            "quota": {"gemini-5h": {"remaining_fraction": 0.8}},
        }
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            with patch.dict(os.environ, {"AGYM_PROFILE": "mock_account", "NO_COLOR": "1"}):
                code = statusline_main()
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("👤 mock_account", output)
        self.assertIn("5h: 80%", output)

    def test_statusline_main_exception_safe_fallback(self) -> None:
        stdin = io.StringIO("")
        stdout = io.StringIO()
        with patch("agym.statusline.render_statusline", side_effect=RuntimeError("unexpected crash")):
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
                with patch.dict(os.environ, {"AGYM_PROFILE": "safe_profile"}):
                    code = statusline_main()
        self.assertEqual(code, 0)
        self.assertIn("[safe_profile]", stdout.getvalue())


class SetupStatuslineIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.config_root = self.tmp_dir / "config"
        self.data_root = self.tmp_dir / "data"
        self.store = ProfileStore(config_root=self.config_root, data_root=self.data_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("agym.cli.resolve_agy")
    @patch("agym.cli.run_agy", return_value=0)
    @patch("agym.cli.persistent_profile_data_exists", return_value=True)
    def test_setup_configures_statusline_across_all_accounts(
        self,
        mock_data_exists: MagicMock,
        mock_run_agy: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_resolve.return_value = Path("/usr/bin/agy")

        # Create an existing profile first
        existing_profile = self.store.create("existing_acc")
        # Run setup for a new profile
        from agym.cli import _setup
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = _setup(["new_acc", "-s", "2027-01-01"], self.store)
        self.assertEqual(code, 0)
        self.assertIn("Statusline configured for all registered accounts (2 profiles)", out.getvalue())

        # Verify settings.json for both profiles
        for p in (existing_profile, self.store.get("new_acc")):
            settings_file = get_profile_settings_path(p.home)
            self.assertTrue(settings_file.is_file())
            data = json.loads(settings_file.read_text())
            self.assertIn("statusLine", data)
            self.assertEqual(data["statusLine"]["type"], "command")
            self.assertTrue(data["statusLine"]["enabled"])


class StatuslineVCSTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_parse_git_head(self) -> None:
        self.assertEqual(parse_git_head("ref: refs/heads/main\n"), "main")
        self.assertEqual(parse_git_head("ref: refs/heads/feat/statusline\n"), "feat/statusline")
        self.assertEqual(parse_git_head("ref: refs/tags/v1.0.0"), "v1.0.0")
        self.assertEqual(parse_git_head("ref: refs/remotes/origin/main"), "origin/main")
        self.assertEqual(parse_git_head("ref: custom/ref"), "custom/ref")
        self.assertEqual(parse_git_head("d3b07384d113edec49eaa6238ad5ff00\n"), "d3b0738")
        self.assertIsNone(parse_git_head(None))
        self.assertIsNone(parse_git_head(""))
        self.assertIsNone(parse_git_head("   \n"))

    def test_format_vcs_tag(self) -> None:
        vcs_wt = VCSInfo(branch="feat/statusline", worktree="statusline", directory="wt_dir")
        formatted_color = format_vcs_tag(vcs_wt, include_worktree=True, no_color=False)
        self.assertIn("🌳 🌿 feat/statusline", formatted_color)
        self.assertIn("\033[38;5;75m", formatted_color)

        formatted_no_color = format_vcs_tag(vcs_wt, include_worktree=True, no_color=True)
        self.assertEqual(formatted_no_color, "🌳 🌿 feat/statusline")

        formatted_no_wt = format_vcs_tag(vcs_wt, include_worktree=False, no_color=True)
        self.assertEqual(formatted_no_wt, "🌿 feat/statusline")

        # Normal repository with directory
        vcs_repo = VCSInfo(branch="main", directory="my-project")
        self.assertEqual(format_vcs_tag(vcs_repo, no_color=True), "my-project 🌿 main")
        formatted_repo_color = format_vcs_tag(vcs_repo, no_color=False)
        self.assertIn("my-project", formatted_repo_color)
        self.assertIn("🌿 main", formatted_repo_color)

        # Directory truncation
        long_dir = VCSInfo(branch="main", directory="very-long-project-folder")
        self.assertEqual(format_vcs_tag(long_dir, max_dir_len=14, no_color=True), "very-long-pro… 🌿 main")

        # Directory disabled
        self.assertEqual(format_vcs_tag(vcs_repo, include_directory=False, no_color=True), "🌿 main")

        # None inputs
        self.assertIsNone(format_vcs_tag(None))
        self.assertIsNone(format_vcs_tag(VCSInfo()))

        # Branch Truncation
        long_vcs = VCSInfo(branch="feature/very-long-branch-name", worktree=None)
        truncated = format_vcs_tag(long_vcs, include_worktree=False, max_branch_len=14, no_color=True)
        self.assertEqual(truncated, "🌿 feature/very-…")

    def test_resolve_git_vcs_standard_repo(self) -> None:
        repo_dir = self.tmp_dir / "standard_repo"
        git_dir = repo_dir / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

        vcs = resolve_git_vcs(cwd=repo_dir)
        self.assertEqual(vcs.branch, "main")
        self.assertIsNone(vcs.worktree)
        self.assertEqual(vcs.directory, "standard_repo")

    def test_resolve_git_vcs_linked_worktree(self) -> None:
        main_git = self.tmp_dir / "main_repo" / ".git"
        wt_meta = main_git / "worktrees" / "my-wt"
        wt_meta.mkdir(parents=True)
        (wt_meta / "commondir").write_text("../..\n")
        (wt_meta / "gitdir").write_text(str(self.tmp_dir / "wt_work" / ".git") + "\n")
        (wt_meta / "HEAD").write_text("ref: refs/heads/feat/test-wt\n")

        wt_dir = self.tmp_dir / "wt_work"
        wt_dir.mkdir(parents=True)
        (wt_dir / ".git").write_text(f"gitdir: {wt_meta}\n")

        vcs = resolve_git_vcs(cwd=wt_dir)
        self.assertEqual(vcs.branch, "feat/test-wt")
        self.assertEqual(vcs.worktree, "my-wt")

    def test_resolve_git_vcs_relative_gitdir(self) -> None:
        main_git = self.tmp_dir / "main_repo" / ".git"
        wt_meta = main_git / "worktrees" / "wt-rel"
        wt_meta.mkdir(parents=True)
        (wt_meta / "commondir").write_text("../..\n")
        (wt_meta / "HEAD").write_text("ref: refs/heads/feat/rel\n")

        wt_dir = self.tmp_dir / "wt_rel_work"
        wt_dir.mkdir(parents=True)
        rel_path = os.path.relpath(wt_meta, wt_dir)
        (wt_dir / ".git").write_text(f"gitdir: {rel_path}\n")

        vcs = resolve_git_vcs(cwd=wt_dir)
        self.assertEqual(vcs.branch, "feat/rel")
        self.assertEqual(vcs.worktree, "wt-rel")

    def test_resolve_git_vcs_from_subdir(self) -> None:
        main_git = self.tmp_dir / "main_repo" / ".git"
        wt_meta = main_git / "worktrees" / "nested-wt"
        wt_meta.mkdir(parents=True)
        (wt_meta / "commondir").write_text("../..\n")
        (wt_meta / "HEAD").write_text("ref: refs/heads/feat/nested\n")

        wt_dir = self.tmp_dir / "nested_work"
        (wt_dir / ".git").parent.mkdir(parents=True, exist_ok=True)
        (wt_dir / ".git").write_text(f"gitdir: {wt_meta}\n")

        subdir = wt_dir / "src" / "package" / "deep"
        subdir.mkdir(parents=True)

        vcs = resolve_git_vcs(cwd=subdir)
        self.assertEqual(vcs.branch, "feat/nested")
        self.assertEqual(vcs.worktree, "nested-wt")

    def test_resolve_git_vcs_submodule(self) -> None:
        sub_meta = self.tmp_dir / "main_repo" / ".git" / "modules" / "submodule1"
        sub_meta.mkdir(parents=True)
        (sub_meta / "HEAD").write_text("ref: refs/heads/sub-branch\n")

        sub_dir = self.tmp_dir / "sub_work"
        sub_dir.mkdir(parents=True)
        (sub_dir / ".git").write_text(f"gitdir: {sub_meta}\n")

        vcs = resolve_git_vcs(cwd=sub_dir)
        self.assertEqual(vcs.branch, "sub-branch")
        self.assertIsNone(vcs.worktree)

    def test_resolve_git_vcs_non_git_dir(self) -> None:
        empty_dir = self.tmp_dir / "empty"
        empty_dir.mkdir()
        vcs = resolve_git_vcs(cwd=empty_dir)
        self.assertIsNone(vcs.branch)
        self.assertIsNone(vcs.worktree)

    def test_resolve_git_vcs_payload_metadata(self) -> None:
        payload = {
            "vcs": {"branch": "payload-branch", "worktree": "payload-wt"},
        }
        vcs = resolve_git_vcs(payload=payload)
        self.assertEqual(vcs.branch, "payload-branch")
        self.assertEqual(vcs.worktree, "payload-wt")

    def test_render_statusline_vcs_integration(self) -> None:
        wt_meta = self.tmp_dir / "repo" / ".git" / "worktrees" / "my-feature-wt"
        wt_meta.mkdir(parents=True)
        (wt_meta / "commondir").write_text("../..\n")
        (wt_meta / "HEAD").write_text("ref: refs/heads/feat/statusline\n")

        wt_dir = self.tmp_dir / "feat_wt"
        wt_dir.mkdir(parents=True)
        (wt_dir / ".git").write_text(f"gitdir: {wt_meta}\n")

        payload = {
            "model": {"display_name": "Flash 2.5"},
            "quota": {"gemini-5h": {"remaining_fraction": 0.85}},
        }

        # Wide terminal in worktree: includes worktree icon
        wide_line = render_statusline(
            payload,
            profile_name="tiagoliv",
            terminal_width=110,
            no_color=True,
            cwd=wt_dir,
        )
        self.assertIn("👤 tiagoliv", wide_line)
        self.assertIn("🌳 🌿 feat/statusline", wide_line)
        self.assertIn("5h: [█████░] 85%", wide_line)

        # Standard repository (non-worktree): includes current directory before branch
        repo_dir = self.tmp_dir / "standard_repo"
        git_dir = repo_dir / ".git"
        git_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        repo_line = render_statusline(
            payload,
            profile_name="tiagoliv",
            terminal_width=110,
            no_color=True,
            cwd=repo_dir,
        )
        self.assertIn("👤 tiagoliv", repo_line)
        self.assertIn("standard_repo 🌿 main", repo_line)

        # Narrower terminal (70 columns): worktree icon still included
        narrow_line = render_statusline(
            payload,
            profile_name="tiagoliv",
            terminal_width=70,
            no_color=True,
            cwd=wt_dir,
        )
        self.assertIn("👤 tiagoliv", narrow_line)
        self.assertIn("🌳 🌿 feat/statusline", narrow_line)

        # Ultra compact terminal (50 columns): omits vcs tag
        ultra_line = render_statusline(
            payload,
            profile_name="tiagoliv",
            terminal_width=50,
            no_color=True,
            cwd=wt_dir,
        )
        self.assertIn("👤 tiagoliv", ultra_line)
        self.assertNotIn("🌿", ultra_line)


if __name__ == "__main__":
    unittest.main()

