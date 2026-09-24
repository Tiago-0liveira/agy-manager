from __future__ import annotations

from .backends import (
    TMUX_SPLIT_FLAGS,
    WEZTERM_SPLIT_FLAGS,
    WT_SPLIT_FLAGS,
    BackendError,
    TerminalPaneBackend,
    TmuxActiveBackend,
    TmuxManagedBackend,
    WezTermBackend,
    WindowsTerminalBackend,
    build_profile_command,
)
from .detector import detect_backend, detect_terminal_name, get_unsupported_message
from .layout import (
    LayoutPlan,
    PaneInfo,
    Rect,
    SplitDirection,
    SplitStep,
    calculate_layout,
)
from .runner import run_all

__all__ = [
    "BackendError",
    "LayoutPlan",
    "PaneInfo",
    "Rect",
    "SplitDirection",
    "SplitStep",
    "TMUX_SPLIT_FLAGS",
    "TerminalPaneBackend",
    "TmuxActiveBackend",
    "TmuxManagedBackend",
    "WEZTERM_SPLIT_FLAGS",
    "WT_SPLIT_FLAGS",
    "WezTermBackend",
    "WindowsTerminalBackend",
    "build_profile_command",
    "calculate_layout",
    "detect_backend",
    "detect_terminal_name",
    "get_unsupported_message",
    "run_all",
]
