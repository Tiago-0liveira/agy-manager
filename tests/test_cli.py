from __future__ import annotations

import os
os.environ["AGYM_DISABLE_WINCRED"] = "1"
os.environ["AGYM_NO_UPDATE_CHECK"] = "1"
import tempfile
import unittest
from pathlib import Path
import io
from unittest import mock

from agym import cli
from agym.profiles import ProfileStore
from agym.orchestration.contracts import (
    BudgetUsage,
    OrchestrationBudget,
    RunId,
    RunMode,
    RunState,
    RunStatus,
)
from agym.orchestration.engine import DryRunPlan
from agym.orchestration.wiring import OrchestrationDependencies


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

    @mock.patch("agym.cli.resolve_agy", return_value=Path("/usr/bin/agy"))
    @mock.patch("agym.cli.ProfileStore")
    def test_auto_prompt_errors(self, Store: mock.Mock, _resolve: mock.Mock) -> None:
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
                self.assertIn("Accounts", out)
                self.assertIn("Usage & Monitoring", out)
                self.assertIn("Launching", out)
                self.assertIn("Tools", out)
                self.assertIn("Run `agym help <command>` for details.", out)

        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = cli.main([])
            self.assertEqual(code, 2)
            out = mock_out.getvalue()
            self.assertIn("Accounts", out)
            self.assertIn("Usage & Monitoring", out)
            self.assertIn("Launching", out)
            self.assertIn("Tools", out)
            self.assertIn("Run `agym help <command>` for details.", out)

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

    @mock.patch("agym.cli.ProfileStore")
    def test_rename_command(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("personal")
            Store.return_value = store

            # Successful rename
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["rename", "personal", "AI1"])
            self.assertEqual(code, 0)
            self.assertIn("Renamed profile 'personal' to 'AI1'.", out.getvalue())
            self.assertEqual(store.get("AI1").name, "AI1")

            # Successful rename with alias 'mv'
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["mv", "AI1", "AI2"])
            self.assertEqual(code, 0)
            self.assertIn("Renamed profile 'AI1' to 'AI2'.", out.getvalue())
            self.assertEqual(store.get("AI2").name, "AI2")

            # Nonexistent profile
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["rename", "nonexistent", "target"])
            self.assertEqual(code, 2)
            self.assertIn("profile not found: nonexistent", err.getvalue())

            # Already existing target
            store.create("target")
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["rename", "AI2", "target"])
            self.assertEqual(code, 2)
            self.assertIn("profile already exists: target", err.getvalue())

            # Renaming to same name
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["rename", "AI2", "AI2"])
            self.assertEqual(code, 2)
            self.assertIn("cannot rename profile to the same name: 'AI2'", err.getvalue())

    @mock.patch("agym.cli.ProfileStore")
    def test_edit_command_rename(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("personal")
            Store.return_value = store

            # Rename using --name
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["edit", "personal", "--name", "AI1"])
            self.assertEqual(code, 0)
            self.assertIn("Renamed profile 'personal' to 'AI1'.", out.getvalue())
            self.assertEqual(store.get("AI1").name, "AI1")

            # Rename using --rename and update subscription date
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["edit", "AI1", "--rename", "AI2", "-s", "14/03/2027"])
            self.assertEqual(code, 0)
            self.assertIn("Renamed profile 'AI1' to 'AI2'.", out.getvalue())
            self.assertIn("Updated subscription date for profile 'AI2' to 14/03/2027.", out.getvalue())
            p = store.get("AI2")
            self.assertEqual(p.name, "AI2")
            self.assertEqual(p.subscription_date, "2027-03-14")


class OrchestrateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(self.root / "config", self.root / "data")
        self.store.create("test-prof")

        self.mock_engine = mock.MagicMock()
        self.mock_store = mock.MagicMock()
        self.mock_runner = mock.MagicMock()
        self.mock_coord = mock.MagicMock()
        self.mock_sink = mock.MagicMock()
        self.mock_sched = mock.MagicMock()
        self.mock_lease = mock.MagicMock()
        self.mock_cache = mock.MagicMock()

        self.deps = OrchestrationDependencies(
            profile_store=self.store,
            cache_manager=self.mock_cache,
            lease_manager=self.mock_lease,
            scheduler=self.mock_sched,
            run_store=self.mock_store,
            runner=self.mock_runner,
            coordinator=self.mock_coord,
            event_sink=self.mock_sink,
            engine=self.mock_engine,
            budget=OrchestrationBudget(),
        )

        self.default_state = RunState(
            run_id=RunId("run-test-1"),
            task="Default task",
            mode=RunMode.PLAN,
            status=RunStatus.COMPLETED,
            budget_usage=BudgetUsage(invocations=3, rounds=2, runtime_seconds=12.5),
            final_result="Plan created successfully",
        )
        self.mock_engine.run.return_value = self.default_state
        self.mock_engine.resume.return_value = self.default_state

        self.default_plan = DryRunPlan(
            run_id=RunId("dry-test-1"),
            task="Dry run task",
            mode=RunMode.PLAN,
            is_valid=True,
        )
        self.mock_engine.dry_run.return_value = self.default_plan

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @mock.patch("agym.cli.run_agy")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_dispatch(
        self,
        mock_build: mock.Mock,
        mock_resolve: mock.Mock,
        mock_run_agy: mock.Mock,
    ) -> None:
        mock_build.return_value = self.deps
        code = cli.main(["orchestrate", "Analyze architecture"])
        self.assertEqual(code, 0)
        self.mock_engine.run.assert_called_once_with("Analyze architecture", mode=RunMode.PLAN)
        mock_run_agy.assert_not_called()
        mock_resolve.assert_not_called()

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_task_parsing_default_plan(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        code = cli.main(["orchestrate", "Build auth module"])
        self.assertEqual(code, 0)
        self.mock_engine.run.assert_called_once_with("Build auth module", mode=RunMode.PLAN)

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_plan_mode_explicit(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        code = cli.main(["orchestrate", "Review tests", "--mode", "plan"])
        self.assertEqual(code, 0)
        self.mock_engine.run.assert_called_once_with("Review tests", mode=RunMode.PLAN)

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_implement_mode(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        code = cli.main(["orchestrate", "Implement feature X", "--mode", "implement"])
        self.assertEqual(code, 0)
        self.mock_engine.run.assert_called_once_with("Implement feature X", mode=RunMode.IMPLEMENT)

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_dry_run(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["orchestrate", "Dry run task", "--dry-run"])
        self.assertEqual(code, 0)
        self.mock_engine.dry_run.assert_called_once_with("Dry run task", mode=RunMode.PLAN)
        self.mock_engine.run.assert_not_called()
        self.assertIn("Dry Run Plan", out.getvalue())

        # Test -n shortcut and implement mode
        self.mock_engine.dry_run.reset_mock()
        code2 = cli.main(["orchestrate", "Dry run task 2", "-n", "--mode", "implement"])
        self.assertEqual(code2, 0)
        self.mock_engine.dry_run.assert_called_once_with("Dry run task 2", mode=RunMode.IMPLEMENT)
        self.mock_engine.run.assert_not_called()

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_status(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        self.mock_store.get_run.return_value = self.default_state
        self.mock_store.get_results.return_value = []

        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["orchestrate", "status", "run-test-1"])
        self.assertEqual(code, 0)
        self.mock_store.get_run.assert_called_once_with(RunId("run-test-1"))
        self.assertIn("Run ID:        run-test-1", out.getvalue())
        self.assertIn("Status:        COMPLETED", out.getvalue())
        self.assertIn("Task:          Default task", out.getvalue())
        self.mock_engine.run.assert_not_called()
        self.mock_runner.run.assert_not_called()

        # Nonexistent run returns 1
        self.mock_store.get_run.return_value = None
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code_not_found = cli.main(["orchestrate", "status", "run-ghost"])
        self.assertEqual(code_not_found, 1)
        self.assertIn("run not found: run-ghost", err.getvalue())

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_inspect(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        self.mock_store.get_run.return_value = self.default_state
        self.mock_store.get_results.return_value = []
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["orchestrate", "inspect", "run-test-1"])
        self.assertEqual(code, 0)
        self.assertIn("Run ID:        run-test-1", out.getvalue())

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_depth_is_forwarded(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        code = cli.main(["orchestrate", "Deep review", "--depth", "deep"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_build.call_args.kwargs["depth"], "deep")
        self.assertIsNone(mock_build.call_args.kwargs["coordinator_profile"])
        self.assertIn("profile_store", mock_build.call_args.kwargs)

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_resume(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        code = cli.main(["orchestrate", "resume", "run-test-1"])
        self.assertEqual(code, 0)
        self.mock_engine.resume.assert_called_once_with(RunId("run-test-1"))

        # Resume resulting in interrupted returns 130
        int_state = RunState(run_id=RunId("run-int"), task="t", status=RunStatus.INTERRUPTED)
        self.mock_engine.resume.return_value = int_state
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code_int = cli.main(["orchestrate", "resume", "run-int"])
        self.assertEqual(code_int, 130)
        self.assertIn("interrupted", err.getvalue())

        # Resume resulting in failure returns 1
        fail_state = RunState(run_id=RunId("run-fail"), task="t", status=RunStatus.FAILED)
        self.mock_engine.resume.return_value = fail_state
        code_fail = cli.main(["orchestrate", "resume", "run-fail"])
        self.assertEqual(code_fail, 1)

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_unknown_subcommand(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        for bad_cmd in [
            ["orchestrate", "unknown"],
            ["orchestrate", "invalid"],
            ["orchestrate", "unknown-subcommand", "foo"],
            ["orchestrate", "cancel", "run-123"],
            ["orchestrate", "foobar", "extra"],
        ]:
            err = io.StringIO()
            with self.subTest(bad_cmd=bad_cmd), mock.patch("sys.stderr", err):
                code = cli.main(bad_cmd)
                self.assertEqual(code, 2)
                self.assertIn("unknown subcommand", err.getvalue())

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_missing_run_id(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        for args in [
            ["orchestrate", "status"],
            ["orchestrate", "status", ""],
            ["orchestrate", "status", "   "],
            ["orchestrate", "inspect"],
            ["orchestrate", "inspect", ""],
            ["orchestrate", "resume"],
            ["orchestrate", "resume", ""],
            ["orchestrate", "resume", "   "],
        ]:
            err = io.StringIO()
            with self.subTest(args=args), mock.patch("sys.stderr", err):
                code = cli.main(args)
                self.assertEqual(code, 2)
                self.assertIn("run ID", err.getvalue())

    @mock.patch("agym.cli.ProfileStore")
    def test_orchestrate_reserved_profile_name(self, Store: mock.Mock) -> None:
        Store.return_value = self.store
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cli.main(["setup", "orchestrate"])
        self.assertEqual(code, 2)
        self.assertIn("reserved", err.getvalue())

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_keyboard_interrupt(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        # KeyboardInterrupt in engine.run
        self.mock_engine.run.side_effect = KeyboardInterrupt
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cli.main(["orchestrate", "task to cancel"])
        self.assertEqual(code, 130)
        self.assertIn("interrupted", err.getvalue())

        # KeyboardInterrupt in engine.resume
        self.mock_engine.resume.side_effect = KeyboardInterrupt
        err2 = io.StringIO()
        with mock.patch("sys.stderr", err2):
            code2 = cli.main(["orchestrate", "resume", "run-123"])
        self.assertEqual(code2, 130)
        self.assertIn("interrupted", err2.getvalue())

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_exit_codes(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps

        # 0: Completed run
        self.mock_engine.run.return_value = RunState(run_id=RunId("r1"), task="t", status=RunStatus.COMPLETED)
        self.assertEqual(cli.main(["orchestrate", "task"]), 0)

        # 1: Failed run
        self.mock_engine.run.return_value = RunState(run_id=RunId("r2"), task="t", status=RunStatus.FAILED)
        self.assertEqual(cli.main(["orchestrate", "task"]), 1)

        # 130: Interrupted run status
        self.mock_engine.run.return_value = RunState(run_id=RunId("r3"), task="t", status=RunStatus.INTERRUPTED)
        self.assertEqual(cli.main(["orchestrate", "task"]), 130)

        # 2: Missing task / no arguments
        self.assertEqual(cli.main(["orchestrate"]), 2)

        # 2: Invalid mode
        self.assertEqual(cli.main(["orchestrate", "task", "--mode", "invalid_mode"]), 2)

        # 2: Missing run ID
        self.assertEqual(cli.main(["orchestrate", "status"]), 2)
        self.assertEqual(cli.main(["orchestrate", "resume"]), 2)

        # 2: Unknown subcommand
        self.assertEqual(cli.main(["orchestrate", "badsubcommand", "foo"]), 2)

        # 1: Nonexistent status run
        self.mock_store.get_run.return_value = None
        self.assertEqual(cli.main(["orchestrate", "status", "nonexistent-id"]), 1)

        # 1: Invalid dry-run plan
        self.mock_engine.dry_run.return_value = DryRunPlan(
            run_id=RunId("d1"), task="t", mode=RunMode.PLAN, is_valid=False, validation_error="Capacity error"
        )
        self.assertEqual(cli.main(["orchestrate", "task", "--dry-run"]), 1)

    def test_orchestrate_help(self) -> None:
        for flag in ["-h", "--help"]:
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["orchestrate", flag])
            self.assertEqual(code, 0)
            self.assertIn("agym orchestrate", out.getvalue())
            self.assertIn("Usage:", out.getvalue())
            self.assertIn("status <run-id>", out.getvalue())
            self.assertIn("inspect <run-id>", out.getvalue())
            self.assertIn("--depth {quick,balanced,deep}", out.getvalue())
            self.assertIn("resume <run-id>", out.getvalue())

    @mock.patch("agym.cli.build_orchestration_dependencies")
    def test_orchestrate_run_alias(self, mock_build: mock.Mock) -> None:
        mock_build.return_value = self.deps
        code = cli.main(["orchestrate", "run", "Refactor module"])
        self.assertEqual(code, 0)
        self.mock_engine.run.assert_called_once_with("Refactor module", mode=RunMode.PLAN)


class WiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(self.root / "config", self.root / "data")
        self.store.create("p1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_budget_values(self) -> None:
        from agym.orchestration.wiring import (
            DEFAULT_MAX_BOOST_INVOCATIONS,
            DEFAULT_MAX_INVOCATIONS,
            DEFAULT_MAX_PARALLEL,
            DEFAULT_MAX_RETRIES,
            DEFAULT_MAX_ROUNDS,
            DEFAULT_MAX_RUNTIME_SECONDS,
            DEFAULT_MIN_QUOTA_REMAINING_PERCENT,
            build_default_budget,
        )

        b = build_default_budget()
        self.assertEqual(b.max_parallel, DEFAULT_MAX_PARALLEL)
        self.assertEqual(b.max_invocations, DEFAULT_MAX_INVOCATIONS)
        self.assertEqual(b.max_rounds, DEFAULT_MAX_ROUNDS)
        self.assertEqual(b.max_boost_invocations, DEFAULT_MAX_BOOST_INVOCATIONS)
        self.assertEqual(b.max_retries, DEFAULT_MAX_RETRIES)
        self.assertEqual(b.max_runtime_seconds, DEFAULT_MAX_RUNTIME_SECONDS)
        self.assertEqual(b.min_quota_remaining, DEFAULT_MIN_QUOTA_REMAINING_PERCENT)

    def test_build_orchestration_dependencies_defaults(self) -> None:
        from agym.orchestration.wiring import build_orchestration_dependencies

        deps = build_orchestration_dependencies(
            profile_store=self.store,
            stream=io.StringIO(),
            is_tty=False,
            use_color=False,
        )
        self.assertIsNotNone(deps.profile_store)
        self.assertIsNotNone(deps.cache_manager)
        self.assertIsNotNone(deps.lease_manager)
        self.assertIsNotNone(deps.scheduler)
        self.assertIsNotNone(deps.run_store)
        self.assertIsNotNone(deps.runner)
        self.assertIsNotNone(deps.coordinator)
        self.assertIsNotNone(deps.event_sink)
        self.assertIsNotNone(deps.engine)
        self.assertIsNotNone(deps.budget)

    def test_broadcast_run_store(self) -> None:
        from agym.orchestration.contracts import EventType, OrchestrationEvent, RunId
        from agym.orchestration.wiring import BroadcastRunStore

        mock_store = mock.MagicMock()
        mock_sink = mock.MagicMock()
        broadcaster = BroadcastRunStore(mock_store, mock_sink)

        evt = OrchestrationEvent(
            event_id="e1",
            run_id=RunId("r1"),
            type=EventType.RUN_CREATED,
        )
        broadcaster.emit(evt)
        mock_store.emit.assert_called_once_with(evt)
        mock_sink.emit.assert_called_once_with(evt)

        # Delegated methods
        broadcaster.get_run(RunId("r1"))
        mock_store.get_run.assert_called_once_with(RunId("r1"))

        broadcaster.save_run(mock.MagicMock())
        mock_store.save_run.assert_called_once()

        broadcaster.list_runs()
        mock_store.list_runs.assert_called_once()

        broadcaster.save_result(RunId("r1"), mock.MagicMock())
        mock_store.save_result.assert_called_once()

        broadcaster.get_results(RunId("r1"))
        mock_store.get_results.assert_called_once_with(RunId("r1"))

        broadcaster.get_events(RunId("r1"))
        mock_store.get_events.assert_called_once_with(RunId("r1"))
