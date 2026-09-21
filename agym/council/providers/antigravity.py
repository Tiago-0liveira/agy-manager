"""Headless Antigravity CLI Provider Adapter for AGYM Council.

Executes Google's official Antigravity CLI binary (`agy`) in headless non-interactive mode.
Features:
- Subprocess argv formatting respecting Go flag parser rules (["--print", prompt] at the end).
- Separate stdout and stderr stream processing, isolating model discovery tab-separated lines
  from braille spinner frames.
- Three-tier non-billable account verification probe (ProfileStore, .gemini directory, agy models exit code 0).
- Asynchronous NDJSON stream parsing for progressive turn events, conversation handle tracking,
  and token usage extraction.
- Multi-strategy output section extractor (direct JSON, markdown code fence, markdown H2/H3 headings, fallback).
- Process-tree lifecycle management (POSIX session leader via start_new_session and os.killpg;
  Windows process groups via CREATE_NEW_PROCESS_GROUP and taskkill).
- Startup crash reconciliation with OS PID and start time validation preventing PID recycling traps.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from agym.launcher import (
    AgyNotFound,
    agy_version,
    build_profile_env,
    cleanup_profile_locks,
    persistent_profile_data_exists,
    resolve_agy,
    resolve_dangerously_skip_permissions,
)
from agym.profiles import ProfileNotFound, ProfileStore
from agym.council.models import (
    AccountAuthStatus,
    AccountStatus,
    AttemptReconciliation,
    AttemptSnapshot,
    AttemptStatus,
    CouncilOutputSections,
    ModelDescriptor,
    ProviderCapabilities,
    TurnEvent,
    TurnRequest,
    TurnResult,
    TurnStatus,
)
from agym.council.providers.base import ProviderAdapter


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_pid_alive(pid: int | None) -> bool:
    """Return True if OS process with given PID exists and is running."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def get_process_start_time(pid: int) -> float | None:
    """Extract OS process start time as Unix epoch float without external dependencies."""
    if pid <= 0:
        return None

    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
                content = f.read()
            r_idx = content.rfind(")")
            if r_idx == -1:
                return None
            fields = content[r_idx + 2 :].split()
            # In /proc/[pid]/stat, field 3 is fields[0]. Field 22 (starttime) is fields[19].
            starttime_ticks = int(fields[19])

            btime = 0
            try:
                with open("/proc/stat", "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("btime "):
                            btime = int(line.split()[1])
                            break
            except Exception:
                pass

            ticks_per_sec = 100
            try:
                ticks_per_sec = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
            except Exception:
                pass

            return btime + (starttime_ticks / ticks_per_sec)
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
            try:
                return os.stat(f"/proc/{pid}").st_mtime
            except Exception:
                return None

    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-p", str(pid), "-o", "etimes="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            val = (proc.stdout or "").strip()
            if val and val.isdigit():
                return time.time() - float(val)
        except Exception:
            pass
        return None

    elif sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h_proc = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not h_proc:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_t = wintypes.FILETIME()
                kernel_t = wintypes.FILETIME()
                user_t = wintypes.FILETIME()
                if kernel32.GetProcessTimes(
                    h_proc,
                    ctypes.byref(creation),
                    ctypes.byref(exit_t),
                    ctypes.byref(kernel_t),
                    ctypes.byref(user_t),
                ):
                    ft_int = (creation.dwHighDateTime << 32) + creation.dwLowDateTime
                    return (ft_int - 116444736000000000) / 10000000.0
            finally:
                kernel32.CloseHandle(h_proc)
        except Exception:
            pass
        return None

    return None


def get_subprocess_creation_kwargs() -> dict[str, Any]:
    """Return platform-specific subprocess creation kwargs for process group isolation."""
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags |= subprocess.CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}


async def terminate_child_process(
    proc: asyncio.subprocess.Process, grace_period: float = 1.5
) -> None:
    """Terminate a managed asyncio subprocess and await reap to prevent zombies."""
    if proc.returncode is not None:
        return

    pid = proc.pid
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await proc.wait()
        except Exception:
            pass
        return

    # POSIX: Send SIGTERM to process group
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_period)
    except asyncio.TimeoutError:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            await proc.wait()
        except Exception:
            pass


