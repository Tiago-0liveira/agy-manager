from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .profiles import Profile, ProfileError, _default_config_root, _default_data_root
from .wincred import has_profile_token, profile_credential_context

logger = logging.getLogger("agym.launcher")


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
            encoding="utf-8",
            errors="replace",
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
    profile_name: str | None = None,
) -> dict[str, str]:
    from .profiles import _default_config_root, _default_data_root

    env = dict(os.environ if base_env is None else base_env)
    home = str(Path(profile_home).resolve())
    host_home = env.get("HOME") or env.get("USERPROFILE")

    # Preserve agym config and data roots before overriding HOME, so that child
    # processes (like statusline scripts) can locate agym profiles and cache.
    if "AGYM_CONFIG_HOME" not in env:
        env["AGYM_CONFIG_HOME"] = str(_default_config_root())
    if "AGYM_DATA_HOME" not in env:
        env["AGYM_DATA_HOME"] = str(_default_data_root())

    env["GEMINI_FORCE_FILE_STORAGE"] = "true"
    env["HOME"] = home
    if profile_name:
        env["AGYM_PROFILE"] = profile_name

    target_system = system or platform.system()
    if target_system == "Windows":
        home_path = Path(home)
        env["USERPROFILE"] = home
        drive, tail = os.path.splitdrive(home)
        if drive:
            env["HOMEDRIVE"] = drive
            env["HOMEPATH"] = tail or "\\"

        # On Windows, redirect application and Chromium user data directories
        # to the isolated profile so Chrome/agy does not fall back to host %LOCALAPPDATA%
        local_appdata = (home_path / "AppData" / "Local").resolve()
        roaming_appdata = (home_path / "AppData" / "Roaming").resolve()
        env["LOCALAPPDATA"] = str(local_appdata)
        env["APPDATA"] = str(roaming_appdata)
        try:
            local_appdata.mkdir(parents=True, exist_ok=True)
            roaming_appdata.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # Ensure profile sessions retain access to the manager store
    if "AGYM_CONFIG_HOME" not in env:
        env["AGYM_CONFIG_HOME"] = str(_default_config_root())
    if "AGYM_DATA_HOME" not in env:
        env["AGYM_DATA_HOME"] = str(_default_data_root())

    # Preserve the host's conventional Git global config without copying it into
    # the isolated home. Existing explicit GIT_CONFIG_GLOBAL always wins.
    if "GIT_CONFIG_GLOBAL" not in env and host_home:
        candidate = Path(host_home) / ".gitconfig"
        if candidate.is_file():
            env["GIT_CONFIG_GLOBAL"] = str(candidate)

    # Preserve the host's GitHub CLI config for credentials and gh CLI operations.
    if "GH_CONFIG_DIR" not in env and host_home:
        candidate_gh = Path(host_home) / ".config" / "gh"
        if candidate_gh.is_dir():
            env["GH_CONFIG_DIR"] = str(candidate_gh)

    return env


LOCK_FILE_PATTERNS: tuple[str, ...] = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "lockfile",
    "parent.lock",
)


def cleanup_profile_locks(profile_home: Path | str) -> list[Path]:
    """Removes stale Chromium and profile lock files (SingletonLock, lockfile, etc.)
    that can cause browser launches to fail or bind to an existing zombie process on Windows.
    """
    removed: list[Path] = []
    base = Path(profile_home).resolve()
    if not base.exists():
        return removed

    search_dirs = [
        base,
        base / ".gemini",
        base / ".gemini" / "antigravity-cli",
        base / "AppData" / "Local",
        base / "AppData" / "Roaming",
        base / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
    ]

    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for lock_name in LOCK_FILE_PATTERNS:
            lock_path = directory / lock_name
            if lock_path.exists() or lock_path.is_symlink():
                try:
                    lock_path.unlink(missing_ok=True)
                    removed.append(lock_path)
                    logger.debug("Cleaned up stale lockfile: %s", lock_path)
                except OSError as exc:
                    logger.warning("Could not remove stale lockfile %s: %s", lock_path, exc)
    return removed


def build_browser_args(
    user_data_dir: Path | str,
    extra_args: Sequence[str] = (),
    profile_directory: str | None = None,
) -> list[str]:
    """Builds robust cross-platform Chromium/browser launch arguments.

    Ensures --user-data-dir is an absolute, resolved path with directory created,
    avoiding Windows backslash escaping and quote mishandling.
    """
    profile_path = Path(user_data_dir).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)
    args = [f"--user-data-dir={str(profile_path)}"]
    if profile_directory:
        args.append(f"--profile-directory={profile_directory}")
    args.extend(extra_args)
    return args


