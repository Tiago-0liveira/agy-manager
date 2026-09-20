from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .profiles import Profile, ProfileError


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


def build_agy_args(
    profile: Profile,
    operation_args: Sequence[str] = (),
    passthrough_args: Sequence[str] = (),
    *,
    agy_path: Path | str | None = None,
) -> list[str]:
    norm_op = _normalized_args(operation_args)
    norm_pass = _normalized_args(passthrough_args)
    combined = list(norm_op) + list(norm_pass)

    has_model = ("--model" in combined) or any(arg.startswith("--model=") for arg in combined)
    has_danger = "--dangerously-skip-permissions" in combined

    prefix_args: list[str] = []
    if not has_model and profile.settings.model and profile.settings.model.strip():
        prefix_args.extend(["--model", profile.settings.model.strip()])
    if not has_danger and profile.settings.dangerously_skip_permissions:
        prefix_args.append("--dangerously-skip-permissions")

    final_args: list[str] = []
    if agy_path is not None:
        final_args.append(str(agy_path))
    final_args.extend(prefix_args)
    final_args.extend(norm_op)
    final_args.extend(norm_pass)
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
) -> int:
    if profile.settings.validation_errors:
        raise ProfileError(
            f"invalid settings for profile '{profile.name}': {', '.join(profile.settings.validation_errors)}"
        )
    env = build_profile_env(profile.home)
    cmd_args = build_agy_args(profile, passthrough_args=args)
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
) -> int:
    if profile.settings.validation_errors:
        raise ProfileError(
            f"invalid settings for profile '{profile.name}': {', '.join(profile.settings.validation_errors)}"
        )
    if not user_prompt or not user_prompt.strip():
        print("agym: --auto-prompt requires a non-empty prompt", file=sys.stderr)
        return 1

    env = build_profile_env(profile.home)
    stage1_prompt = build_stage1_prompt(user_prompt)
    stage1_args = build_agy_args(profile, operation_args=["--prompt", stage1_prompt])

    spinner = Spinner(f"Generating implementation plan with profile '{profile.name}'")
    spinner.start()
    try:
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
    stage2_args = build_agy_args(profile, operation_args=["--prompt-interactive", stage2_prompt])
    return exec_agy_interactive(
        agy_path=agy_path,
        env=env,
        args=stage2_args,
        replace_process=replace_process,
    )


def persistent_profile_data_exists(profile: Profile) -> bool:
    gemini = profile.home / ".gemini"
    if not gemini.is_dir():
        return False
    try:
        return any(path.is_file() for path in gemini.rglob("*"))
    except OSError:
        return False
