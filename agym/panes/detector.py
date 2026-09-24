from __future__ import annotations

import os
import platform
import shutil
from typing import Callable, Mapping

from .backends import (
    TerminalPaneBackend,
    TmuxActiveBackend,
    TmuxManagedBackend,
    WezTermBackend,
    WindowsTerminalBackend,
)


def detect_terminal_name(environ: Mapping[str, str]) -> str:
    """Infers the name of the current terminal emulator from environment signals."""
    if environ.get("WT_SESSION"):
        return "Windows Terminal"
    if environ.get("TMUX"):
        return "tmux"
    if environ.get("WEZTERM_PANE") or environ.get("TERM_PROGRAM") == "WezTerm":
        return "WezTerm"
    if environ.get("KITTY_WINDOW_ID"):
        return "Kitty"
    if environ.get("ALACRITTY_LOG"):
        return "Alacritty"
    term_program = environ.get("TERM_PROGRAM")
    if term_program:
        return term_program
    term = environ.get("TERM")
    if term:
        return f"terminal ({term})"
    return "unknown terminal"


def detect_backend(
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> TerminalPaneBackend | None:
    """Detects and returns the best available terminal pane backend.
    
    Priority:
    1. Active tmux session ($TMUX present).
    2. Windows Terminal (native backend when $WT_SESSION present on Windows).
    3. WezTerm (when $WEZTERM_PANE present and wezterm binary found).
    4. Managed tmux fallback on Linux/macOS when tmux binary is installed.
    5. None if no supported backend is available.
    """
    os_name = system or platform.system()
    env = os.environ if environ is None else environ
    which_fn = which or (lambda cmd: shutil.which(cmd, path=env.get("PATH")))

    # 1. Active tmux session takes top priority across platforms
    if env.get("TMUX"):
        return TmuxActiveBackend()

    # 2. Windows Terminal native backend on Windows
    if os_name == "Windows" and env.get("WT_SESSION"):
        return WindowsTerminalBackend()

    # 3. WezTerm backend if running inside WezTerm
    if env.get("WEZTERM_PANE") and which_fn("wezterm"):
        return WezTermBackend()

    # 4. Managed tmux fallback on Linux and macOS
    if os_name in ("Linux", "Darwin") and which_fn("tmux"):
        return TmuxManagedBackend()

    return None


def get_unsupported_message(
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Generates a detailed, informative error message when no pane backend is available."""
    os_name = system or platform.system()
    env = os.environ if environ is None else environ
    term_name = detect_terminal_name(env)

    lines = [
        "agym all: unable to create terminal panes in the current terminal.",
        "",
        "Environment:",
        f"  OS:       {os_name}",
        f"  Terminal: {term_name}",
        "",
        f"Supported options on {os_name}:",
    ]

    if os_name == "Windows":
        lines.extend([
            "  - Windows Terminal (native pane splitting via wt.exe)",
            "    Run 'agym all' from inside a Windows Terminal tab/window.",
        ])
    elif os_name == "Darwin":
        lines.extend([
            "  - tmux (run inside an existing tmux session, or install tmux via 'brew install tmux')",
            "  - WezTerm (native pane splitting via wezterm cli)",
        ])
    else:  # Linux and other POSIX
        lines.extend([
            "  - tmux (run inside an existing tmux session, or install tmux via your package manager)",
            "  - WezTerm (native pane splitting via wezterm cli)",
        ])

    lines.extend([
        "",
        "Note: agym does not attempt brittle GUI or keyboard automation.",
    ])

    return "\n".join(lines)