def terminate_process(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Cleanly terminates a subprocess, escalating from SIGTERM to SIGKILL if hanging."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _normalized_args(args: Sequence[str]) -> list[str]:
    forwarded = list(args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    return forwarded


DANGEROUS_SKIP_PERMISSIONS_ALIASES: frozenset[str] = frozenset({
    "-y",
    "--yes",
    "--dsp",
    "--skip-perms",
    "--dangerously-skip-permission",
    "--dangerously-skip-permissions",
})

NO_DANGEROUS_SKIP_PERMISSIONS_ALIASES: frozenset[str] = frozenset({
    "--no-dangerously-skip-permissions",
    "--no-dangerously-skip-permission",
    "--no-dsp",
    "--no-skip-perms",
})

ALL_PERMISSIONS_ALIASES: frozenset[str] = (
    DANGEROUS_SKIP_PERMISSIONS_ALIASES | NO_DANGEROUS_SKIP_PERMISSIONS_ALIASES
)


def resolve_env_dangerously_skip_permissions(env: Mapping[str, str] | None = None) -> bool | None:
    lookup = os.environ if env is None else env
    raw = lookup.get("DANGEROUSLY_SKIP_PERMISSIONS")
    if raw is None:
        raw = lookup.get("DSP")
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if cleaned in {"1", "true", "yes", "y", "on"}:
        return True
    if cleaned in {"0", "false", "no", "n", "off", ""}:
        return False
    return None


def resolve_cli_dangerously_skip_permissions(args: Sequence[str]) -> bool | None:
    result = None
    for arg in args:
        if arg in DANGEROUS_SKIP_PERMISSIONS_ALIASES:
            result = True
        elif arg in NO_DANGEROUS_SKIP_PERMISSIONS_ALIASES:
            result = False
    return result


def resolve_dangerously_skip_permissions(
    args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    profile_default: bool = False,
) -> bool:
    cli_val = resolve_cli_dangerously_skip_permissions(args)
    if cli_val is not None:
        return cli_val
    env_val = resolve_env_dangerously_skip_permissions(env)
    if env_val is not None:
        return env_val
    return profile_default


def build_agy_args(
    profile: Profile,
    operation_args: Sequence[str] = (),
    passthrough_args: Sequence[str] = (),
    *,
    agy_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    norm_op = _normalized_args(operation_args)
    norm_pass = _normalized_args(passthrough_args)
    combined = list(norm_op) + list(norm_pass)

    has_model = ("--model" in combined) or any(arg.startswith("--model=") for arg in combined)
    effective_danger = resolve_dangerously_skip_permissions(
        combined,
        env=env,
        profile_default=profile.settings.dangerously_skip_permissions,
    )

    filtered_op = [arg for arg in norm_op if arg not in ALL_PERMISSIONS_ALIASES]
    filtered_pass = [arg for arg in norm_pass if arg not in ALL_PERMISSIONS_ALIASES]

    prefix_args: list[str] = []
    if not has_model and profile.settings.model and profile.settings.model.strip():
        prefix_args.extend(["--model", profile.settings.model.strip()])
    if effective_danger:
        prefix_args.append("--dangerously-skip-permissions")

    final_args: list[str] = []
    if agy_path is not None:
        final_args.append(str(agy_path))
    final_args.extend(prefix_args)
    final_args.extend(filtered_op)
    final_args.extend(filtered_pass)
    return final_args


def run_agy_capture(
    agy_path: Path,
    env: Mapping[str, str],
    args: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    argv = [str(agy_path), *_normalized_args(args)]
    return subprocess.run(
        argv,
        env=dict(env),
        cwd=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def exec_agy_interactive(
    agy_path: Path,
    env: Mapping[str, str],
    args: Sequence[str],
    *,
    replace_process: bool = True,
) -> int:
    argv = [str(agy_path), *_normalized_args(args)]
    if replace_process and os.name == "posix":
        os.execve(str(agy_path), argv, dict(env))
        return 0

    completed = subprocess.run(argv, env=dict(env), cwd=None, check=False)
    return completed.returncode


def run_agy(
    agy_path: Path,
    profile: Profile,
    args: Sequence[str] = (),
    *,
    replace_process: bool = False,
    is_setup: bool = False,
) -> int:
    if profile.settings.validation_errors:
        raise ProfileError(
            f"invalid settings for profile '{profile.name}': {', '.join(profile.settings.validation_errors)}"
        )
    cleanup_profile_locks(profile.home)
    env = build_profile_env(profile.home, profile_name=profile.name)
    cmd_args = build_agy_args(profile, passthrough_args=args, env=env)
    with profile_credential_context(profile.home, is_setup=is_setup):
        return exec_agy_interactive(
            agy_path=agy_path,
            env=env,
            args=cmd_args,
            replace_process=replace_process,
        )


def build_stage1_prompt(user_prompt: str) -> str:
    return (
        "You are an expert software engineer and architect.\n"
        "Create a well-structured, elaborate, and actionable implementation plan in plain text "
        "for the following user request. Detail the exact file changes, design choices, and "
        "step-by-step implementation instructions so that an AI coding agent can execute it right away.\n"
        "Output ONLY the implementation plan in plain text. Do not execute tools or modify files yet.\n\n"
        f"User Request:\n{user_prompt.strip()}"
    )


def build_stage2_prompt(plan: str) -> str:
    return (
        "Please implement the following plan right away. Follow the steps, make all necessary "
        "file modifications or additions, run relevant tests to verify your changes, and summarize "
        "what you did when finished so I can review and test:\n\n"
        f"{plan.strip()}"
    )


class Spinner:
    def __init__(self, message: str, stream: Any = None) -> None:
        self.message = message
        self.stream = stream if stream is not None else sys.stderr
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.is_tty = hasattr(self.stream, "isatty") and self.stream.isatty()

    def _spin(self) -> None:
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        idx = 0
        start_time = time.time()
        while not self._stop_event.is_set():
            if self.is_tty:
                elapsed = int(time.time() - start_time)
                frame = frames[idx % len(frames)]
                self.stream.write(f"\r\033[K{frame} {self.message}... ({elapsed}s)")
                self.stream.flush()
                idx += 1
            self._stop_event.wait(0.08)

    def start(self) -> None:
        if not self.is_tty:
            self.stream.write(f"agym: {self.message}...\n")
            self.stream.flush()
            return
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self, status: str | None = None) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self.is_tty:
            self.stream.write("\r\033[K")
            if status:
                self.stream.write(f"{status}\n")
            self.stream.flush()
        elif status:
            self.stream.write(f"agym: {status}\n")
            self.stream.flush()

    def __enter__(self) -> Spinner:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


def run_auto_prompt(
    agy_path: Path,
    profile: Profile,
    user_prompt: str,
    *,
    replace_process: bool = True,
    extra_args: Sequence[str] = (),
) -> int:
    if profile.settings.validation_errors:
        raise ProfileError(
            f"invalid settings for profile '{profile.name}': {', '.join(profile.settings.validation_errors)}"
        )
    if not user_prompt or not user_prompt.strip():
        print("agym: --auto-prompt requires a non-empty prompt", file=sys.stderr)
        return 1

    cleanup_profile_locks(profile.home)
    env = build_profile_env(profile.home, profile_name=profile.name)
    stage1_prompt = build_stage1_prompt(user_prompt)
    stage1_args = build_agy_args(
        profile,
        operation_args=["--prompt", stage1_prompt],
        passthrough_args=extra_args,
        env=env,
    )

    spinner = Spinner(f"Generating implementation plan with profile '{profile.name}'")
    spinner.start()
    try:
        with profile_credential_context(profile.home):
            proc = run_agy_capture(agy_path=agy_path, env=env, args=stage1_args)
    finally:
        spinner.stop()

    if proc.returncode != 0:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
            if not proc.stderr.endswith("\n"):
                sys.stderr.write("\n")
        print(f"agym: agy --prompt failed with exit code {proc.returncode}", file=sys.stderr)
        return proc.returncode

    raw_response = proc.stdout or ""
    if not raw_response or not raw_response.strip():
        if proc.stderr:
            sys.stderr.write(proc.stderr)
            if not proc.stderr.endswith("\n"):
                sys.stderr.write("\n")
        print("agym: agy --prompt returned empty response", file=sys.stderr)
        return 1

    if hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
        sys.stderr.write("✓ Plan generated. Opening interactive implementation session...\n\n")
        sys.stderr.flush()
    else:
        sys.stderr.write("agym: Plan generated. Opening interactive implementation session...\n")
        sys.stderr.flush()

    stage2_prompt = build_stage2_prompt(raw_response)
    stage2_args = build_agy_args(
        profile,
        operation_args=["--prompt-interactive", stage2_prompt],
        passthrough_args=extra_args,
        env=env,
    )
    with profile_credential_context(profile.home):
        return exec_agy_interactive(
            agy_path=agy_path,
            env=env,
            args=stage2_args,
            replace_process=replace_process,
        )


def persistent_profile_data_exists(profile: Profile) -> bool:
    if has_profile_token(profile.home):
        return True
    gemini = profile.home / ".gemini"
    if not gemini.is_dir():
        return False
    try:
        return any(path.is_file() for path in gemini.rglob("*"))
    except OSError:
        return False
