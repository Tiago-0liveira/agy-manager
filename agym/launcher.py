from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from .profiles import Profile


class AgyNotFound(RuntimeError):
    pass


def resolve_agy(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    path = shutil.which("agy", path=env.get("PATH"))
    if not path:
        raise AgyNotFound("could not find 'agy' on PATH")
    return Path(path).resolve()


def agy_version(agy_path: Path) -> str | None:
    try:
        proc = subprocess.run(
            [str(agy_path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "").strip()
    return text.splitlines()[0] if text else None


def build_profile_env(
    profile_home: Path,
    base_env: Mapping[str, str] | None = None,
    system: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    home = str(Path(profile_home).resolve())
    host_home = env.get("HOME") or env.get("USERPROFILE")

    env["GEMINI_FORCE_FILE_STORAGE"] = "true"
    env["HOME"] = home

    target_system = system or platform.system()
    if target_system == "Windows":
        home_path = Path(home)
        env["USERPROFILE"] = home
        drive, tail = os.path.splitdrive(home)
        if drive:
            env["HOMEDRIVE"] = drive
            env["HOMEPATH"] = tail or "\\"

    # Preserve the host's conventional Git global config without copying it into
    # the isolated home. Existing explicit GIT_CONFIG_GLOBAL always wins.
    if "GIT_CONFIG_GLOBAL" not in env and host_home:
        candidate = Path(host_home) / ".gitconfig"
        if candidate.is_file():
            env["GIT_CONFIG_GLOBAL"] = str(candidate)

    return env


def _normalized_args(args: Sequence[str]) -> list[str]:
    forwarded = list(args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    return forwarded


def run_agy(
    agy_path: Path,
    profile: Profile,
    args: Sequence[str] = (),
    *,
    replace_process: bool = False,
) -> int:
    env = build_profile_env(profile.home)
    argv = [str(agy_path), "--dangerously-skip-permissions", *_normalized_args(args)]

    # cwd=None deliberately preserves the caller's current working directory.
    # No stdio streams are captured, so the native TTY/TUI is inherited.
    if replace_process and os.name == "posix":
        os.execve(str(agy_path), argv, env)
        raise AssertionError("unreachable")

    completed = subprocess.run(argv, env=env, cwd=None, check=False)
    return completed.returncode


def persistent_profile_data_exists(profile: Profile) -> bool:
    gemini = profile.home / ".gemini"
    if not gemini.is_dir():
        return False
    try:
        return any(path.is_file() for path in gemini.rglob("*"))
    except OSError:
        return False