async def terminate_pid_tree(pid: int, grace_period: float = 1.5) -> None:
    """Terminate an external or orphaned process tree cleanly without calling waitpid."""
    if not is_pid_alive(pid):
        return

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
            )
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        return

    # POSIX: Send SIGTERM to process group
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return

    # Poll liveness up to grace period
    deadline = time.time() + grace_period
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return
        await asyncio.sleep(0.05)

    # Escalate to SIGKILL
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def build_turn_argv(
    agy_path: Path | str,
    prompt: str,
    model: str | None = None,
    conversation_handle: str | None = None,
    dangerously_skip_permissions: bool = False,
    output_format: str = "stream-json",
) -> list[str]:
    """Construct exact argv array for headless agy execution adhering to Go flag rules.

    The `--print` flag requires an argument in Go flag parsing and consumes the next token.
    Therefore, `--print` must always be placed at the END of the argument list, followed
    immediately by the prompt string.
    """
    argv: list[str] = [str(agy_path), "--output-format", output_format]
    if model and model.strip():
        argv.extend(["--model", model.strip()])
    if conversation_handle and conversation_handle.strip():
        argv.extend(["--conversation", conversation_handle.strip()])
    if dangerously_skip_permissions:
        argv.append("--dangerously-skip-permissions")
    # Must be at the end:
    argv.extend(["--print", prompt])
    return argv


