from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym import cli
from agym.panes.backends import BackendError, TerminalPaneBackend
from agym.panes.layout import LayoutPlan, calculate_layout
from agym.profiles import InvalidProfileName, Profile, ProfileStore


class MockBackend(TerminalPaneBackend):
    def __init__(self, exit_code: int = 0, should_fail: bool = False) -> None:
        self.exit_code = exit_code
        self.should_fail = should_fail
        self.called_with: dict[str, object] = {}

    @property
    def name(self) -> str:
        return "mock-backend"

    def launch_all(
        self,
        profiles: list[Profile],
        passthrough_args: list[str],
        plan: LayoutPlan,
        cwd: Path | str,
        store: ProfileStore,
        launcher_fn: object = None,
    ) -> int:
        if self.should_fail:
            raise BackendError("mock backend internal failure")
        self.called_with = {
            "profiles": [p.name for p in profiles],
            "passthrough_args": list(passthrough_args),
            "plan": plan,
            "cwd": str(cwd),
        }
        return self.exit_code


class CliAllCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(self.root / "config", self.root / "data")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reserved_name_all(self) -> None:
        with self.assertRaises(InvalidProfileName):
            self.store.create("all")

    @mock.patch("agym.cli.ProfileStore")
    def test_all_command_dispatched_and_not_treated_as_profile_name(self, Store: mock.Mock) -> None:
        Store.return_value = self.store
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["all", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("Launch a bounded number of configured Antigravity profiles", out.getvalue())

    @mock.patch("agym.cli.ProfileStore")
    def test_zero_profiles_prints_setup_message(self, Store: mock.Mock) -> None:
        Store.return_value = self.store
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["all"])
        self.assertEqual(code, 0)
        self.assertIn("No profiles configured. Run 'agym setup <profile>' first.", out.getvalue())

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    def test_single_profile_bypasses_pane_backend(
        self, run_mock: mock.Mock, resolve_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        p1 = self.store.create("solo")
        Store.return_value = self.store
        resolve_mock.return_value = Path("/mock/agy")
        run_mock.return_value = 0

        code = cli.main(["all", "-p", "do work"])
        self.assertEqual(code, 0)
        run_mock.assert_called_once_with(
            Path("/mock/agy"), p1, ["-p", "do work"], replace_process=True
        )

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_multi_profile_launches_detected_backend(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        p1 = self.store.create("alpha")
        p2 = self.store.create("beta")
        p3 = self.store.create("gamma")
        Store.return_value = self.store

        mock_backend = MockBackend(exit_code=0)
        detect_mock.return_value = mock_backend

        code = cli.main(["all", "--", "-p", "review code"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_backend.called_with["profiles"], ["alpha", "beta", "gamma"])
        self.assertEqual(mock_backend.called_with["passthrough_args"], ["-p", "review code"])
        plan = mock_backend.called_with["plan"]
        self.assertIsInstance(plan, LayoutPlan)
        self.assertEqual(plan.total_panes, 3)

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_unsupported_terminal_graceful_exit(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        self.store.create("p1")
        self.store.create("p2")
        Store.return_value = self.store
        detect_mock.return_value = None

        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cli.main(["all"])
        self.assertEqual(code, 1)
        err_output = err.getvalue()
        self.assertIn("agym all: unable to create terminal panes", err_output)
        self.assertIn("Supported options", err_output)

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_backend_failure_graceful_exit(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        self.store.create("p1")
        self.store.create("p2")
        Store.return_value = self.store
        mock_backend = MockBackend(should_fail=True)
        detect_mock.return_value = mock_backend

        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cli.main(["all"])
        self.assertEqual(code, 1)
        self.assertIn("agym all: mock backend internal failure", err.getvalue())

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    def test_existing_profile_command_not_regressed(
        self, run_mock: mock.Mock, resolve_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        p1 = self.store.create("myprofile")
        Store.return_value = self.store
        resolve_mock.return_value = Path("/mock/agy")
        run_mock.return_value = 0

        code = cli.main(["myprofile", "--dsp"])
        self.assertEqual(code, 0)
        run_mock.assert_called_once_with(
            Path("/mock/agy"), p1, ["--dsp"], replace_process=True
        )


    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_all_defaults_to_four_profiles(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        for name in ("p1", "p2", "p3", "p4", "p5", "p6"):
            self.store.create(name)
        Store.return_value = self.store

        mock_backend = MockBackend(exit_code=0)
        detect_mock.return_value = mock_backend

        code = cli.main(["all"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_backend.called_with["profiles"], ["p1", "p2", "p3", "p4"])
        self.assertEqual(mock_backend.called_with["plan"].total_panes, 4)

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_all_positional_count_limits_profiles(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        for name in ("p1", "p2", "p3", "p4"):
            self.store.create(name)
        Store.return_value = self.store

        mock_backend = MockBackend(exit_code=0)
        detect_mock.return_value = mock_backend

        code = cli.main(["all", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_backend.called_with["profiles"], ["p1", "p2"])
        self.assertEqual(mock_backend.called_with["plan"].total_panes, 2)


    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_all_count_option_limits_profiles(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        self.store.create("p1")
        self.store.create("p2")
        self.store.create("p3")
        self.store.create("p4")
        Store.return_value = self.store

        mock_backend = MockBackend(exit_code=0)
        detect_mock.return_value = mock_backend

        code = cli.main(["all", "-n", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_backend.called_with["profiles"], ["p1", "p2"])
        self.assertEqual(mock_backend.called_with["plan"].total_panes, 2)

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_all_cwd_option_sets_target_directory(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        self.store.create("p1")
        self.store.create("p2")
        Store.return_value = self.store

        mock_backend = MockBackend(exit_code=0)
        detect_mock.return_value = mock_backend

        target_dir = Path(self.tmp.name)
        code = cli.main(["all", "-C", str(target_dir)])
        self.assertEqual(code, 0)
        self.assertEqual(mock_backend.called_with["cwd"], str(target_dir.resolve()))

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_all_profiles_subset_selection(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        self.store.create("p1")
        self.store.create("p2")
        self.store.create("p3")
        Store.return_value = self.store

        mock_backend = MockBackend(exit_code=0)
        detect_mock.return_value = mock_backend

        code = cli.main(["all", "--profiles", "p3,p1"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_backend.called_with["profiles"], ["p3", "p1"])

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.panes.runner.detect_backend")
    def test_all_dsp_and_combined_options(
        self, detect_mock: mock.Mock, Store: mock.Mock
    ) -> None:
        self.store.create("p1")
        self.store.create("p2")
        self.store.create("p3")
        Store.return_value = self.store

        mock_backend = MockBackend(exit_code=0)
        detect_mock.return_value = mock_backend

        target_dir = Path(self.tmp.name)
        code = cli.main(["all", "-n", "2", "-C", str(target_dir), "--dsp"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_backend.called_with["profiles"], ["p1", "p2"])
        self.assertEqual(mock_backend.called_with["cwd"], str(target_dir.resolve()))
        self.assertEqual(mock_backend.called_with["passthrough_args"], ["--dsp"])


if __name__ == "__main__":
    unittest.main()
