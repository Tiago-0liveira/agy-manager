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
  agym all [options] [--] [agy args...]

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
  -n, --count <N>                     Limit number of profiles to launch (e.g. -n 4 for 2x2 grid)
  --profiles <p1,p2,...>              Launch specific profile subset by name
  -C, --cwd, --dir <path>             Working directory to open panes in (default: current dir)
  -- [agy args...]                    Pass trailing arguments directly to each profile
                                      (e.g. agym all --dsp, agym all -C /repo -- -p "explain")
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
    import os
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    if any(arg in {"-h", "--help", "help"} for arg in argv):
        out.write(HELP_TEXT)
        out.flush()
        return 0

    # Separate agym all options from trailing passthrough if '--' is used
    if "--" in argv:
        idx = argv.index("--")
        all_argv = argv[:idx]
        passthrough = list(argv[idx + 1:])
    else:
        all_argv = list(argv)
        passthrough = []

    cwd_arg: str | None = None
    count_arg: int | None = None
    profiles_arg: str | None = None
    rest_args: list[str] = []

    i = 0
    while i < len(all_argv):
        arg = all_argv[i]
        if arg in ("-C", "--cwd", "--dir"):
            if i + 1 >= len(all_argv):
                err.write(f"agym all: option '{arg}' requires a directory path\n")
                err.flush()
                return 2
            cwd_arg = all_argv[i + 1]
            i += 2
        elif arg.startswith(("-C=", "--cwd=", "--dir=")):
            cwd_arg = arg.split("=", 1)[1]
            i += 1
        elif arg in ("-n", "--count", "--limit"):
            if i + 1 >= len(all_argv):
                err.write(f"agym all: option '{arg}' requires an integer count\n")
                err.flush()
                return 2
            val = all_argv[i + 1]
            try:
                count_arg = int(val)
                if count_arg <= 0:
                    raise ValueError
            except ValueError:
                err.write(f"agym all: invalid count '{val}': must be a positive integer\n")
                err.flush()
                return 2
            i += 2
        elif arg.startswith(("-n=", "--count=", "--limit=")):
            val = arg.split("=", 1)[1]
            try:
                count_arg = int(val)
                if count_arg <= 0:
                    raise ValueError
            except ValueError:
                err.write(f"agym all: invalid count '{val}': must be a positive integer\n")
                err.flush()
                return 2
            i += 1
        elif arg in ("--profiles",):
            if i + 1 >= len(all_argv):
                err.write(f"agym all: option '{arg}' requires a comma-separated list of profile names\n")
                err.flush()
                return 2
            profiles_arg = all_argv[i + 1]
            i += 2
        elif arg.startswith("--profiles="):
            profiles_arg = arg.split("=", 1)[1]
            i += 1
        else:
            rest_args.append(arg)
            i += 1

    passthrough = rest_args + passthrough

    # Resolve working directory
    if cwd_arg is not None:
        target_dir = Path(cwd_arg).expanduser()
        if not target_dir.is_dir():
            err.write(f"agym all: directory not found: {cwd_arg}\n")
            err.flush()
            return 2
        working_dir = target_dir.resolve()
    elif cwd is not None:
        working_dir = Path(cwd).resolve()
    else:
        working_dir = Path.cwd()

    all_profiles = store.list()
    if not all_profiles:
        out.write("No profiles configured. Run 'agym setup <profile>' first.\n")
        out.flush()
        return 0

    if profiles_arg:
        names = [n.strip() for n in profiles_arg.split(",") if n.strip()]
        name_map = {p.name: p for p in all_profiles}
        missing = [n for n in names if n not in name_map]
        if missing:
            err.write(f"agym all: profile(s) not found: {', '.join(missing)}\n")
            err.flush()
            return 2
        profiles = [name_map[n] for n in names]
    else:
        profiles = list(all_profiles)

    if count_arg is not None:
        profiles = profiles[:count_arg]

    if not profiles:
        err.write("agym all: no profiles selected to launch\n")
        err.flush()
        return 2

    if len(profiles) == 1:
        single_profile = profiles[0]
        if cwd_arg is not None:
            try:
                os.chdir(working_dir)
            except OSError:
                pass
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
