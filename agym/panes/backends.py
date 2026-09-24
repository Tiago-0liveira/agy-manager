from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..profiles import Profile, ProfileStore
from .layout import LayoutPlan, Rect, SplitDirection

# Explicit orientation-to-flag mappings to prevent inverted splits across multiplexers:
# LEFT_RIGHT produces side-by-side panes with a vertical divider.
# TOP_BOTTOM produces stacked panes with a horizontal divider.
TMUX_SPLIT_FLAGS: dict[SplitDirection, str] = {
    SplitDirection.LEFT_RIGHT: "-h",
    SplitDirection.TOP_BOTTOM: "-v",
}

WT_SPLIT_FLAGS: dict[SplitDirection, str] = {
    SplitDirection.LEFT_RIGHT: "-V",
    SplitDirection.TOP_BOTTOM: "-H",
}

WEZTERM_SPLIT_FLAGS: dict[SplitDirection, str] = {
    SplitDirection.LEFT_RIGHT: "--horizontal",
    SplitDirection.TOP_BOTTOM: "--bottom",
}


class BackendError(RuntimeError):
    pass


def build_profile_command(
    profile_name: str,
    passthrough_args: Sequence[str] = (),
    *,
    agym_bin: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Builds the command vector to launch an individual agym profile."""
    env = os.environ if environ is None else environ
    if agym_bin:
        cmd = [agym_bin]
    else:
        found = shutil.which("agym", path=env.get("PATH"))
        if found:
            cmd = ["agym"]
        else:
            cmd = [sys.executable, "-m", "agym"]

    cmd.append(profile_name)
    if passthrough_args:
        cmd.extend(passthrough_args)
    return cmd


def calculate_navigation_moves(from_rect: Rect, to_rect: Rect) -> list[str]:
    """Calculates directional move-focus steps between two pane rectangles."""
    moves: list[str] = []
    # Horizontal movement
    if to_rect.center_x < from_rect.center_x - 1e-4:
        moves.append("left")
    elif to_rect.center_x > from_rect.center_x + 1e-4:
        moves.append("right")

    # Vertical movement
    if to_rect.center_y < from_rect.center_y - 1e-4:
        moves.append("up")
    elif to_rect.center_y > from_rect.center_y + 1e-4:
        moves.append("down")

    return moves


class TerminalPaneBackend(ABC):
    """Abstract base class for terminal multiplexer and pane managers."""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def launch_all(
        self,
        profiles: Sequence[Profile],
        passthrough_args: Sequence[str],
        plan: LayoutPlan,
        cwd: Path | str,
        store: ProfileStore,
        launcher_fn: Callable[..., int] | None = None,
    ) -> int:
        pass


class WindowsTerminalBackend(TerminalPaneBackend):
    """Native Windows Terminal backend using documented wt.exe command-line arguments.
    
    Targets the existing/recent Windows Terminal window (-w 0 / --window 0),
    reproduces the logical split plan using split-pane and move-focus commands,
    and runs profile commands in each resulting pane without opening separate GUI windows.
    """

    def __init__(self, wt_bin: str = "wt.exe", runner: Callable[..., Any] | None = None) -> None:
        self.wt_bin = wt_bin
        self.runner = runner or subprocess.run

    @property
    def name(self) -> str:
        return "windows-terminal"

    def build_wt_command(
        self,
        profiles: Sequence[Profile],
        passthrough_args: Sequence[str],
        plan: LayoutPlan,
        cwd: Path | str,
    ) -> list[str]:
        """Builds a single wt.exe argument vector chaining split-pane and move-focus actions."""
        # Target the existing/most recent window (-w 0)
        cmd: list[str] = [self.wt_bin, "-w", "0"]

        curr_focus = 0
        pane_rects: dict[int, Rect] = {0: Rect(0.0, 0.0, 1.0, 1.0)}
        has_action = False

        for step in plan.splits:
            target_id = step.target_pane_id
            new_id = step.new_pane_id

            # If the pane to split is not the currently focused pane, move focus to it
            if target_id != curr_focus:
                moves = calculate_navigation_moves(pane_rects[curr_focus], pane_rects[target_id])
                for m in moves:
                    if has_action:
                        cmd.append(";")
                    cmd.extend(["move-focus", m])
                    has_action = True
                curr_focus = target_id

            # Determine split direction flag:
            # LEFT_RIGHT (width split) -> produces side-by-side pane to the right (-V)
            # TOP_BOTTOM (height split) -> produces stacked pane below (-H)
            dir_flag = WT_SPLIT_FLAGS[step.direction]
            profile_cmd = build_profile_command(profiles[new_id].name, passthrough_args)

            if has_action:
                cmd.append(";")

            split_action = ["split-pane", dir_flag, "-d", str(cwd)]
            split_action.extend(profile_cmd)
            cmd.extend(split_action)
            has_action = True

            # Update tracked geometry and focus
            t_rect = pane_rects[target_id]
            if step.direction == SplitDirection.LEFT_RIGHT:
                pane_rects[target_id] = Rect(t_rect.x, t_rect.y, t_rect.width / 2.0, t_rect.height)
                pane_rects[new_id] = Rect(t_rect.x + (t_rect.width / 2.0), t_rect.y, t_rect.width / 2.0, t_rect.height)
            else:
                pane_rects[target_id] = Rect(t_rect.x, t_rect.y, t_rect.width, t_rect.height / 2.0)
                pane_rects[new_id] = Rect(t_rect.x, t_rect.y + (t_rect.height / 2.0), t_rect.width, t_rect.height / 2.0)

            curr_focus = new_id

        # Return focus to Pane 0 at the end if needed
        if curr_focus != 0 and 0 in pane_rects:
            moves = calculate_navigation_moves(pane_rects[curr_focus], pane_rects[0])
            for m in moves:
                if has_action:
                    cmd.append(";")
                cmd.extend(["move-focus", m])
                has_action = True

        return cmd

    def launch_all(
        self,
        profiles: Sequence[Profile],
        passthrough_args: Sequence[str],
        plan: LayoutPlan,
        cwd: Path | str,
        store: ProfileStore,
        launcher_fn: Callable[..., int] | None = None,
    ) -> int:
        if plan.total_panes > 1:
            wt_cmd = self.build_wt_command(profiles, passthrough_args, plan, cwd)
            try:
                res = self.runner(wt_cmd, check=False)
                retcode = getattr(res, "returncode", 0)
                if retcode != 0:
                    raise BackendError(f"Windows Terminal command failed with exit code {retcode}")
            except (OSError, subprocess.SubprocessError) as exc:
                raise BackendError(f"Failed to execute Windows Terminal command: {exc}") from exc

        # Launch profile 0 in the current pane
        if launcher_fn is not None:
            return launcher_fn(profiles[0].name, passthrough_args, store)
        from ..cli import _launch
        return _launch(profiles[0].name, list(passthrough_args), store)


class TmuxActiveBackend(TerminalPaneBackend):
    """Backend for when agym all is run inside an existing tmux session."""

    def __init__(self, runner: Callable[..., Any] | None = None) -> None:
        self.runner = runner or subprocess.run

    @property
    def name(self) -> str:
        return "tmux-active"

    def launch_all(
        self,
        profiles: Sequence[Profile],
        passthrough_args: Sequence[str],
        plan: LayoutPlan,
        cwd: Path | str,
        store: ProfileStore,
        launcher_fn: Callable[..., int] | None = None,
    ) -> int:
        # Determine the current pane ID
        try:
            proc = self.runner(
                ["tmux", "display-message", "-p", "#{pane_id}"],
                capture_output=True,
                text=True,
                check=True,
            )
            initial_pane = proc.stdout.strip().splitlines()[-1].strip()
        except Exception as exc:
            raise BackendError(f"Failed to query active tmux pane: {exc}") from exc

        pane_map: dict[int, str] = {0: initial_pane}

        for step in plan.splits:
            target_tmux_id = pane_map.get(step.target_pane_id)
            if not target_tmux_id:
                raise BackendError(f"Target pane ID {step.target_pane_id} not mapped to a tmux pane")

            # In tmux:
            # -h splits left/right (side-by-side, vertical divider)
            # -v splits top/bottom (stacked, horizontal divider)
            dir_flag = TMUX_SPLIT_FLAGS[step.direction]
            profile_cmd = build_profile_command(profiles[step.new_pane_id].name, passthrough_args)

            cmd = [
                "tmux",
                "split-window",
                "-t",
                target_tmux_id,
                dir_flag,
                "-c",
                str(cwd),
                "-P",
                "-F",
                "#{pane_id}",
                *profile_cmd,
            ]
            try:
                res = self.runner(cmd, capture_output=True, text=True, check=True)
                new_tmux_id = res.stdout.strip().splitlines()[-1].strip()
                pane_map[step.new_pane_id] = new_tmux_id
            except Exception as exc:
                raise BackendError(f"Failed to split tmux pane {target_tmux_id}: {exc}") from exc

        # Execute profile 0 in the current pane (replacing process on POSIX)
        if launcher_fn is not None:
            return launcher_fn(profiles[0].name, passthrough_args, store)
        from ..cli import _launch
        return _launch(profiles[0].name, list(passthrough_args), store)


class TmuxManagedBackend(TerminalPaneBackend):
    """Fallback backend for Linux/macOS outside tmux when tmux is available.
    
    Creates a dedicated collision-safe tmux session (agym-<pid>-<time>),
    builds the pane layout, launches profiles in all panes, attaches to it,
    and cleans up on failure.
    """

    def __init__(self, runner: Callable[..., Any] | None = None) -> None:
        self.runner = runner or subprocess.run

    @property
    def name(self) -> str:
        return "tmux-managed"

    def launch_all(
        self,
        profiles: Sequence[Profile],
        passthrough_args: Sequence[str],
        plan: LayoutPlan,
        cwd: Path | str,
        store: ProfileStore,
        launcher_fn: Callable[..., int] | None = None,
    ) -> int:
        session_name = f"agym-{os.getpid()}-{int(time.time())}"
        created = False

        # Clean up any stale unattached agym sessions from previous runs to prevent resource starvation
        try:
            list_proc = self.runner(
                ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}"],
                capture_output=True,
                text=True,
                check=False,
            )
            stdout = getattr(list_proc, "stdout", "")
            if isinstance(stdout, str) and getattr(list_proc, "returncode", 1) == 0:
                for line in stdout.splitlines():
                    parts = line.strip().split(":")
                    if len(parts) >= 2 and parts[0].startswith("agym-") and parts[1] == "0":
                        self.runner(["tmux", "kill-session", "-t", parts[0]], check=False)
        except Exception:
            pass

        try:
            # Query current terminal window dimensions so background session matches display
            cols, lines = shutil.get_terminal_size(fallback=(80, 24))

            # Create session with Profile 0
            cmd0 = build_profile_command(profiles[0].name, passthrough_args)
            new_session_cmd = [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-x",
                str(cols),
                "-y",
                str(lines),
                "-c",
                str(cwd),
                "-P",
                "-F",
                "#{pane_id}",
                *cmd0,
            ]
            proc = self.runner(new_session_cmd, capture_output=True, text=True, check=True)
            initial_pane = proc.stdout.strip().splitlines()[-1].strip()
            created = True
            pane_map: dict[int, str] = {0: initial_pane}

            # Create the remaining panes immediately according to the split plan
            for step in plan.splits:
                target_tmux_id = pane_map.get(step.target_pane_id)
                if not target_tmux_id:
                    raise BackendError(f"Target pane ID {step.target_pane_id} not mapped in session")

                dir_flag = TMUX_SPLIT_FLAGS[step.direction]
                profile_cmd = build_profile_command(profiles[step.new_pane_id].name, passthrough_args)

                split_cmd = [
                    "tmux",
                    "split-window",
                    "-t",
                    target_tmux_id,
                    dir_flag,
                    "-c",
                    str(cwd),
                    "-P",
                    "-F",
                    "#{pane_id}",
                    *profile_cmd,
                ]
                res = self.runner(split_cmd, capture_output=True, text=True, check=True)
                new_tmux_id = res.stdout.strip().splitlines()[-1].strip()
                pane_map[step.new_pane_id] = new_tmux_id

            # Disable status bar specifically for this managed session without affecting global/user config
            self.runner(["tmux", "set-option", "-t", session_name, "status", "off"], check=False)
            # Enable mouse support specifically for this managed session so clicking any pane focuses it
            self.runner(["tmux", "set-option", "-t", session_name, "mouse", "on"], check=False)
            # Preserve grid geometry and output if any profile process exits or encounters an error
            self.runner(["tmux", "set-option", "-t", session_name, "-w", "remain-on-exit", "on"], check=False)

            # Terminal emulation & font settings:
            # Force xterm-256color and RGB/TrueColor features to ensure clean fonts and syntax highlighting
            self.runner(["tmux", "set-option", "-t", session_name, "default-terminal", "xterm-256color"], check=False)
            self.runner(["tmux", "set-option", "-t", session_name, "-a", "terminal-features", ",*:RGB:256:clipboard:mouse"], check=False)
            self.runner(["tmux", "set-option", "-t", session_name, "-a", "terminal-overrides", ",*:Tc"], check=False)
            self.runner(["tmux", "set-option", "-t", session_name, "set-clipboard", "on"], check=False)

            # Pane borders: use clean Unicode box lines that do not trigger ACS font corruption
            self.runner(["tmux", "set-option", "-t", session_name, "pane-border-lines", "single"], check=False)
            self.runner(["tmux", "set-option", "-t", session_name, "-w", "pane-border-style", "fg=colour240"], check=False)
            self.runner(["tmux", "set-option", "-t", session_name, "-w", "pane-active-border-style", "fg=colour39"], check=False)

            # Clean up copy-mode overlay and styling:
            # 1. Hide '0/0' position indicator in the top right
            # 2. Modern slate-blue text selection highlight (no weird/bright yellow)
            self.runner(["tmux", "set-option", "-t", session_name, "-w", "copy-mode-position-format", ""], check=False)
            self.runner(["tmux", "set-option", "-t", session_name, "-w", "mode-style", "bg=colour24,fg=white"], check=False)

            # Ensure the managed session and its processes are destroyed when client detaches or window closes
            self.runner(["tmux", "set-hook", "-t", session_name, "client-detached", "kill-session"], check=False)

            # Mouse interaction & text highlighting bindings:
            # 1. Clicking any pane immediately focuses it
            # 2. Dragging mouse highlights text cleanly (soft slate-blue, no yellow, no 0/0)
            # 3. Double-clicking selects word, triple-clicking selects line without laggy run-shell subshell storms
            # 4. Clicking anywhere while in copy-mode cancels copy-mode and restores typing immediately
            # 5. Drag end preserves selection until next click or key
            self.runner(["tmux", "bind-key", "-T", "root", "MouseDown1Pane", "select-pane -t ="], check=False)
            self.runner(["tmux", "bind-key", "-T", "root", "MouseDrag1Pane", "copy-mode -M"], check=False)
            self.runner(["tmux", "bind-key", "-T", "root", "DoubleClick1Pane", "select-pane -t = ; copy-mode -H ; send-keys -X select-word"], check=False)
            self.runner(["tmux", "bind-key", "-T", "root", "TripleClick1Pane", "select-pane -t = ; copy-mode -H ; send-keys -X select-line"], check=False)

            self.runner(["tmux", "bind-key", "-T", "copy-mode", "MouseDown1Pane", "select-pane ; send-keys -X cancel"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode-vi", "MouseDown1Pane", "select-pane ; send-keys -X cancel"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode", "MouseDrag1Pane", "select-pane ; send-keys -X begin-selection"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode-vi", "MouseDrag1Pane", "select-pane ; send-keys -X begin-selection"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode", "MouseDragEnd1Pane", "send-keys -X copy-selection-no-clear"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode-vi", "MouseDragEnd1Pane", "send-keys -X copy-selection-no-clear"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode", "DoubleClick1Pane", "select-pane ; send-keys -X select-word"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode-vi", "DoubleClick1Pane", "select-pane ; send-keys -X select-word"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode", "TripleClick1Pane", "select-pane ; send-keys -X select-line"], check=False)
            self.runner(["tmux", "bind-key", "-T", "copy-mode-vi", "TripleClick1Pane", "select-pane ; send-keys -X select-line"], check=False)

            # Ensure initial focus is on profile 0 (initial pane)
            self.runner(["tmux", "select-pane", "-t", initial_pane], check=False)

            # Attach user terminal to the new session with forced UTF-8 mode (-u)
            attach_cmd = ["tmux", "-u", "attach-session", "-t", session_name]
            if os.name == "posix" and hasattr(os, "execvp"):
                # Replace current process with tmux client
                os.execvp("tmux", attach_cmd)
                return 0
            else:
                completed = self.runner(attach_cmd, check=False)
                return getattr(completed, "returncode", 0)

        except Exception as exc:
            # Clean up the session if setup fails so no broken/orphaned sessions remain
            if created:
                try:
                    self.runner(["tmux", "kill-session", "-t", session_name], check=False)
                except Exception:
                    pass
            raise BackendError(f"Failed to create managed tmux session: {exc}") from exc


class WezTermBackend(TerminalPaneBackend):
    """WezTerm backend using wezterm cli split-pane."""

    def __init__(self, runner: Callable[..., Any] | None = None) -> None:
        self.runner = runner or subprocess.run

    @property
    def name(self) -> str:
        return "wezterm"

    def launch_all(
        self,
        profiles: Sequence[Profile],
        passthrough_args: Sequence[str],
        plan: LayoutPlan,
        cwd: Path | str,
        store: ProfileStore,
        launcher_fn: Callable[..., int] | None = None,
    ) -> int:
        initial_pane = os.environ.get("WEZTERM_PANE", "0")
        pane_map: dict[int, str] = {0: initial_pane}

        for step in plan.splits:
            target_pane_id = pane_map.get(step.target_pane_id, "0")
            dir_flag = WEZTERM_SPLIT_FLAGS[step.direction]
            profile_cmd = build_profile_command(profiles[step.new_pane_id].name, passthrough_args)

            cmd = [
                "wezterm",
                "cli",
                "split-pane",
                "--pane-id",
                str(target_pane_id),
                dir_flag,
                "--cwd",
                str(cwd),
                "--",
                *profile_cmd,
            ]
            try:
                res = self.runner(cmd, capture_output=True, text=True, check=True)
                new_pane_id = res.stdout.strip()
                pane_map[step.new_pane_id] = new_pane_id
            except Exception as exc:
                raise BackendError(f"Failed to split WezTerm pane: {exc}") from exc

        if launcher_fn is not None:
            return launcher_fn(profiles[0].name, passthrough_args, store)
        from ..cli import _launch
        return _launch(profiles[0].name, list(passthrough_args), store)
