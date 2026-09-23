from __future__ import annotations

import io
import json
import os
os.environ["AGYM_DISABLE_WINCRED"] = "1"
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym import cli
from agym.profiles import ProfileStore


class HelpTests(unittest.TestCase):
    def test_main_help_flags(self) -> None:
        """agym, agym help, agym -h, and agym --help show compact main help without individual flags."""
        expected_sections = [
            "Accounts",
            "setup       Create a profile",
            "list        List profiles",
            "edit        Edit profile settings",
            "config      Configure profile defaults",
            "rename      Rename a profile",
            "remove      Delete a profile",
            "Usage & Monitoring",
            "usage       Show quota usage",
            "tokens      Show token usage",
            "statusline  Manage statusline",
            "Launching",
            "select      Pick an account",
            "rotate      Rotate accounts",
            "<profile>   Launch Antigravity",
            "Tools",
            "doctor",
            "auto-pr",
            "update",
            "integration",
            "Run `agym help <command>` for details.",
        ]

        for arg in ["--help", "-h", "help"]:
            with self.subTest(arg=arg), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main([arg])
                self.assertEqual(code, 0)
                output = out.getvalue()
                for section in expected_sections:
                    self.assertIn(section, output)
                # Ensure detailed flags are not present in main help
                self.assertNotIn("--json", output)
                self.assertNotIn("--claude", output)
                self.assertNotIn("--simulate", output)

        # Empty args returns exit code 2 and outputs main help
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = cli.main([])
            self.assertEqual(code, 2)
            output = out.getvalue()
            for section in expected_sections:
                self.assertIn(section, output)

    def test_command_help_usage_and_tokens(self) -> None:
        """agym help <cmd> and agym <cmd> --help work and display clean syntax and argparse reference."""
        # usage
        for cmd_args in [["help", "usage"], ["usage", "--help"], ["usage", "-h"]]:
            with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(cmd_args)
                self.assertEqual(code, 0)
                output = out.getvalue()
                self.assertIn("usage [profiles...]", output)
                self.assertIn("--json", output)
                self.assertIn("-c", output)
                self.assertIn("--claude", output)
                self.assertIn("--sort", output)
                self.assertIn("--timeout", output)

        # tokens
        for cmd_args in [["help", "tokens"], ["tokens", "--help"], ["tokens", "-h"]]:
            with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(cmd_args)
                self.assertEqual(code, 0)
                output = out.getvalue()
                self.assertIn("tokens [profiles...]", output)
                self.assertIn("-b", output)
                self.assertIn("--breakdown", output)
                self.assertIn("--matrix", output)

    def test_aliases_help(self) -> None:
        """Aliases resolve to the canonical command help."""
        # pick -> select
        for cmd_args in [["help", "pick"], ["pick", "--help"]]:
            with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(cmd_args)
                self.assertEqual(code, 0)
                output = out.getvalue()
                self.assertIn("select", output)
                self.assertIn("-f", output)
                self.assertIn("--fresh", output)

        # mv -> rename
        for cmd_args in [["help", "mv"], ["mv", "--help"]]:
            with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(cmd_args)
                self.assertEqual(code, 0)
                output = out.getvalue()
                self.assertIn("rename <profile> <new-name>", output)

        # token / token-usage -> tokens
        for alias in ["token", "token-usage"]:
            for cmd_args in [["help", alias], [alias, "--help"]]:
                with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    code = cli.main(cmd_args)
                    self.assertEqual(code, 0)
                    output = out.getvalue()
                    self.assertIn("tokens [profiles...]", output)

    def test_launch_help(self) -> None:
        """agym help launch and agym launch --help show launch and passthrough documentation."""
        for cmd_args in [["help", "launch"], ["launch", "--help"], ["help", "<profile>"]]:
            with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(cmd_args)
                self.assertEqual(code, 0)
                output = out.getvalue()
                self.assertIn("agym <profile> [agy args...]", output)
                self.assertIn("--auto-prompt", output)
                self.assertIn("--auto-pr", output)

    def test_integration_help_hierarchy(self) -> None:
        """agym help integration, agym integration --help, and nested subcommands."""
        # Root integration help
        for cmd_args in [["help", "integration"], ["integration", "--help"], ["integration", "help"]]:
            with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(cmd_args)
                self.assertEqual(code, 0)
                output = out.getvalue()
                self.assertIn("integration", output)
                self.assertIn("├── info", output)
                self.assertIn("├── profiles", output)
                self.assertIn("├── usage", output)
                self.assertIn("├── run", output)
                self.assertIn("└── lease", output)
                self.assertIn("integration info", output)
                self.assertIn("integration run start", output)

        # integration run help
        for cmd_args in [["help", "integration", "run"], ["integration", "run", "--help"]]:
            with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(cmd_args)
                self.assertEqual(code, 0)
                output = out.getvalue()
                self.assertIn("integration run", output)
                self.assertIn("├── start", output)
                self.assertIn("├── get", output)
                self.assertIn("├── list", output)
                self.assertIn("├── stop", output)
                self.assertIn("└── events", output)
                self.assertIn("integration run start", output)
                self.assertIn("--request-json -", output)

        # integration run events help
        for cmd_args in [["help", "integration", "run", "events"], ["integration", "run", "events", "--help"]]:
            with self.subTest(cmd_args=cmd_args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(cmd_args)
                self.assertEqual(code, 0)
                output = out.getvalue()
                self.assertIn("integration run events", output)
                self.assertIn("--after N", output)
                self.assertIn("--limit N", output)
                self.assertIn("--follow", output)
                self.assertIn("--ndjson", output)

    def test_all_registered_commands_help(self) -> None:
        """Verify every registered command responds to help <cmd> and <cmd> --help."""
        commands = [
            "setup", "config", "edit", "rename", "list", "select",
            "rotate", "usage", "tokens", "statusline", "remove",
            "doctor", "auto-pr", "update", "integration",
        ]
        for cmd in commands:
            with self.subTest(cmd=cmd, mode="agym help <cmd>"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cli.main(["help", cmd]), 0)
                self.assertTrue(len(out.getvalue()) > 0)

            with self.subTest(cmd=cmd, mode="agym <cmd> --help"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cli.main([cmd, "--help"]), 0)
                self.assertTrue(len(out.getvalue()) > 0)

    def test_unknown_command_help(self) -> None:
        """agym help <unknown> prints an error and returns 2."""
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli.main(["help", "nonexistent"])
            self.assertEqual(code, 2)
            self.assertIn("unknown command 'nonexistent'", err.getvalue())

        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli.main(["help", "integration", "unknown_sub"])
            self.assertEqual(code, 2)
            self.assertIn("unknown integration command", err.getvalue())

    def test_help_does_no_network_or_api_work(self) -> None:
        """Help commands must be zero-cost, offline, with no network/API/updater calls."""
        with mock.patch("agym.cli.maybe_prompt_startup_update", side_effect=AssertionError("startup update called")), \
             mock.patch("agym.updater.check_for_updates", side_effect=AssertionError("updater check called")), \
             mock.patch("agym.cli.resolve_agy", side_effect=AssertionError("resolve_agy called")), \
             mock.patch("agym.usage.run_usage", side_effect=AssertionError("run_usage called")), \
             mock.patch("agym.tokens.run_tokens", side_effect=AssertionError("run_tokens called")), \
             mock.patch("sys.stdout", new_callable=io.StringIO):

            # Test various help invocations
            self.assertEqual(cli.main(["--help"]), 0)
            self.assertEqual(cli.main(["help"]), 0)
            self.assertEqual(cli.main(["help", "usage"]), 0)
            self.assertEqual(cli.main(["usage", "--help"]), 0)
            self.assertEqual(cli.main(["help", "tokens"]), 0)
            self.assertEqual(cli.main(["tokens", "--help"]), 0)
            self.assertEqual(cli.main(["help", "update"]), 0)
            self.assertEqual(cli.main(["update", "--help"]), 0)
            self.assertEqual(cli.main(["help", "integration"]), 0)
            self.assertEqual(cli.main(["integration", "--help"]), 0)
            self.assertEqual(cli.main(["help", "integration", "run", "events"]), 0)
            self.assertEqual(cli.main(["integration", "run", "events", "--help"]), 0)
            self.assertEqual(cli.main(["help", "launch"]), 0)

    def test_integration_machine_output_stays_json(self) -> None:
        """Normal integration execution must remain strictly JSON/machine-safe without banners."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"AGYM_DATA_HOME": tmp}):
            # Valid command
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(["integration", "info", "--json"])
                self.assertEqual(code, 0)
                data = json.loads(out.getvalue())
                self.assertTrue(data["ok"])
                self.assertEqual(data["protocol"]["major"], 1)

            # Error command
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(["integration", "profiles", "--protocol", "99", "--json"])
                self.assertEqual(code, 2)
                data = json.loads(out.getvalue())
                self.assertFalse(data["ok"])
                self.assertEqual(data["error"]["code"], "UNSUPPORTED_PROTOCOL")

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    def test_profile_passthrough_help(self, run: mock.Mock, resolve: mock.Mock, Store: mock.Mock) -> None:
        """agym <profile> --help must pass through to Antigravity, not show AGYM help."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            profile = store.create("myprofile")
            Store.return_value = store
            resolve.return_value = Path("/fake/agy")
            run.return_value = 0

            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main(["myprofile", "--help"])
                self.assertEqual(code, 0)
                # Ensure no AGYM help menu was printed
                self.assertNotIn("Accounts", out.getvalue())
                # Ensure it called run_agy with --help
                resolve.assert_called_once_with()
                run.assert_called_once_with(Path("/fake/agy"), profile, ["--help"], replace_process=True)

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    @mock.patch("agym.cli.fetch_and_cache_usage")
    @mock.patch("agym.cli.run_picker")
    def test_double_dash_separator_help_passthrough(self, picker: mock.Mock, fetch: mock.Mock, run: mock.Mock, resolve: mock.Mock, Store: mock.Mock) -> None:
        """agym select -- --help passes --help after -- to Antigravity."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            profile = store.create("acc1")
            Store.return_value = store
            resolve.return_value = Path("/fake/agy")
            run.return_value = 0
            fetch.return_value = []
            picker.return_value = "acc1"

            with mock.patch("sys.stdout", new_callable=io.StringIO):
                code = cli.main(["select", "--", "--help"])
                self.assertEqual(code, 0)
                run.assert_called_once_with(Path("/fake/agy"), profile, ["--help"], replace_process=True)

    def test_version_flags(self) -> None:
        """agym -v and agym --version output version and exit 0."""
        from agym import __version__
        for flag in ["-v", "--version"]:
            with self.subTest(flag=flag), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = cli.main([flag])
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), __version__)


if __name__ == "__main__":
    unittest.main()
