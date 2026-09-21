from __future__ import annotations

import os
os.environ["AGYM_DISABLE_WINCRED"] = "1"
import tempfile
import unittest
from pathlib import Path
import io
from unittest import mock

from agym import cli
from agym.profiles import ProfileStore


class CliTests(unittest.TestCase):
    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    def test_profile_launch_resolves_before_launch_env(self, run: mock.Mock, resolve: mock.Mock, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            profile = store.create("personal")
            Store.return_value = store
            resolve.return_value = Path("/real/agy")
            run.return_value = 0
            self.assertEqual(cli.main(["personal", "-p", "hello"]), 0)
            resolve.assert_called_once_with()
            run.assert_called_once_with(Path("/real/agy"), profile, ["-p", "hello"], replace_process=True)

    @mock.patch("agym.cli.ProfileStore")
    def test_config_display_and_mutation(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("personal")
            Store.return_value = store

            # 1. Initial config display
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["config", "personal"])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("profile: personal", text)
            self.assertIn("model: default", text)
            self.assertIn("dangerously-skip-permissions: false", text)

            # 2. Set model
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["config", "personal", "--model", "gemini-2.5-pro"])
            self.assertEqual(code, 0)
            self.assertIn("model: gemini-2.5-pro", out.getvalue())
            self.assertEqual(store.get("personal").settings.model, "gemini-2.5-pro")

            # 3. Enable dangerously-skip-permissions
            out = io.StringIO()
            err = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                code = cli.main(["config", "personal", "--dangerously-skip-permissions"])
            self.assertEqual(code, 0)
            self.assertIn("dangerously-skip-permissions: true", out.getvalue())
            self.assertIn("warning: --dangerously-skip-permissions is enabled", out.getvalue())
            self.assertTrue(store.get("personal").settings.dangerously_skip_permissions)

            # 4. Disable dangerously-skip-permissions
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["config", "personal", "--no-dangerously-skip-permissions"])
            self.assertEqual(code, 0)
            self.assertIn("dangerously-skip-permissions: false", out.getvalue())
            self.assertFalse(store.get("personal").settings.dangerously_skip_permissions)

            # 4a. Verify aliases: -y, --yes, --dsp, --skip-perms, --dangerously-skip-permission
            for alias in ["-y", "--yes", "--dsp", "--skip-perms", "--dangerously-skip-permission"]:
                out = io.StringIO()
                with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
                    code = cli.main(["config", "personal", alias])
                self.assertEqual(code, 0, f"Failed for alias {alias}")
                self.assertTrue(store.get("personal").settings.dangerously_skip_permissions, f"Failed for {alias}")

                # Disable using a negative alias
                with mock.patch("sys.stdout", io.StringIO()):
                    code = cli.main(["config", "personal", "--no-dsp"])
                self.assertEqual(code, 0)
                self.assertFalse(store.get("personal").settings.dangerously_skip_permissions)

            # 4b. Verify negative aliases: --no-skip-perms, --no-dangerously-skip-permission
            for neg_alias in ["--no-skip-perms", "--no-dangerously-skip-permission"]:
                # Enable first
                with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                    cli.main(["config", "personal", "-y"])
                self.assertTrue(store.get("personal").settings.dangerously_skip_permissions)
                out = io.StringIO()
                with mock.patch("sys.stdout", out):
                    code = cli.main(["config", "personal", neg_alias])
                self.assertEqual(code, 0)
                self.assertFalse(store.get("personal").settings.dangerously_skip_permissions)

            # 5. Reset model with 'default'
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["config", "personal", "--model", "default"])
            self.assertEqual(code, 0)
            self.assertIn("model: default", out.getvalue())
            self.assertIsNone(store.get("personal").settings.model)

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_auto_prompt")
    @mock.patch("agym.cli.run_agy")
    def test_auto_prompt_dispatch(
        self,
        mock_run_agy: mock.Mock,
        mock_run_auto_prompt: mock.Mock,
        mock_resolve: mock.Mock,
        Store: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            profile = store.create("personal")
            Store.return_value = store
            mock_resolve.return_value = Path("/usr/bin/agy")
            mock_run_auto_prompt.return_value = 0
            mock_run_agy.return_value = 0

            # 1. agym personal --auto-prompt "make a plan"
            code = cli.main(["personal", "--auto-prompt", "make a plan"])
            self.assertEqual(code, 0)
            mock_run_auto_prompt.assert_called_once_with(
                Path("/usr/bin/agy"), profile, "make a plan", replace_process=True
            )
            mock_run_agy.assert_not_called()

            mock_run_auto_prompt.reset_mock()
            mock_run_agy.reset_mock()

            # 2. agym personal --auto-prompt="make a plan"
            code = cli.main(["personal", "--auto-prompt=make a plan"])
            self.assertEqual(code, 0)
            mock_run_auto_prompt.assert_called_once_with(
                Path("/usr/bin/agy"), profile, "make a plan", replace_process=True
            )
            mock_run_agy.assert_not_called()

            mock_run_auto_prompt.reset_mock()
            mock_run_agy.reset_mock()

            # 3. Normal passthrough still routes to run_agy
            code = cli.main(["personal", "-p", "review"])
            self.assertEqual(code, 0)
            mock_run_agy.assert_called_once_with(
                Path("/usr/bin/agy"), profile, ["-p", "review"], replace_process=True
            )
            mock_run_auto_prompt.assert_not_called()

            mock_run_auto_prompt.reset_mock()
            mock_run_agy.reset_mock()

            # 4. Explicit -- passthrough with --auto-prompt forwards to agy
            code = cli.main(["personal", "--", "--auto-prompt", "something"])
            self.assertEqual(code, 0)
            mock_run_agy.assert_called_once_with(
                Path("/usr/bin/agy"), profile, ["--", "--auto-prompt", "something"], replace_process=True
            )
            mock_run_auto_prompt.assert_not_called()

            # 5. Permission aliases with --auto-prompt forward to run_auto_prompt
            mock_run_auto_prompt.reset_mock()
            mock_run_agy.reset_mock()
            code = cli.main(["personal", "--auto-prompt", "make a plan", "-y"])
            self.assertEqual(code, 0)
            mock_run_auto_prompt.assert_called_once_with(
                Path("/usr/bin/agy"), profile, "make a plan", replace_process=True, extra_args=["-y"]
            )
            mock_run_agy.assert_not_called()

    @mock.patch("agym.cli.ProfileStore")
    def test_auto_prompt_errors(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("personal")
            Store.return_value = store

            # Missing argument for --auto-prompt
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["personal", "--auto-prompt"])
            self.assertEqual(code, 2)
            self.assertIn("--auto-prompt requires a prompt argument", err.getvalue())

            # Unexpected trailing arguments
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["personal", "--auto-prompt", "plan", "extra"])
            self.assertEqual(code, 2)
            self.assertIn("unexpected arguments with --auto-prompt", err.getvalue())

    @mock.patch("agym.cli.ProfileStore")
    def test_list_command(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("personal")
            Store.return_value = store

            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["list"])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("personal", text)
            self.assertIn("model=default", text)
            self.assertIn("permissions=normal", text)
            self.assertNotIn("created with", text)

    @mock.patch("agym.cli.ProfileStore")
    def test_list_command_does_not_print_version_even_if_in_legacy_config(self, Store: mock.Mock) -> None:
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("personal")
            raw = store._load()
            raw["profiles"]["personal"]["agy_version"] = "1.2.7"
            store._save(raw)
            Store.return_value = store

            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["list"])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertNotIn("created with", text)
            self.assertNotIn("1.2.7", text)
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    @mock.patch("agym.cli.persistent_profile_data_exists", return_value=True)
    @mock.patch("agym.cli.ProfileStore")
    def test_setup_with_subscription_date(
        self,
        Store: mock.Mock,
        _exists: mock.Mock,
        run: mock.Mock,
        resolve: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            Store.return_value = store
            resolve.return_value = Path("/real/agy")
            run.return_value = 0

            code = cli.main(["setup", "my-prof", "--subscription-date", "14/03/2027"])
            self.assertEqual(code, 0)
            p = store.get("my-prof")
            self.assertEqual(p.subscription_date, "2027-03-14")
            run.assert_called_with(Path("/real/agy"), p, replace_process=False, is_setup=True)

            # Setting up an existing profile raises error
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code_err = cli.main(["setup", "my-prof"])
            self.assertEqual(code_err, 2)
            self.assertIn("profile already exists", err.getvalue())

    @mock.patch("agym.cli.ProfileStore")
    def test_edit_subscription_date(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("test-prof")
            Store.return_value = store

            # Update with flag
            code = cli.main(["edit", "test-prof", "--subscription-date", "15/04/2027"])
            self.assertEqual(code, 0)
            self.assertEqual(store.get("test-prof").subscription_date, "2027-04-15")

            # Clear with flag
            code = cli.main(["edit", "test-prof", "--clear-subscription-date"])
            self.assertEqual(code, 0)
            self.assertIsNone(store.get("test-prof").subscription_date)

            # Invalid date flag returns error 2
            code = cli.main(["edit", "test-prof", "--subscription-date", "31/02/2027"])
            self.assertEqual(code, 2)

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.prompt_subscription_date")
    def test_edit_interactive(self, mock_prompt: mock.Mock, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("test-prof", subscription_date="2027-03-14")
            Store.return_value = store

            with mock.patch("sys.stdin.isatty", return_value=True):
                mock_prompt.return_value = "2028-05-20"
                code = cli.main(["edit", "test-prof"])
                self.assertEqual(code, 0)
                self.assertEqual(store.get("test-prof").subscription_date, "2028-05-20")

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.persistent_profile_data_exists", return_value=True)
    def test_list_formatting(self, _exists: mock.Mock, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("p-with-date", subscription_date="2030-01-01")
            store.create("p-no-date")
            Store.return_value = store

            import io
            from unittest.mock import patch

            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = cli.main(["list"])
                self.assertEqual(code, 0)
                out = mock_out.getvalue()
                self.assertIn("p-with-date\tready; renews 01/01/2030", out)
                self.assertIn("p-no-date\tready; subscription: unknown", out)

    def test_help_output(self) -> None:
        import io
        from unittest.mock import patch

        for arg in ["--help", "-h", "help"]:
            with self.subTest(arg=arg), patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = cli.main([arg])
                self.assertEqual(code, 0)
                out = mock_out.getvalue()
                self.assertIn("agym — Explicit isolated-profile manager", out)
                self.assertIn("Commands:", out)
                self.assertIn("Launching Antigravity:", out)
                self.assertIn("Command Options:", out)
                self.assertIn("Examples:", out)

        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = cli.main([])
            self.assertEqual(code, 2)
            out = mock_out.getvalue()
            self.assertIn("agym — Explicit isolated-profile manager", out)

    @mock.patch("agym.cli.ProfileStore")
    def test_rotate_simulate(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("acc1")
            store.create("acc2")
            Store.return_value = store

            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["rotate", "--simulate", "3"])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("Simulating 3 account rotations across 2 accounts:", text)
            self.assertIn("Active Account: acc1", text)
            self.assertIn("Active Account: acc2", text)
            self.assertIn("Rotation Index: 1/2", text)
            self.assertIn("Rotation Index: 2/2", text)

    @mock.patch("agym.cli.ProfileStore")
    def test_rotate_status_and_reset(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("user_alpha")
            Store.return_value = store

            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["rotate", "--status"])
            self.assertEqual(code, 0)
            self.assertIn("Total accounts: 1", out.getvalue())
            self.assertIn("Accounts: user_alpha", out.getvalue())

            out_reset = io.StringIO()
            with mock.patch("sys.stdout", out_reset):
                code_reset = cli.main(["rotate", "--reset"])
            self.assertEqual(code_reset, 0)
            self.assertIn("Rotation state reset", out_reset.getvalue())

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    def test_rotate_launch(self, run: mock.Mock, resolve: mock.Mock, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p1 = store.create("p1")
            p2 = store.create("p2")
            Store.return_value = store
            resolve.return_value = Path("/real/agy")
            run.return_value = 0

            # 1st rotate -> launches p1
            out1 = io.StringIO()
            with mock.patch("sys.stdout", out1):
                code1 = cli.main(["rotate", "-p", "step 1"])
            self.assertEqual(code1, 0)
            self.assertIn("Active Account: p1", out1.getvalue())
            run.assert_called_with(Path("/real/agy"), p1, ["-p", "step 1"], replace_process=True)

            # 2nd rotate -> launches p2
            out2 = io.StringIO()
            with mock.patch("sys.stdout", out2):
                code2 = cli.main(["rotate", "-p", "step 2"])
            self.assertEqual(code2, 0)
            self.assertIn("Active Account: p2", out2.getvalue())
            run.assert_called_with(Path("/real/agy"), p2, ["-p", "step 2"], replace_process=True)