def sanitize_auth_error(stderr_text: str, account_ref: str) -> str:
    """Sanitize CLI error output to prevent credential or secret leakage."""
    cleaned = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", stderr_text)
    cleaned = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]+", r"\1[REDACTED]", cleaned)
    cleaned = re.sub(
        r"(?i)\b(token|code|key|secret|password|credential|access_token|refresh_token)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        cleaned,
    )
    cleaned = re.sub(r"(?i)([?&](?:token|code|key|secret)=)[^&\s]+", r"\1[REDACTED]", cleaned)

    lines = [
        line.strip()
        for line in cleaned.splitlines()
        if line.strip()
        and not any(ch in line for ch in ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"))
    ]
    if not lines:
        return f"Authentication required for profile '{account_ref}'. Run 'agym setup {account_ref}'."

    for line in lines:
        if any(term in line.lower() for term in ("sign in", "login", "error", "unauthenticated", "auth")):
            return line[:200]

    return lines[0][:200]


def extract_output_sections(
    text: str,
) -> tuple[CouncilOutputSections | None, dict[str, Any] | None]:
    """Extract CouncilOutputSections using multi-strategy parsing.

    Strategies attempted in order:
    1. Direct JSON parsing of the entire text.
    2. Markdown fenced code blocks (```json ... ``` or ``` ... ```).
    3. Markdown headings (e.g. ## Findings, ## Evidence or Assumptions, etc.).
    4. Fallback: treat entire text as the 'findings' section.
    """
    if not text or not text.strip():
        return None, None

    cleaned = text.strip()

    # Strategy 1: Direct JSON parsing
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            try:
                sections = CouncilOutputSections.model_validate(data)
                return sections, data
            except Exception:
                pass
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Markdown fenced code blocks
    code_block_pattern = re.compile(
        r"```(?:json)?\s*([\s\S]*?)\s*```",
        re.IGNORECASE,
    )
    for match in code_block_pattern.finditer(cleaned):
        block_content = match.group(1).strip()
        try:
            data = json.loads(block_content)
            if isinstance(data, dict):
                try:
                    sections = CouncilOutputSections.model_validate(data)
                    return sections, data
                except Exception:
                    pass
        except (json.JSONDecodeError, ValueError):
            continue

    # Strategy 3: Markdown Headings (## Findings, ## Evidence or Assumptions, etc.)
    heading_pattern = re.compile(r"^(#{1,4})\s+([^\n]+)", re.MULTILINE)
    matches = list(heading_pattern.finditer(cleaned))
    if matches:
        sections_dict: dict[str, Any] = {}
        for i, m in enumerate(matches):
            raw_title = m.group(2).strip()
            key = re.sub(r"[^a-zA-Z0-9]+", "_", raw_title.lower()).strip("_")
            if key in ("evidence", "assumptions", "evidence_assumptions"):
                key = "evidence_or_assumptions"
            elif key in ("uncertainty", "risks", "dissent", "objections"):
                key = "uncertainties"
            elif key in ("next_steps", "next_actions", "action", "recommendation"):
                key = "next_action"

            start_pos = m.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
            section_content = cleaned[start_pos:end_pos].strip()
            sections_dict[key] = section_content

        std_keys = {"findings", "evidence_or_assumptions", "uncertainties", "next_action"}
        if any(k in sections_dict for k in std_keys):
            try:
                sections = CouncilOutputSections.model_validate(sections_dict)
                return sections, sections_dict
            except Exception:
                pass

    # Strategy 4: Fallback - treat entire text as findings
    fallback_dict = {
        "findings": cleaned,
        "evidence_or_assumptions": "",
        "uncertainties": "",
        "next_action": "",
    }
    try:
        sections = CouncilOutputSections.model_validate(fallback_dict)
        return sections, fallback_dict
    except Exception:
        return None, None


class AntigravityProviderAdapter(ProviderAdapter):
    """Headless Antigravity CLI adapter executing the official `agy` binary."""

    def __init__(
        self,
        profile_store: ProfileStore | None = None,
        agy_path: Path | str | None = None,
    ) -> None:
        self.profile_store = profile_store or ProfileStore()
        self.agy_path = Path(agy_path).resolve() if agy_path else None
        self.active_processes: dict[str, asyncio.subprocess.Process] = {}
        self.active_tasks: dict[str, asyncio.Task[Any]] = {}
        self.completed_attempts: dict[str, TurnResult] = {}
        self.cancelled_attempts: set[str] = set()
        self.process_start_times: dict[str, float] = {}

    def _resolve_agy_path(self, environ: Mapping[str, str] | None = None) -> Path:
        """Resolve agy binary path using explicit instance setting or PATH lookup."""
        if self.agy_path is not None:
            if not self.agy_path.is_file():
                raise AgyNotFound(f"Specified agy binary does not exist: {self.agy_path}")
            return self.agy_path
        return resolve_agy(environ=environ)

    # -----------------------------------------------------------------------
    # 1. capabilities
    # -----------------------------------------------------------------------

    async def capabilities(self, account_ref: str | None = None) -> ProviderCapabilities:
        """Return provider capabilities for an account or generic capabilities."""
        return ProviderCapabilities(
            provider_name="antigravity",
            structured_output=True,
            resume_conversation=True,
            cancellation=True,
            tool_execution=False,
            token_usage=True,
            model_discovery=True,
            access_restrictions=False,
            supports_streaming=True,
            supports_structured_output=True,
            supports_resume=True,
            supports_cancellation=True,
            supports_tools=False,
            supports_usage_metrics=True,
            supports_model_discovery=True,
            enforces_workspace_isolation=False,
        )

    # -----------------------------------------------------------------------
    # 2. check_account
    # -----------------------------------------------------------------------

    async def check_account(self, account_ref: str) -> AccountStatus:
        """Probe account readiness without leaking secret credentials or auth codes.

        Three-tier non-billable auth probe:
        - Tier 1: Look up profile in ProfileStore. Resolve `agy` executable path.
        - Tier 2: Check for `.gemini` directory in profile home.
        - Tier 3: Run `[agy_path, "models"]` with profile env. Exit code 0 indicates READY.
        """
        if not account_ref or not str(account_ref).strip():
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.UNAVAILABLE,
                message="Account reference is empty",
                details="Account reference is empty",
            )

        # Tier 1: ProfileStore check
        try:
            profile = self.profile_store.get(account_ref)
        except ProfileNotFound:
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.UNAVAILABLE,
                message=f"Profile '{account_ref}' not found",
                details=f"Profile '{account_ref}' not found",
            )
        except Exception as exc:
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.UNAVAILABLE,
                message=f"Error accessing profile store: {exc}",
                details=str(exc),
            )

        # Tier 1b: Binary existence check
        try:
            agy_path = self._resolve_agy_path()
        except AgyNotFound:
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.UNAVAILABLE,
                message="The 'agy' CLI binary was not found on PATH",
                details="The 'agy' CLI binary was not found on PATH",
            )

        # Tier 2: Credential directory check
        if not persistent_profile_data_exists(profile):
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.NEEDS_LOGIN,
                message=f"Profile '{account_ref}' requires sign-in: .gemini credentials not found. Run 'agym setup {account_ref}' to log in.",
                details="Credentials directory missing or empty",
            )

        # Tier 3: Non-billable CLI model catalog probe
        env = build_profile_env(profile.home, profile_name=profile.name)
        try:
            proc = await asyncio.create_subprocess_exec(
                str(agy_path),
                "models",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.UNKNOWN,
                message="Authentication probe timed out after 15 seconds",
                details="Timed out executing 'agy models'",
            )
        except Exception as exc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.UNAVAILABLE,
                message=f"Failed to execute authentication probe: {exc}",
                details=str(exc),
            )

        if proc.returncode == 0:
            ver = agy_version(agy_path) or "1.2.7"
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.READY,
                message=f"Profile '{account_ref}' is authenticated and ready",
                cli_version=ver,
                details=f"Ready with CLI version {ver}",
            )
        else:
            raw_stderr = stderr_bytes.decode("utf-8", errors="replace")
            sanitized = sanitize_auth_error(raw_stderr, account_ref)
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.NEEDS_LOGIN,
                message=sanitized,
                details=sanitized,
            )

    # -----------------------------------------------------------------------
    # 3. discover_models
    # -----------------------------------------------------------------------

    async def discover_models(self, account_ref: str) -> list[ModelDescriptor]:
        """Query and return the catalog of available models for the account.

        Executes `agy models` under the profile environment and parses stdout
        tab-separated records (`<model_id>\t<display_name>`), strictly isolating
        stderr spinner frames.
        """
        if not account_ref or not str(account_ref).strip():
            return []

        try:
            profile = self.profile_store.get(account_ref)
        except Exception:
            return []

        try:
            agy_path = self._resolve_agy_path()
        except Exception:
            return []

        env = build_profile_env(profile.home, profile_name=profile.name)
        try:
            proc = await asyncio.create_subprocess_exec(
                str(agy_path),
                "models",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except Exception:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return []

        if proc.returncode != 0:
            return []

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        models: list[ModelDescriptor] = []
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "\t" in line:
                parts = line.split("\t", 1)
                model_id = parts[0].strip()
                display_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else model_id
            else:
                parts = line.split(None, 1)
                model_id = parts[0].strip()
                display_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else model_id

            if not model_id:
                continue

            caps = ["structured_output", "streaming"]
            model_lower = model_id.lower()
            display_lower = display_name.lower()
            if "thinking" in model_lower or "thinking" in display_lower:
                caps.append("thinking")
            if "tools" in model_lower or "tool" in model_lower:
                caps.append("tools")

            context_window = 128000
            if "gemini" in model_lower:
                context_window = 1000000
            elif "claude" in model_lower:
                context_window = 200000

            models.append(
                ModelDescriptor(
                    id=model_id,
                    display_name=display_name,
                    provider="antigravity",
                    context_window=context_window,
                    capabilities=caps,
                )
            )
        return models

    # -----------------------------------------------------------------------
    # 4. run_turn
    # -----------------------------------------------------------------------

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent | TurnResult]:
        """Execute a worker turn as an asynchronous stream of progressive events and result."""
        start_time = _utc_now_iso()

        # 1. Pre-cancellation check
        if request.attempt_id in self.cancelled_attempts:
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.CANCELLED,
                error_message="Attempt cancelled before execution",
                error_code="CANCELLED",
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            yield res
            return

        # 2. Prompt validation
        if not request.prompt or not str(request.prompt).strip():
            yield TurnEvent(
                attempt_id=request.attempt_id,
                sequence=1,
                event_type="started",
                payload={"worker_id": request.worker_id, "model": request.model},
            )
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.MALFORMED_OUTPUT,
                error_message="Empty prompt is not allowed by Antigravity CLI",
                error_code="EMPTY_PROMPT",
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            yield res
            return

        # 3. Profile resolution
        try:
            profile = self.profile_store.get(request.account_ref)
        except ProfileNotFound:
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.UNAVAILABLE,
                error_message=f"Profile '{request.account_ref}' not found",
                error_code="PROFILE_NOT_FOUND",
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            yield res
            return
        except Exception as exc:
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.UNAVAILABLE,
                error_message=f"Failed to access profile store: {exc}",
                error_code="PROFILE_STORE_ERROR",
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            yield res
            return

        # 4. Binary resolution
        try:
            agy_path = self._resolve_agy_path()
        except AgyNotFound:
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.UNAVAILABLE,
                error_message="could not find 'agy' on PATH",
                error_code="AGY_NOT_FOUND",
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            yield res
            return

        # 5. Environment & CWD setup
        cleanup_profile_locks(profile.home)
        env = build_profile_env(profile.home, profile_name=profile.name)
        cwd = request.working_directory or str(profile.home)
        Path(cwd).mkdir(parents=True, exist_ok=True)

        dangerously_skip = resolve_dangerously_skip_permissions(
            env=env,
            profile_default=profile.settings.dangerously_skip_permissions,
        )
        if request.permissions.allow_command_execution or request.permissions.allow_workspace_write:
            dangerously_skip = True

        conv_handle = request.conversation_handle or request.conversation_id

        # 6. Argv construction adhering to Go flag parser rules
        cmd = build_turn_argv(
            agy_path=agy_path,
            prompt=request.prompt,
            model=request.model,
            conversation_handle=conv_handle,
            dangerously_skip_permissions=dangerously_skip,
            output_format="stream-json",
        )

        creation_kwargs = get_subprocess_creation_kwargs()

        # 7. Subprocess spawning
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=env,
                cwd=cwd,
                **creation_kwargs,
            )
        except Exception as exc:
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.UNAVAILABLE,
                error_message=f"Failed to spawn agy subprocess: {exc}",
                error_code="SPAWN_ERROR",
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            yield res
            return

        self.active_processes[request.attempt_id] = proc
        start_ts = get_process_start_time(proc.pid) or time.time()
        self.process_start_times[request.attempt_id] = start_ts

        # 8. Emit started event
        seq = 1
        yield TurnEvent(
            attempt_id=request.attempt_id,
            sequence=seq,
            event_type="started",
            payload={
                "worker_id": request.worker_id,
                "model": request.model,
                "pid": proc.pid,
                "process_start_time": start_ts,
            },
        )

        # 9. Asynchronously read stderr to prevent pipe buffer deadlock
        async def _read_stderr_stream(stream: asyncio.StreamReader | None) -> str:
            if stream is None:
                return ""
            try:
                data = await stream.read()
                return data.decode("utf-8", errors="replace")
            except Exception:
                return ""

        stderr_task = asyncio.create_task(_read_stderr_stream(proc.stderr))

        # 10. Read stdout line-by-line with timeout
        timeout_secs = request.allowance.timeout_seconds if request.allowance else 600
        timeout_secs = max(1.0, float(timeout_secs))

        observed_conv = conv_handle
        extracted_usage: dict[str, Any] = {}
        delta_text_chunks: list[str] = []
        raw_stdout_lines: list[str] = []
        terminal_result: dict[str, Any] | None = None
        timed_out = False
        cancelled = False

        deadline = time.time() + timeout_secs

        try:
            assert proc.stdout is not None
            while True:
                if request.attempt_id in self.cancelled_attempts:
                    cancelled = True
                    break

                remaining = deadline - time.time()
                if remaining <= 0:
                    timed_out = True
                    break

                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=min(remaining, 0.5)
                    )
                except asyncio.TimeoutError:
                    if request.attempt_id in self.cancelled_attempts:
                        cancelled = True
                        break
                    if time.time() >= deadline:
                        timed_out = True
                        break
                    continue

                if not line_bytes:
                    # Pipe closed / EOF reached
                    break

                line_str = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line_str.strip():
                    continue

                raw_stdout_lines.append(line_str)

                # Attempt NDJSON parsing
                parsed_json = None
                if line_str.startswith("{") and line_str.endswith("}"):
                    try:
                        parsed_json = json.loads(line_str)
                    except Exception:
                        parsed_json = None

                if isinstance(parsed_json, dict):
                    event_type = parsed_json.get("type") or parsed_json.get("event")
                    if event_type == "init":
                        cid = parsed_json.get("conversation_id") or parsed_json.get("conversationId")
                        if cid:
                            observed_conv = str(cid)
                        seq += 1
                        yield TurnEvent(
                            attempt_id=request.attempt_id,
                            sequence=seq,
                            event_type="heartbeat",
                            payload=parsed_json,
                        )
                    elif event_type == "step_update":
                        thought_content = parsed_json.get("thinking") or parsed_json.get("thought") or parsed_json.get("reasoning")
                        if isinstance(thought_content, dict):
                            thought_content = thought_content.get("text", "")
                        if thought_content:
                            seq += 1
                            yield TurnEvent(
                                attempt_id=request.attempt_id,
                                sequence=seq,
                                event_type="thought",
                                delta=str(thought_content),
                                payload=parsed_json,
                            )
                        delta_content = parsed_json.get("delta") or parsed_json.get("content")
                        if isinstance(delta_content, dict):
                            delta_content = delta_content.get("text", "")
                        if delta_content:
                            delta_text_chunks.append(str(delta_content))
                            seq += 1
                            yield TurnEvent(
                                attempt_id=request.attempt_id,
                                sequence=seq,
                                event_type="delta",
                                delta=str(delta_content),
                                payload=parsed_json,
                            )
                    elif event_type == "result":
                        res_obj = parsed_json.get("result") or parsed_json
                        cid = res_obj.get("conversation_id") or parsed_json.get("conversation_id")
                        if cid:
                            observed_conv = str(cid)
                        u = res_obj.get("usage") or parsed_json.get("usage")
                        if isinstance(u, dict):
                            extracted_usage.update(u)
                        resp = res_obj.get("response") or res_obj.get("output") or res_obj.get("text")
                        if resp and not delta_text_chunks:
                            delta_text_chunks.append(str(resp))
                        terminal_result = res_obj
                        seq += 1
                        yield TurnEvent(
                            attempt_id=request.attempt_id,
                            sequence=seq,
                            event_type="heartbeat",
                            payload=parsed_json,
                        )
                    else:
                        d = parsed_json.get("delta") or parsed_json.get("text")
                        if d:
                            delta_text_chunks.append(str(d))
                            seq += 1
                            yield TurnEvent(
                                attempt_id=request.attempt_id,
                                sequence=seq,
                                event_type="delta",
                                delta=str(d),
                                payload=parsed_json,
                            )
                        else:
                            seq += 1
                            yield TurnEvent(
                                attempt_id=request.attempt_id,
                                sequence=seq,
                                event_type="heartbeat",
                                payload=parsed_json,
                            )
                else:
                    # Plain text line fallback
                    delta_text_chunks.append(line_str)
                    seq += 1
                    yield TurnEvent(
                        attempt_id=request.attempt_id,
                        sequence=seq,
                        event_type="delta",
                        delta=line_str,
                    )

        finally:
            if cancelled or timed_out:
                await terminate_child_process(proc)

        # Ensure process termination and reap exit status
        await terminate_child_process(proc)
        try:
            stderr_text = await asyncio.wait_for(stderr_task, timeout=2.0)
        except Exception:
            stderr_text = ""

        exit_code = proc.returncode

        # 11. Handle cancellation
        if cancelled or request.attempt_id in self.cancelled_attempts:
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.CANCELLED,
                conversation_handle=observed_conv,
                conversation_id=observed_conv,
                output_text="".join(delta_text_chunks),
                error_message="Attempt was cancelled",
                error_code="CANCELLED",
                exit_code=exit_code,
                observed_model=request.model,
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            self.active_processes.pop(request.attempt_id, None)
            self.process_start_times.pop(request.attempt_id, None)
            yield res
            return

        # 12. Handle timeout
        if timed_out:
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.TIMEOUT,
                conversation_handle=observed_conv,
                conversation_id=observed_conv,
                output_text="".join(delta_text_chunks),
                error_message=f"Turn timed out after {timeout_secs}s",
                error_code="TIMEOUT",
                exit_code=exit_code,
                observed_model=request.model,
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            self.active_processes.pop(request.attempt_id, None)
            self.process_start_times.pop(request.attempt_id, None)
            yield res
            return

        # 13. Map token usage
        normalized_usage = dict(extracted_usage)
        if "input_tokens" in normalized_usage and "prompt_tokens" not in normalized_usage:
            normalized_usage["prompt_tokens"] = normalized_usage["input_tokens"]
        if "output_tokens" in normalized_usage and "completion_tokens" not in normalized_usage:
            normalized_usage["completion_tokens"] = normalized_usage["output_tokens"]
        if "prompt_tokens" in normalized_usage and "completion_tokens" in normalized_usage:
            if "total_tokens" not in normalized_usage:
                pt = normalized_usage.get("prompt_tokens") or 0
                ct = normalized_usage.get("completion_tokens") or 0
                normalized_usage["total_tokens"] = pt + ct

        # 14. Assemble output text
        is_ndjson = bool(terminal_result or any(l.startswith("{") for l in raw_stdout_lines))
        if terminal_result and (terminal_result.get("response") or terminal_result.get("output")):
            full_output_text = str(terminal_result.get("response") or terminal_result.get("output"))
        elif is_ndjson:
            full_output_text = "".join(delta_text_chunks)
        else:
            full_output_text = "\n".join(delta_text_chunks)

        # 15. Determine final TurnStatus & parse sections
        if exit_code != 0:
            err_lower = stderr_text.lower()
            if any(term in err_lower for term in ("sign in", "login", "unauthenticated", "auth")):
                status = TurnStatus.NEEDS_AUTH
            elif any(term in err_lower for term in ("rate limit", "quota", "resource_exhausted")):
                status = TurnStatus.RATE_LIMITED
            elif any(term in err_lower for term in ("permission", "denied", "forbidden")):
                status = TurnStatus.PERMISSION_BLOCKED
            elif any(term in err_lower for term in ("empty prompt", "flag needs an argument")):
                status = TurnStatus.MALFORMED_OUTPUT
            else:
                status = TurnStatus.UNAVAILABLE

            sanitized_err = sanitize_auth_error(stderr_text, request.account_ref)
            parsed_sections, structured_dict = extract_output_sections(full_output_text)
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=status,
                conversation_handle=observed_conv,
                conversation_id=observed_conv,
                output_text=full_output_text,
                parsed_sections=parsed_sections,
                structured_output=structured_dict,
                exit_code=exit_code,
                observed_model=request.model,
                usage=normalized_usage,
                error_message=sanitized_err,
                error_code=status.value.upper(),
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
        else:
            parsed_sections, structured_dict = extract_output_sections(full_output_text)
            missing = (
                parsed_sections.validate_required(request.required_sections)
                if parsed_sections
                else list(request.required_sections)
            )
            if missing:
                status = TurnStatus.MALFORMED_OUTPUT
                error_message = f"Missing required output sections: {', '.join(missing)}"
                error_code = "MALFORMED_OUTPUT"
            else:
                status = TurnStatus.SUCCESS
                error_message = None
                error_code = None

            res = TurnResult(
                attempt_id=request.attempt_id,
                status=status,
                conversation_handle=observed_conv,
                conversation_id=observed_conv,
                output_text=full_output_text,
                parsed_sections=parsed_sections,
                structured_output=structured_dict,
                exit_code=exit_code,
                observed_model=request.model,
                usage=normalized_usage,
                error_message=error_message,
                error_code=error_code,
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )

        self.completed_attempts[request.attempt_id] = res
        self.active_processes.pop(request.attempt_id, None)
        self.process_start_times.pop(request.attempt_id, None)
        yield res

    # -----------------------------------------------------------------------
    # 5. cancel
    # -----------------------------------------------------------------------

    async def cancel(self, attempt_id: str) -> None:
        """Actively terminate an in-flight attempt. Must be idempotent."""
        self.cancelled_attempts.add(attempt_id)
        proc = self.active_processes.get(attempt_id)
        if proc is not None:
            await terminate_child_process(proc)
            self.active_processes.pop(attempt_id, None)
            self.process_start_times.pop(attempt_id, None)

    # -----------------------------------------------------------------------
    # 6. reconcile
    # -----------------------------------------------------------------------

    async def reconcile(
        self, in_flight_attempts: list[AttemptSnapshot]
    ) -> list[AttemptReconciliation]:
        """Inspect interrupted attempts after restart and return reconciled statuses.

        Validates in-flight attempts against OS PID and process creation start time.
        Detects PID recycling traps and cleans up orphaned child processes without
        calling waitpid on non-children.
        """
        reconciled: list[AttemptReconciliation] = []
        for snap in in_flight_attempts:
            att_id = snap.attempt_id

            # 1. Check completed registry
            if att_id in self.completed_attempts:
                completed = self.completed_attempts[att_id]
                st = (
                    AttemptStatus.SUCCEEDED
                    if completed.status == TurnStatus.SUCCESS
                    else AttemptStatus.FAILED
                )
                reconciled.append(
                    AttemptReconciliation(
                        attempt_id=att_id,
                        reconciled_status=st,
                        reason="Attempt found in completed provider registry",
                        details="Attempt found in completed provider registry",
                        result=completed,
                    )
                )
                continue

            # 2. Check cancelled set
            if att_id in self.cancelled_attempts:
                reconciled.append(
                    AttemptReconciliation(
                        attempt_id=att_id,
                        reconciled_status=AttemptStatus.CANCELLED,
                        reason="Attempt was recorded as cancelled",
                        details="Attempt was recorded as cancelled",
                    )
                )
                continue

            # 3. Missing PID
            if snap.pid is None or snap.pid <= 0:
                reconciled.append(
                    AttemptReconciliation(
                        attempt_id=att_id,
                        reconciled_status=AttemptStatus.UNKNOWN,
                        reason="Attempt interrupted before process creation",
                        details="Missing PID in snapshot",
                    )
                )
                continue

            # 4. Dead PID
            if not is_pid_alive(snap.pid):
                reconciled.append(
                    AttemptReconciliation(
                        attempt_id=att_id,
                        reconciled_status=AttemptStatus.UNKNOWN,
                        reason=f"Process PID {snap.pid} is no longer running",
                        details=f"Process PID {snap.pid} died during server interruption",
                    )
                )
                continue

            # 5. PID is alive: verify process start time to detect PID reuse
            current_start_time = get_process_start_time(snap.pid)
            if snap.process_start_time is not None and current_start_time is not None:
                diff = abs(current_start_time - snap.process_start_time)
                if diff > 2.0:
                    # PID reuse detected: running process belongs to another program!
                    reconciled.append(
                        AttemptReconciliation(
                            attempt_id=att_id,
                            reconciled_status=AttemptStatus.UNKNOWN,
                            reason=f"PID reuse detected: OS process start time diff {diff:.2f}s > 2.0s",
                            details="Running process belongs to an unrelated application",
                        )
                    )
                    continue

            # 6. Orphaned worker process from previous run: terminate cleanly without waitpid
            if snap.pid != os.getpid():
                await terminate_pid_tree(snap.pid)
            reconciled.append(
                AttemptReconciliation(
                    attempt_id=att_id,
                    reconciled_status=AttemptStatus.UNKNOWN,
                    reason=f"Orphaned worker process PID {snap.pid} terminated",
                    details="Interrupted in-flight attempt cleaned up upon reconciliation",
                )
            )

        return reconciled
