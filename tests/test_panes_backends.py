from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.panes.backends import (
    TMUX_SPLIT_FLAGS,
    WEZTERM_SPLIT_FLAGS,
    WT_SPLIT_FLAGS,
    BackendError,
    TmuxActiveBackend,
    TmuxManagedBackend,
    WezTermBackend,
    WindowsTerminalBackend,
    build_profile_command,
    calculate_navigation_moves,
)
from agym.panes.detector import detect_backend, detect_terminal_name, get_unsupported_message
from agym.panes.layout import Rect, SplitDirection, calculate_layout
from agym.profiles import Profile, ProfileSettings, ProfileStore


class BackendDetectorTests(unittest.TestCase):
    def test_windows_terminal_detected_on_windows(self) -> None:
        backend = detect_backend(
            system="Windows",
            environ={"WT_SESSION": "some-guid-1234"},
            which=lambda _: None,
        )
        self.assertIsInstance(backend, WindowsTerminalBackend)
        self.assertEqual(backend.name, "windows-terminal")

    def test_windows_unsupported_without_wt_session(self) -> None:
        backend = detect_backend(
            system="Windows",
            environ={"PROMPT": "$P$G"},
            which=lambda _: None,
        )
        self.assertIsNone(backend)
        msg = get_unsupported_message(system="Windows", environ={})
        self.assertIn("Windows Terminal", msg)
        self.assertIn("wt.exe", msg)

    def test_active_tmux_on_linux(self) -> None:
        backend = detect_backend(
            system="Linux",
            environ={"TMUX": "/tmp/tmux-1000/default,1234,0"},
            which=lambda _: "/usr/bin/tmux",
        )
        self.assertIsInstance(backend, TmuxActiveBackend)
        self.assertEqual(backend.name, "tmux-active")

    def test_active_tmux_on_macos(self) -> None:
        backend = detect_backend(
            system="Darwin",
            environ={"TMUX": "/tmp/tmux-501/default,5678,0"},
            which=lambda _: "/opt/homebrew/bin/tmux",
        )
        self.assertIsInstance(backend, TmuxActiveBackend)
        self.assertEqual(backend.name, "tmux-active")

    def test_managed_tmux_fallback_linux(self) -> None:
        backend = detect_backend(
            system="Linux",
            environ={},
            which=lambda cmd: "/usr/bin/tmux" if cmd == "tmux" else None,
        )
        self.assertIsInstance(backend, TmuxManagedBackend)
        self.assertEqual(backend.name, "tmux-managed")

    def test_managed_tmux_fallback_macos(self) -> None:
        backend = detect_backend(
            system="Darwin",
            environ={},
            which=lambda cmd: "/usr/local/bin/tmux" if cmd == "tmux" else None,
        )
        self.assertIsInstance(backend, TmuxManagedBackend)
        self.assertEqual(backend.name, "tmux-managed")

    def test_wezterm_detected_when_in_wezterm(self) -> None:
        backend = detect_backend(
            system="Linux",
            environ={"WEZTERM_PANE": "1"},
            which=lambda cmd: "/usr/bin/wezterm" if cmd == "wezterm" else None,
        )
        self.assertIsInstance(backend, WezTermBackend)
        self.assertEqual(backend.name, "wezterm")

    def test_unsupported_terminal_linux_without_tmux(self) -> None:
        backend = detect_backend(
            system="Linux",
            environ={"TERM": "xterm-256color"},
            which=lambda _: None,
        )
        self.assertIsNone(backend)
        msg = get_unsupported_message(system="Linux", environ={"TERM": "xterm-256color"})
        self.assertIn("agym all: unable to create terminal panes", msg)
        self.assertIn("OS:       Linux", msg)
        self.assertIn("tmux", msg)

    def test_unsupported_terminal_macos_without_tmux(self) -> None:
        backend = detect_backend(
            system="Darwin",
            environ={"TERM_PROGRAM": "Apple_Terminal"},
            which=lambda _: None,
        )
        self.assertIsNone(backend)
        msg = get_unsupported_message(system="Darwin", environ={"TERM_PROGRAM": "Apple_Terminal"})
        self.assertIn("Apple_Terminal", msg)
        self.assertIn("brew install tmux", msg)


class BackendExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(self.root / "config", self.root / "data")
        self.p1 = self.store.create("profile1")
        self.p2 = self.store.create("profile2")
        self.p3 = self.store.create("profile3")
        self.p4 = self.store.create("profile4")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_build_profile_command(self) -> None:
        cmd = build_profile_command("myprof", ["-p", "test prompt"], agym_bin="/custom/agym")
        self.assertEqual(cmd, ["/custom/agym", "myprof", "-p", "test prompt"])

    def test_calculate_navigation_moves(self) -> None:
        r_tl = Rect(0.0, 0.0, 0.5, 0.5)
        r_tr = Rect(0.5, 0.0, 0.5, 0.5)
        r_bl = Rect(0.0, 0.5, 0.5, 0.5)
        r_br = Rect(0.5, 0.5, 0.5, 0.5)

        self.assertEqual(calculate_navigation_moves(r_tl, r_tr), ["right"])
        self.assertEqual(calculate_navigation_moves(r_tr, r_tl), ["left"])
        self.assertEqual(calculate_navigation_moves(r_tl, r_bl), ["down"])
        self.assertEqual(calculate_navigation_moves(r_bl, r_tl), ["up"])
        self.assertEqual(calculate_navigation_moves(r_br, r_tl), ["left", "up"])

    def test_windows_terminal_build_command(self) -> None:
        backend = WindowsTerminalBackend()
        profiles = [self.p1, self.p2, self.p3, self.p4]
        plan = calculate_layout(4)
        cwd = Path("/work/dir")

        wt_cmd = backend.build_wt_command(profiles, ["--flag"], plan, cwd)
        self.assertEqual(wt_cmd[0], "wt.exe")
        self.assertEqual(wt_cmd[1:3], ["-w", "0"])

        # Check that wt_cmd is an argument vector and contains documented commands
        self.assertIn("split-pane", wt_cmd)
        self.assertIn("move-focus", wt_cmd)
        self.assertIn(";", wt_cmd)
        self.assertIn("-d", wt_cmd)
        self.assertIn(str(cwd), wt_cmd)
        self.assertIn("profile2", wt_cmd)
        self.assertIn("profile3", wt_cmd)
        self.assertIn("profile4", wt_cmd)

    def test_windows_terminal_launch_all(self) -> None:
        mock_runner = mock.MagicMock(return_value=mock.Mock(returncode=0))
        backend = WindowsTerminalBackend(runner=mock_runner)
        profiles = [self.p1, self.p2]
        plan = calculate_layout(2)
        cwd = Path("/work/dir")
        mock_launcher = mock.MagicMock(return_value=0)

        ret = backend.launch_all(profiles, [], plan, cwd, self.store, launcher_fn=mock_launcher)
        self.assertEqual(ret, 0)
        mock_runner.assert_called_once()
        wt_args = mock_runner.call_args[0][0]
        self.assertEqual(wt_args[:3], ["wt.exe", "-w", "0"])
        # Profile 0 launched in current pane
        mock_launcher.assert_called_once_with("profile1", [], self.store)

    def test_windows_terminal_failure_raises_backend_error(self) -> None:
        mock_runner = mock.MagicMock(return_value=mock.Mock(returncode=1))
        backend = WindowsTerminalBackend(runner=mock_runner)
        profiles = [self.p1, self.p2]
        plan = calculate_layout(2)
        with self.assertRaises(BackendError):
            backend.launch_all(profiles, [], plan, Path("/work"), self.store)

    def test_tmux_active_launch_all(self) -> None:
        calls: list[list[str]] = []

        def mock_runner(cmd: list[str], **kwargs: object) -> mock.Mock:
            calls.append(cmd)
            if cmd[:3] == ["tmux", "display-message", "-p"]:
                return mock.Mock(stdout="%0\n", returncode=0)
            if cmd[:2] == ["tmux", "split-window"]:
                return mock.Mock(stdout=f"%{len(calls)}\n", returncode=0)
            return mock.Mock(returncode=0)

        backend = TmuxActiveBackend(runner=mock_runner)
        profiles = [self.p1, self.p2, self.p3]
        plan = calculate_layout(3)
        cwd = Path("/my/repo")
        mock_launcher = mock.MagicMock(return_value=0)

        ret = backend.launch_all(profiles, ["-v"], plan, cwd, self.store, launcher_fn=mock_launcher)
        self.assertEqual(ret, 0)

        # 1 call for display-message, 2 calls for splits
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], ["tmux", "display-message", "-p", "#{pane_id}"])
        self.assertIn("-c", calls[1])
        self.assertIn(str(cwd), calls[1])
        self.assertIn("profile2", calls[1])
        self.assertIn("profile3", calls[2])
        # Current pane runs profile1
        mock_launcher.assert_called_once_with("profile1", ["-v"], self.store)

    def test_tmux_managed_launch_all_and_cleanup_on_error(self) -> None:
        calls: list[list[str]] = []

        def mock_runner(cmd: list[str], **kwargs: object) -> mock.Mock:
            calls.append(cmd)
            if cmd[:2] == ["tmux", "new-session"]:
                return mock.Mock(stdout="%0\n", returncode=0)
            if cmd[:2] == ["tmux", "split-window"]:
                raise subprocess.SubprocessError("split failed")
            return mock.Mock(returncode=0)

        backend = TmuxManagedBackend(runner=mock_runner)
        profiles = [self.p1, self.p2]
        plan = calculate_layout(2)
        cwd = Path("/my/repo")

        with self.assertRaises(BackendError):
            backend.launch_all(profiles, [], plan, cwd, self.store)

        # Ensure kill-session was called to prevent orphaned sessions
        kill_calls = [c for c in calls if len(c) >= 3 and c[:2] == ["tmux", "kill-session"]]
        self.assertEqual(len(kill_calls), 1)
        self.assertTrue(kill_calls[0][2] == "-t" and kill_calls[0][3].startswith("agym-"))

    def test_tmux_4_profiles_produces_2x2_sequence(self) -> None:
        calls: list[list[str]] = []

        def mock_runner(cmd: list[str], **kwargs: object) -> mock.Mock:
            calls.append(cmd)
            if cmd[:3] == ["tmux", "display-message", "-p"]:
                return mock.Mock(stdout="%0\n", returncode=0)
            if cmd[:2] == ["tmux", "split-window"]:
                return mock.Mock(stdout=f"%{len(calls) - 1}\n", returncode=0)
            return mock.Mock(returncode=0)

        backend = TmuxActiveBackend(runner=mock_runner)
        profiles = [self.p1, self.p2, self.p3, self.p4]
        plan = calculate_layout(4)
        mock_launcher = mock.MagicMock(return_value=0)

        backend.launch_all(profiles, [], plan, Path("/repo"), self.store, launcher_fn=mock_launcher)

        # Split 1: targets %0 with -h -> creates %1
        # Split 2: targets %1 with -v -> creates %2
        # Split 3: targets %0 with -v -> creates %3
        splits = [c for c in calls if c[:2] == ["tmux", "split-window"]]
        self.assertEqual(len(splits), 3)

        self.assertEqual(splits[0][2:5], ["-t", "%0", "-h"])
        self.assertEqual(splits[1][2:5], ["-t", "%1", "-v"])
        self.assertEqual(splits[2][2:5], ["-t", "%0", "-v"])

    def test_tmux_5_profiles_splits_bottom_right_quadrant_vertically(self) -> None:
        calls: list[list[str]] = []

        def mock_runner(cmd: list[str], **kwargs: object) -> mock.Mock:
            calls.append(cmd)
            if cmd[:3] == ["tmux", "display-message", "-p"]:
                return mock.Mock(stdout="%0\n", returncode=0)
            if cmd[:2] == ["tmux", "split-window"]:
                return mock.Mock(stdout=f"%{len(calls) - 1}\n", returncode=0)
            return mock.Mock(returncode=0)

        backend = TmuxActiveBackend(runner=mock_runner)
        p5 = self.store.create("profile5")
        profiles = [self.p1, self.p2, self.p3, self.p4, p5]
        plan = calculate_layout(5)
        mock_launcher = mock.MagicMock(return_value=0)

        backend.launch_all(profiles, [], plan, Path("/repo"), self.store, launcher_fn=mock_launcher)

        splits = [c for c in calls if c[:2] == ["tmux", "split-window"]]
        self.assertEqual(len(splits), 4)

        # 4th split must target %2 (bottom-right quadrant) with -v (horizontal line / vertical stack)
        self.assertEqual(splits[3][2:5], ["-t", "%2", "-v"])
        self.assertIn("profile5", splits[3])

    def test_tmux_6_profiles_splits_bottom_left_quadrant_vertically(self) -> None:
        calls: list[list[str]] = []

        def mock_runner(cmd: list[str], **kwargs: object) -> mock.Mock:
            calls.append(cmd)
            if cmd[:3] == ["tmux", "display-message", "-p"]:
                return mock.Mock(stdout="%0\n", returncode=0)
            if cmd[:2] == ["tmux", "split-window"]:
                return mock.Mock(stdout=f"%{len(calls) - 1}\n", returncode=0)
            return mock.Mock(returncode=0)

        backend = TmuxActiveBackend(runner=mock_runner)
        p5 = self.store.create("profile5")
        p6 = self.store.create("profile6")
        profiles = [self.p1, self.p2, self.p3, self.p4, p5, p6]
        plan = calculate_layout(6)
        mock_launcher = mock.MagicMock(return_value=0)

        backend.launch_all(profiles, [], plan, Path("/repo"), self.store, launcher_fn=mock_launcher)

        splits = [c for c in calls if c[:2] == ["tmux", "split-window"]]
        self.assertEqual(len(splits), 5)

        # 4th split targets %2 with -v (bottom-right)
        self.assertEqual(splits[3][2:5], ["-t", "%2", "-v"])
        # 5th split targets %3 with -v (bottom-left)
        self.assertEqual(splits[4][2:5], ["-t", "%3", "-v"])
        self.assertIn("profile6", splits[4])

    def test_logical_orientation_to_multiplexer_flags_mapping(self) -> None:
        """Explicitly verify that logical orientations translate to the correct multiplexer flags:
        - LEFT_RIGHT (side-by-side, vertical divider) -> tmux: -h, wt: -V, wezterm: --horizontal
        - TOP_BOTTOM (stacked, horizontal divider)    -> tmux: -v, wt: -H, wezterm: --bottom
        """
        # tmux mappings
        self.assertEqual(TMUX_SPLIT_FLAGS[SplitDirection.LEFT_RIGHT], "-h")
        self.assertEqual(TMUX_SPLIT_FLAGS[SplitDirection.TOP_BOTTOM], "-v")
        # Backward-compatibility enum aliases
        self.assertEqual(TMUX_SPLIT_FLAGS[SplitDirection.HORIZONTAL], "-h")
        self.assertEqual(TMUX_SPLIT_FLAGS[SplitDirection.VERTICAL], "-v")

        # Windows Terminal mappings
        self.assertEqual(WT_SPLIT_FLAGS[SplitDirection.LEFT_RIGHT], "-V")
        self.assertEqual(WT_SPLIT_FLAGS[SplitDirection.TOP_BOTTOM], "-H")
        self.assertEqual(WT_SPLIT_FLAGS[SplitDirection.HORIZONTAL], "-V")
        self.assertEqual(WT_SPLIT_FLAGS[SplitDirection.VERTICAL], "-H")

        # WezTerm mappings
        self.assertEqual(WEZTERM_SPLIT_FLAGS[SplitDirection.LEFT_RIGHT], "--horizontal")
        self.assertEqual(WEZTERM_SPLIT_FLAGS[SplitDirection.TOP_BOTTOM], "--bottom")
        self.assertEqual(WEZTERM_SPLIT_FLAGS[SplitDirection.HORIZONTAL], "--horizontal")
        self.assertEqual(WEZTERM_SPLIT_FLAGS[SplitDirection.VERTICAL], "--bottom")

    def test_tmux_managed_configures_mouse_and_status_without_global_leak(self) -> None:
        calls: list[list[str]] = []

        def mock_runner(cmd: list[str], **kwargs: object) -> mock.Mock:
            calls.append(cmd)
            if cmd[:2] == ["tmux", "new-session"]:
                return mock.Mock(stdout="%0\n", returncode=0)
            if cmd[:2] == ["tmux", "split-window"]:
                return mock.Mock(stdout="%1\n", returncode=0)
            if cmd[:2] == ["tmux", "list-sessions"]:
                return mock.Mock(stdout="", returncode=0)
            return mock.Mock(returncode=0)

        backend = TmuxManagedBackend(runner=mock_runner)
        profiles = [self.p1, self.p2]
        plan = calculate_layout(2)

        # Mock os.execvp so test doesn't exec
        with mock.patch("os.execvp"):
            backend.launch_all(profiles, [], plan, Path("/repo"), self.store)

        # 1. Verify tmux new-session was passed terminal dimensions (-x and -y)
        new_session_call = [c for c in calls if c[:2] == ["tmux", "new-session"]][0]
        self.assertIn("-x", new_session_call)
        self.assertIn("-y", new_session_call)

        # 2. Verify set-option calls: both 'status off' and 'mouse on' must be configured
        # strictly targeted at the session (-t agym-*) with NO global leak (-g)
        set_opt_calls = [c for c in calls if c[:2] == ["tmux", "set-option"]]
        self.assertGreaterEqual(len(set_opt_calls), 2)

        for c in set_opt_calls:
            self.assertEqual(c[2], "-t")
            self.assertTrue(c[3].startswith("agym-"))
            self.assertNotIn("-g", c)

        # Check options map
        opt_dict: dict[str, str] = {}
        for c in set_opt_calls:
            if c[4] == "-w":
                opt_dict[c[5]] = c[6]
            elif c[4] == "-a":
                opt_dict[c[5]] = c[6]
            else:
                opt_dict[c[4]] = c[5]

        self.assertEqual(opt_dict.get("status"), "off")
        self.assertEqual(opt_dict.get("mouse"), "on")
        self.assertEqual(opt_dict.get("copy-mode-position-format"), "")
        self.assertEqual(opt_dict.get("mode-style"), "bg=colour24,fg=white")
        self.assertEqual(opt_dict.get("default-terminal"), "xterm-256color")
        self.assertEqual(opt_dict.get("set-clipboard"), "on")
        self.assertEqual(opt_dict.get("pane-border-lines"), "single")
        self.assertEqual(opt_dict.get("remain-on-exit"), "on")

        # 3. Verify client-detached hook is set to clean up session on exit
        hook_calls = [c for c in calls if c[:2] == ["tmux", "set-hook"]]
        self.assertEqual(len(hook_calls), 1)
        self.assertEqual(hook_calls[0][2:6], ["-t", hook_calls[0][3], "client-detached", "kill-session"])

        # 4. Verify initial focus is restored to profile 0's pane (%0)
        select_calls = [c for c in calls if c[:2] == ["tmux", "select-pane"]]
        self.assertEqual(len(select_calls), 1)
        self.assertEqual(select_calls[0], ["tmux", "select-pane", "-t", "%0"])

        # 5. Verify robust mouse bindings are applied (MouseDown1Pane, Drag, Cancel on Click)
        bind_calls = [c for c in calls if c[:2] == ["tmux", "bind-key"]]
        self.assertGreaterEqual(len(bind_calls), 10)

        # Drag selection enabled in root table
        root_drags = [c for c in bind_calls if c[2:5] == ["-T", "root", "MouseDrag1Pane"]]
        self.assertEqual(len(root_drags), 1)
        self.assertEqual(root_drags[0][5], "copy-mode -M")

        # Click to cancel in copy-mode
        copy_clicks = [c for c in bind_calls if c[2:5] == ["-T", "copy-mode", "MouseDown1Pane"]]
        self.assertEqual(len(copy_clicks), 1)
        self.assertIn("cancel", copy_clicks[0][5])

        # Drag end preserves selection
        copy_drag_ends = [c for c in bind_calls if c[2:5] == ["-T", "copy-mode", "MouseDragEnd1Pane"]]
        self.assertEqual(len(copy_drag_ends), 1)
        self.assertEqual(copy_drag_ends[0][5], "send-keys -X copy-selection-no-clear")

    def test_orphan_cleanup_terminates_unattached_managed_sessions(self) -> None:
        calls: list[list[str]] = []

        def mock_runner(cmd: list[str], **kwargs: object) -> mock.Mock:
            calls.append(cmd)
            if cmd[:2] == ["tmux", "list-sessions"]:
                return mock.Mock(
                    stdout="agym-old-1234:0\nagym-active-5678:1\nuser-session:0\n",
                    returncode=0,
                )
            if cmd[:2] == ["tmux", "new-session"]:
                return mock.Mock(stdout="%0\n", returncode=0)
            if cmd[:2] == ["tmux", "split-window"]:
                return mock.Mock(stdout="%1\n", returncode=0)
            return mock.Mock(returncode=0)

        backend = TmuxManagedBackend(runner=mock_runner)
        profiles = [self.p1, self.p2]
        plan = calculate_layout(2)

        with mock.patch("os.execvp"):
            backend.launch_all(profiles, [], plan, Path("/repo"), self.store)

        # Must kill unattached agym session (agym-old-1234), but NOT active session or user-session
        kill_calls = [c for c in calls if c[:2] == ["tmux", "kill-session"]]
        self.assertEqual(len(kill_calls), 1)
        self.assertEqual(kill_calls[0], ["tmux", "kill-session", "-t", "agym-old-1234"])

    def test_tmux_active_preserves_user_mouse_and_status_settings(self) -> None:
        calls: list[list[str]] = []

        def mock_runner(cmd: list[str], **kwargs: object) -> mock.Mock:
            calls.append(cmd)
            if cmd[:3] == ["tmux", "display-message", "-p"]:
                return mock.Mock(stdout="%0\n", returncode=0)
            if cmd[:2] == ["tmux", "split-window"]:
                return mock.Mock(stdout="%1\n", returncode=0)
            return mock.Mock(returncode=0)

        backend = TmuxActiveBackend(runner=mock_runner)
        profiles = [self.p1, self.p2]
        plan = calculate_layout(2)

        backend.launch_all(profiles, [], plan, Path("/repo"), self.store, launcher_fn=mock.MagicMock())

        # Verify absolutely NO set-option calls were made (preserving user's mouse and status preferences)
        set_opt_calls = [c for c in calls if c[:2] == ["tmux", "set-option"]]
        self.assertEqual(len(set_opt_calls), 0)

    def test_real_tmux_managed_session_options_and_2x2_grid(self) -> None:
        """Integration test using the real installed tmux binary (if available)."""
        import shutil
        if not shutil.which("tmux"):
            self.skipTest("tmux not installed on host")

        profiles = [self.p1, self.p2, self.p3, self.p4]
        plan = calculate_layout(4)

        # Intercept execvp so we can inspect the session before it exits
        created_session: list[str] = []

        def fake_execvp(file: str, args: list[str]) -> None:
            # args: ["tmux", "-u", "attach-session", "-t", session_name]
            self.assertIn("-u", args)
            created_session.append(args[-1])

        backend = TmuxManagedBackend()
        with mock.patch("os.execvp", side_effect=fake_execvp):
            backend.launch_all(profiles, ["--flag"], plan, Path(self.tmp.name), self.store)

        self.assertEqual(len(created_session), 1)
        sname = created_session[0]

        try:
            # 1. Verify session-local options: mouse on, status off
            res_mouse = subprocess.run(
                ["tmux", "show-options", "-t", sname, "mouse"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(res_mouse.stdout.strip(), "mouse on")

            res_status = subprocess.run(
                ["tmux", "show-options", "-t", sname, "status"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(res_status.stdout.strip(), "status off")

            # Verify copy-mode-position-format is cleared
            res_pos = subprocess.run(
                ["tmux", "show-options", "-t", sname, "-w", "copy-mode-position-format"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("copy-mode-position-format", res_pos.stdout)
            self.assertTrue("''" in res_pos.stdout or '""' in res_pos.stdout)

            # 2. Verify 4 panes exist forming a 2x2 grid
            res_panes = subprocess.run(
                ["tmux", "list-panes", "-t", sname, "-F", "#{pane_left},#{pane_top}"],
                capture_output=True,
                text=True,
                check=True,
            )
            coords = [
                tuple(map(int, line.strip().split(",")))
                for line in res_panes.stdout.strip().splitlines()
                if line.strip()
            ]
            self.assertEqual(len(coords), 4)

            # In a 2x2 grid, there must be 2 distinct X coordinates (left and right)
            # and 2 distinct Y coordinates (top and bottom)
            xs = sorted(list({c[0] for c in coords}))
            ys = sorted(list({c[1] for c in coords}))
            self.assertEqual(len(xs), 2)
            self.assertEqual(len(ys), 2)

            # All 4 quadrant positions (xs[0], ys[0]), (xs[1], ys[0]), (xs[0], ys[1]), (xs[1], ys[1])
            expected_grid = {(x, y) for x in xs for y in ys}
            self.assertEqual(set(coords), expected_grid)

        finally:
            # Clean up the test session
            subprocess.run(["tmux", "kill-session", "-t", sname], stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
