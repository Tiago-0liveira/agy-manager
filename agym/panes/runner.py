from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from ..profiles import ProfileStore
from .backends import BackendError, TerminalPaneBackend
from .detector import detect_backend, get_unsupported_message
from .layout import calculate_layout

HELP_TEXT = """Usage:
  agym all [--] [agy args...]

Description:
  Launch every configured Antigravity profile simultaneously, with one
  profile per terminal pane arranged as evenly as practical within the
  current terminal window.

Supported backends:
  - Windows: Windows Terminal (native wt.exe split-pane in current tab/window)
  - Linux:   tmux (active session or automatic managed session fallback), WezTerm
  - macOS:   tmux (active session or automatic managed session fallback), WezTerm

Options:
  -h, --help                          Show this help message and exit
  -- [agy args...]                    Pass trailing arguments directly to each profile
"""


def run_all(
    argv: list[str],
    store: ProfileStore,
    *,
    cwd: Path | str | None = None,
    backend: TerminalPaneBackend | None = None,
    launcher_fn: Callable[..., int] | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Executes the `agym all` command."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    if any(arg in {"-h", "--help", "help"} for arg in argv):
        out.write(HELP_TEXT)
        out.flush()
        return 0

    # Parse passthrough arguments
    passthrough: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        passthrough = list(argv[idx + 1:])
    else:
        passthrough = list(argv)

    profiles = store.list()
    if not profiles:
        out.write("No profiles configured. Run 'agym setup <profile>' first.\n")
        out.flush()
        return 0

    if len(profiles) == 1:
        single_profile = profiles[0]
        if launcher_fn is not None:
            return launcher_fn(single_profile.name, passthrough, store)
        from ..cli import _launch
        return _launch(single_profile.name, passthrough, store)

    # Detect or use provided backend
    active_backend = backend if backend is not None else detect_backend()
    if active_backend is None:
        err.write(get_unsupported_message() + "\n")
        err.flush()
        return 1

    plan = calculate_layout(len(profiles))
    working_dir = Path(cwd) if cwd is not None else Path.cwd()

    try:
        return active_backend.launch_all(
            profiles=profiles,
            passthrough_args=passthrough,
            plan=plan,
            cwd=working_dir,
            store=store,
            launcher_fn=launcher_fn,
        )
    except BackendError as exc:
        err.write(f"agym all: {exc}\n")
        err.flush()
        return 1
