"""Model execution runner for the AGYM orchestration subsystem.

Single responsibility: Turn a ModelInvocation into a ModelResult.
Nothing else.

Does not introduce orchestration logic, budgets, rounds, auditors, or fleet selection.
Reuses existing AGYM profile, launcher, credential contexts, and environment logic.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Generator, Mapping, Sequence

from agym.launcher import (
    AgyNotFound,
    build_agy_args,
    build_profile_env,
    cleanup_profile_locks,
    resolve_agy,
)
from agym.orchestration.contracts import (
    ConversationId,
    ExecutionStrategy,
    FailureClass,
    InvocationId,
    InvocationStatus,
    ModelInvocation,
    ModelResult,
    ModelRunner,
    ModelSession,
    RunId,
    WorkerRole,
    WorkspaceMode,
)
from agym.orchestration.strategies import (
    ExecutionSettings,
    UnsupportedStrategyError,
    get_execution_settings,
    get_strategy_args,
    is_strategy_supported,
    map_strategy,
)
from agym.profiles import Profile, ProfileError, ProfileNotFound, ProfileStore
from agym.orchestration.locking import FileLock
from agym.orchestration.prompts import redact_profile_identities
from agym.wincred import (
    async_profile_credential_context,
    get_wincred_async_lock,
    is_windows_platform,
    profile_credential_context,
    sync_credentials_after_launch,
)

logger = logging.getLogger("agym.orchestration.runner")


def get_wincred_cross_process_lock() -> FileLock:
    """Return a cross-process lock for Windows Credential Manager synchronization."""
    lock_file = Path(tempfile.gettempdir()) / ".agym_wincred_global.lock"
    return FileLock(lock_file, timeout=10.0)

__all__ = [
    "AntigravityRunner",
    "AntigravitySession",
    "FakeModelRunner",
    "FakeModelSession",
    "classify_failure",
    "is_auth_failure",
]

# Patterns detecting authentication issues from stderr or stdout
AUTH_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"auth\s+waiting", re.IGNORECASE),
    re.compile(r"authfailure", re.IGNORECASE),
    re.compile(r"not authenticated", re.IGNORECASE),
    re.compile(r"unauthenticated", re.IGNORECASE),
    re.compile(r"unauthorized", re.IGNORECASE),
    re.compile(r"re-authenticate", re.IGNORECASE),
    re.compile(r"please log in", re.IGNORECASE),
    re.compile(r"sign-in flow", re.IGNORECASE),
    re.compile(r"could not find credentials", re.IGNORECASE),
    re.compile(r"token expired", re.IGNORECASE),
    re.compile(r"oauth2?:.*(?:invalid_grant|unauthorized)", re.IGNORECASE),
    re.compile(r"out of credits", re.IGNORECASE),
)

# Patterns detecting conversation ID from CLI output
CONVERSATION_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'"conversation_id"\s*:\s*"([a-zA-Z0-9_-]+)"', re.IGNORECASE),
    re.compile(r'"conversationId"\s*:\s*"([a-zA-Z0-9_-]+)"', re.IGNORECASE),
    re.compile(r'"session_id"\s*:\s*"([a-zA-Z0-9_-]+)"', re.IGNORECASE),
    re.compile(r'Conversation ID:\s*([a-zA-Z0-9_-]+)', re.IGNORECASE),
    re.compile(r'conversation_id\s*=\s*([a-zA-Z0-9_-]+)', re.IGNORECASE),
)


def is_auth_failure(text: str) -> bool:
    """Detect whether output or error text indicates an authentication failure."""
    if not text:
        return False
    return any(p.search(text) for p in AUTH_FAILURE_PATTERNS)


def classify_failure(result: ModelResult) -> FailureClass:
    """Classify mechanical execution failure of a ModelResult into FailureClass.

    Reports mechanical execution classifications:
    - TIMEOUT, NONZERO, EMPTY OUTPUT -> RETRYABLE
    - AUTHENTICATION, UNSUPPORTED STRATEGY, CANCELLED -> UNRECOVERABLE
    - MALFORMED JSON, MALFORMED STRUCTURED PAYLOAD -> RECOVERABLE
    """
    if result.status == InvocationStatus.SUCCEEDED:
        return FailureClass.RECOVERABLE

    err = (result.error or "").lower()
    if "unsupported execution strategy" in err:
        return FailureClass.UNRECOVERABLE
    if "authentication failure" in err or is_auth_failure(err):
        return FailureClass.UNRECOVERABLE
    if result.status == InvocationStatus.CANCELLED or "cancelled" in err:
        return FailureClass.UNRECOVERABLE
    if "malformed json" in err or "malformed structured payload" in err:
        return FailureClass.RECOVERABLE
    if "timed out" in err or "timeout" in err:
        return FailureClass.RETRYABLE
    if "empty output" in err:
        return FailureClass.RETRYABLE
    if result.exit_code is not None and result.exit_code != 0:
        return FailureClass.RETRYABLE

    return FailureClass.RETRYABLE


def _extract_conversation_id(text: str) -> ConversationId | None:
    """Extract conversation ID from output text if present."""
    if not text:
        return None
    for pattern in CONVERSATION_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return ConversationId(match.group(1))
    return None


def kill_process_tree(
    proc: Any,
    sig: signal.Signals = signal.SIGTERM,
) -> None:
    """Terminate or kill a process and its child descendants across platforms."""
    if proc is None:
        return
    if getattr(proc, "returncode", None) is not None:
        return
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return

    if is_windows_platform():
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        except Exception:
            pass
    else:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            try:
                if hasattr(proc, "send_signal") and callable(proc.send_signal):
                    proc.send_signal(sig)
            except Exception:
                pass


class AntigravityRunner:
    """Conforms to ModelRunner.

    Executes ModelInvocation payloads using the local Antigravity CLI (agy)
    with profile isolation, credential contexts, and async subprocess control.
    """

    def __init__(
        self,
        agy_path: Path | str | None = None,
        profile_store: ProfileStore | None = None,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        if agy_path:
            self._agy_path = Path(agy_path)
        else:
            try:
                self._agy_path = resolve_agy()
            except (AgyNotFound, Exception):
                self._agy_path = Path("agy")

        self._profile_store = profile_store or ProfileStore()
        self._base_env = dict(base_env) if base_env else None

        # Instance-scoped process tracking: run_id -> {invocation_id -> Process}
        self._active_processes: dict[RunId, dict[InvocationId, asyncio.subprocess.Process]] = {}
        self._lock = threading.Lock()

    @property
    def agy_path(self) -> Path:
        return self._agy_path

    # =========================================================================
    # Process management and cancellation (scoped per run)
    # =========================================================================

    def _register_process(
        self,
        run_id: RunId,
        invocation_id: InvocationId,
        proc: asyncio.subprocess.Process,
    ) -> None:
        with self._lock:
            self._active_processes.setdefault(run_id, {})[invocation_id] = proc

    def _deregister_process(self, run_id: RunId, invocation_id: InvocationId) -> None:
        with self._lock:
            run_procs = self._active_processes.get(run_id)
            if run_procs:
                run_procs.pop(invocation_id, None)
                if not run_procs:
                    self._active_processes.pop(run_id, None)

    def cancel_run(self, run_id: RunId | str) -> int:
        """Cancel all active subprocesses belonging to a specific run, terminating process trees."""
        rid = RunId(run_id)
        with self._lock:
            procs = list(self._active_processes.get(rid, {}).values())

        cancelled = 0
        for proc in procs:
            if getattr(proc, "returncode", None) is None:
                kill_process_tree(proc, signal.SIGTERM)
                try:
                    if hasattr(proc, "terminate") and callable(proc.terminate):
                        proc.terminate()
                except Exception:
                    pass
                cancelled += 1

        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if all(getattr(p, "returncode", None) is not None for p in procs):
                break
            time.sleep(0.05)

        for proc in procs:
            if getattr(proc, "returncode", None) is None:
                kill_process_tree(proc, signal.SIGKILL)
                try:
                    if hasattr(proc, "kill") and callable(proc.kill):
                        proc.kill()
                except Exception:
                    pass

        return cancelled

    def cancel_invocation(
        self,
        run_id: RunId | str,
        invocation_id: InvocationId | str,
    ) -> bool:
        """Cancel an individual invocation's active subprocess tree."""
        rid = RunId(run_id)
        iid = InvocationId(invocation_id)
        with self._lock:
            proc = self._active_processes.get(rid, {}).get(iid)

        if proc and getattr(proc, "returncode", None) is None:
            kill_process_tree(proc, signal.SIGTERM)
            try:
                if hasattr(proc, "terminate") and callable(proc.terminate):
                    proc.terminate()
                return True
            except (ProcessLookupError, OSError):
                return False
        return False

    def active_runs(self) -> list[RunId]:
        """List run IDs that currently have running subprocesses."""
        with self._lock:
            return list(self._active_processes.keys())

    def active_invocations(self, run_id: RunId | str) -> list[InvocationId]:
        """List active invocation IDs for a given run ID."""
        rid = RunId(run_id)
        with self._lock:
            return list(self._active_processes.get(rid, {}).keys())

    def is_invocation_running(
        self,
        run_id: RunId | str,
        invocation_id: InvocationId | str,
    ) -> bool:
        """Check if an invocation's subprocess is actively running."""
        rid = RunId(run_id)
        iid = InvocationId(invocation_id)
        with self._lock:
            proc = self._active_processes.get(rid, {}).get(iid)
            return proc is not None and proc.returncode is None

    # =========================================================================
    # Argument & Environment Construction
    # =========================================================================

    def build_argv(
        self,
        invocation: ModelInvocation,
        profile: Profile | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Construct the complete command-line argv for spawning agy."""
        strat_settings = get_execution_settings(invocation.strategy)
        if not strat_settings.is_supported:
            raise UnsupportedStrategyError(
                strategy=strat_settings.strategy,
                reason=strat_settings.unsupported_reason or "Strategy not supported",
            )

        op_args: list[str] = list(strat_settings.args)

        if invocation.output_schema is not None:
            schema_json = json.dumps(invocation.output_schema)
            op_args.extend(["--output-format", "json", "--json-schema", schema_json])
        else:
            op_args.extend(["--output-format", "text"])

        if invocation.conversation_id:
            op_args.extend(["--conversation", str(invocation.conversation_id)])

        op_args.extend(["--print", invocation.prompt])

        if invocation.workspace_mode == WorkspaceMode.READ_ONLY:
            op_args.extend(["--mode", "plan", "--sandbox"])
        elif invocation.workspace_mode == WorkspaceMode.MUTATING:
            op_args.extend(["--mode", "accept-edits"])

        if profile is not None:
            if invocation.workspace_mode == WorkspaceMode.READ_ONLY:
                from dataclasses import replace
                safe_settings = replace(profile.settings, dangerously_skip_permissions=False)
                profile = replace(profile, settings=safe_settings)
                if env and "AGYM_DANGEROUSLY_SKIP_PERMISSIONS" in env:
                    env = {k: v for k, v in env.items() if k != "AGYM_DANGEROUSLY_SKIP_PERMISSIONS"}

            argv = build_agy_args(
                profile,
                operation_args=op_args,
                agy_path=self._agy_path,
                env=env,
            )
        else:
            argv = [str(self._agy_path), *op_args]

        if invocation.workspace_mode == WorkspaceMode.READ_ONLY:
            argv = [arg for arg in argv if arg not in ("--dangerously-skip-permissions", "-y", "--yes")]

        return argv

    # =========================================================================
    # ModelRunner Protocol implementation
    # =========================================================================

    def run(
        self,
        invocation: ModelInvocation,
        profile_name: str | None = None,
    ) -> ModelResult:
        """Execute a single model invocation synchronously (ModelRunner protocol)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, self.run_async(invocation, profile_name))
                return future.result()
        else:
            return asyncio.run(self.run_async(invocation, profile_name))

    async def run_async(
        self,
        invocation: ModelInvocation,
        profile_name: str | None = None,
    ) -> ModelResult:
        """Execute a single model invocation asynchronously with subprocess execution."""
        started_at = datetime.now(timezone.utc).isoformat()

        # Check strategy support upfront
        strat_settings = get_execution_settings(invocation.strategy)
        if not strat_settings.is_supported:
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                error=(
                    f"Unsupported execution strategy '{invocation.strategy.value}': "
                    f"{strat_settings.unsupported_reason}"
                ),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        # Profile and environment resolution
        profile: Profile | None = None
        if profile_name:
            try:
                profile = self._profile_store.get(profile_name)
            except ProfileNotFound as exc:
                return ModelResult(
                    invocation_id=invocation.invocation_id,
                    status=InvocationStatus.FAILED,
                    error=f"Profile '[REDACTED]' not found: {redact_profile_identities(str(exc), [profile_name])}",
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            if profile.settings.validation_errors:
                return ModelResult(
                    invocation_id=invocation.invocation_id,
                    status=InvocationStatus.FAILED,
                    error=f"Invalid profile settings: {', '.join(profile.settings.validation_errors)}",
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            cleanup_profile_locks(profile.home)
            env = build_profile_env(
                profile.home,
                base_env=self._base_env,
                profile_name=profile.name,
            )
            cred_ctx: Any = async_profile_credential_context(profile.home)
        else:
            env = dict(self._base_env or os.environ)
            cred_ctx = nullcontext()

        # Construct command line
        try:
            cmd = self.build_argv(invocation, profile=profile, env=env)
        except UnsupportedStrategyError as exc:
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                error=str(exc),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        # Async subprocess execution
        proc: asyncio.subprocess.Process | None = None
        popen_kwargs: dict[str, Any] = {}
        if not is_windows_platform():
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            if is_windows_platform() and profile is not None:
                wincred_lock = get_wincred_cross_process_lock()
                async with get_wincred_async_lock():
                    with wincred_lock:
                        with profile_credential_context(profile.home):
                            proc = await asyncio.create_subprocess_exec(
                                *cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                env=env,
                                **popen_kwargs,
                            )
            else:
                async with cred_ctx:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        **popen_kwargs,
                    )
            self._register_process(invocation.run_id, invocation.invocation_id, proc)

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=invocation.timeout_seconds,
            )
        except asyncio.TimeoutError:
            if proc:
                await self._cleanup_process(proc)
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                exit_code=proc.returncode if proc else None,
                error=f"Execution timed out after {invocation.timeout_seconds}s",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        except asyncio.CancelledError:
            if proc:
                await self._cleanup_process(proc)
            raise
        except Exception as exc:
            if proc:
                await self._cleanup_process(proc)
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                error=f"Subprocess spawn or execution failed: {exc}",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            self._deregister_process(invocation.run_id, invocation.invocation_id)
            if is_windows_platform() and profile is not None and proc is not None:
                try:
                    wincred_lock = get_wincred_cross_process_lock()
                    async with get_wincred_async_lock():
                        with wincred_lock:
                            sync_credentials_after_launch(profile.home)
                except Exception as exc:
                    logger.debug("Failed to sync credentials after subprocess exit: %s", exc)

        completed_at = datetime.now(timezone.utc).isoformat()
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        exit_code = proc.returncode

        # Check for cancelled via signal termination
        if exit_code in (-15, -9, 143, 137):
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.CANCELLED,
                exit_code=exit_code,
                error=f"Process terminated by signal {abs(exit_code)}",
                started_at=started_at,
                completed_at=completed_at,
            )

        # Check for non-zero exit code
        if exit_code != 0:
            if is_auth_failure(stderr_text) or is_auth_failure(stdout_text):
                err_msg = f"Authentication failure: {stderr_text or stdout_text}"
            else:
                err_msg = f"Process exited with non-zero exit code {exit_code}: {stderr_text or stdout_text}"
            err_msg = redact_profile_identities(err_msg, [profile_name] if profile_name else None)
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                exit_code=exit_code,
                response=stdout_text if stdout_text else None,
                error=err_msg,
                started_at=started_at,
                completed_at=completed_at,
            )

        # Check for empty output
        if not stdout_text:
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                exit_code=0,
                error="Empty output returned from model runner",
                started_at=started_at,
                completed_at=completed_at,
            )

        # Conversation ID resolution
        convo_id = _extract_conversation_id(stdout_text) or _extract_conversation_id(stderr_text)
        if not convo_id and invocation.conversation_id:
            convo_id = invocation.conversation_id

        # Structured output parsing if output_schema was specified
        if invocation.output_schema is not None:
            return self._parse_structured_output(
                invocation=invocation,
                stdout_text=stdout_text,
                convo_id=convo_id,
                started_at=started_at,
                completed_at=completed_at,
            )

        # Standard plain response
        return ModelResult(
            invocation_id=invocation.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            response=stdout_text,
            structured_data=None,
            conversation_id=convo_id,
            exit_code=0,
            started_at=started_at,
            completed_at=completed_at,
        )

    def _parse_structured_output(
        self,
        invocation: ModelInvocation,
        stdout_text: str,
        convo_id: ConversationId | None,
        started_at: str,
        completed_at: str,
    ) -> ModelResult:
        """Parse structured output from stdout while distinguishing payload issues from process failure."""
        try:
            parsed = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                response=stdout_text,
                exit_code=0,
                conversation_id=convo_id,
                error=f"Malformed JSON in output: {exc}",
                started_at=started_at,
                completed_at=completed_at,
            )

        structured_dict: dict[str, Any] | None = None
        if isinstance(parsed, dict):
            # Check if response is wrapped in 'result' or 'response'
            if "result" in parsed and isinstance(parsed["result"], dict):
                structured_dict = parsed["result"]
            elif "response" in parsed and isinstance(parsed["response"], dict):
                structured_dict = parsed["response"]
            else:
                structured_dict = parsed
        else:
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                response=stdout_text,
                exit_code=0,
                conversation_id=convo_id,
                error="Malformed structured payload: expected JSON object",
                started_at=started_at,
                completed_at=completed_at,
            )

        # Validate required properties if schema specified them
        if invocation.output_schema and isinstance(invocation.output_schema, dict):
            required = invocation.output_schema.get("required", [])
            if isinstance(required, list):
                missing = [k for k in required if k not in structured_dict]
                if missing:
                    return ModelResult(
                        invocation_id=invocation.invocation_id,
                        status=InvocationStatus.FAILED,
                        response=stdout_text,
                        structured_data=structured_dict,
                        exit_code=0,
                        conversation_id=convo_id,
                        error=f"Malformed structured payload: missing required field(s): {', '.join(missing)}",
                        started_at=started_at,
                        completed_at=completed_at,
                    )

        return ModelResult(
            invocation_id=invocation.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            response=stdout_text,
            structured_data=structured_dict,
            conversation_id=convo_id,
            exit_code=0,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _cleanup_process(self, proc: asyncio.subprocess.Process) -> None:
        """Escalate from SIGTERM to SIGKILL on process tree if process hangs during cleanup."""
        if getattr(proc, "returncode", None) is not None:
            return
        kill_process_tree(proc, signal.SIGTERM)
        try:
            res = proc.terminate()
            if asyncio.iscoroutine(res):
                await res
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            kill_process_tree(proc, signal.SIGKILL)
            try:
                res = proc.kill()
                if asyncio.iscoroutine(res):
                    await res
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                pass

    def create_session(
        self,
        profile_name: str | None = None,
        strategy: ExecutionStrategy = ExecutionStrategy.STANDARD,
        timeout_seconds: float = 300.0,
        conversation_id: ConversationId | str | None = None,
    ) -> AntigravitySession:
        """Create a multi-turn persistent session."""
        return AntigravitySession(
            profile_name=profile_name,
            strategy=strategy,
            agy_path=self._agy_path,
            profile_store=self._profile_store,
            base_env=self._base_env,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
        )


class AntigravitySession:
    """Persistent multi-turn session conforming to ModelSession protocol.

    Spawns one Antigravity process with stream-json format, keeps stdin/stdout
    alive across multiple requests, captures conversation ID, and terminates cleanly.
    """

    def __init__(
        self,
        profile_name: str | None = None,
        strategy: ExecutionStrategy = ExecutionStrategy.STANDARD,
        agy_path: Path | str | None = None,
        profile_store: ProfileStore | None = None,
        base_env: Mapping[str, str] | None = None,
        timeout_seconds: float = 300.0,
        conversation_id: ConversationId | str | None = None,
    ) -> None:
        self.profile_name = profile_name
        self.strategy = (
            ExecutionStrategy(strategy)
            if isinstance(strategy, str)
            else strategy
        )
        if agy_path:
            self._agy_path = Path(agy_path)
        else:
            try:
                self._agy_path = resolve_agy()
            except (AgyNotFound, Exception):
                self._agy_path = Path("agy")

        self._profile_store = profile_store or ProfileStore()
        self._base_env = dict(base_env) if base_env else None
        self.default_timeout = float(timeout_seconds)
        self._conversation_id = ConversationId(conversation_id) if conversation_id else None
        self._turn_count = 0
        self._is_closed = False
        self._is_invalid = False
        self._lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._stream_lock: asyncio.Lock | None = None

        # Dedicated background event loop and thread for process lifecycle
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._proc: asyncio.subprocess.Process | None = None
        self._proc_started = threading.Event()
        self._start_error: Exception | None = None

        fut = asyncio.run_coroutine_threadsafe(self._start_process(), self._loop)
        try:
            fut.result(timeout=10.0)
        except Exception as exc:
            self._start_error = exc
            self.close()
            raise

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._stream_lock = asyncio.Lock()
        self._loop.run_forever()

    async def _restart_process(self) -> None:
        """Restart the underlying agy subprocess after crash or timeout."""
        await self._close_process()
        self._is_invalid = False
        await self._start_process()

    async def _start_process(self) -> None:
        """Start the long-running agy subprocess in stream-json mode."""
        strat_settings = get_execution_settings(self.strategy)
        if not strat_settings.is_supported:
            raise UnsupportedStrategyError(
                strategy=strat_settings.strategy,
                reason=strat_settings.unsupported_reason or "Strategy not supported",
            )

        profile: Profile | None = None
        if self.profile_name:
            profile = self._profile_store.get(self.profile_name)
            cleanup_profile_locks(profile.home)
            env = build_profile_env(
                profile.home,
                base_env=self._base_env,
                profile_name=profile.name,
            )
        else:
            env = dict(self._base_env or os.environ)

        op_args = [
            *strat_settings.args,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ]
        if self._conversation_id:
            op_args.extend(["--conversation", str(self._conversation_id)])

        if profile is not None:
            cmd = build_agy_args(
                profile,
                operation_args=op_args,
                agy_path=self._agy_path,
                env=env,
            )
        else:
            cmd = [str(self._agy_path), *op_args]

        popen_kwargs: dict[str, Any] = {}
        if not is_windows_platform():
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **popen_kwargs,
        )
        self._proc_started.set()

    @property
    def conversation_id(self) -> ConversationId:
        """Identifier of this ongoing conversation."""
        if self._conversation_id is None:
            return ConversationId("unknown")
        return self._conversation_id

    # =========================================================================
    # Request execution: send() / ask()
    # =========================================================================

    def send(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        """Send a prompt and receive the model's result (ModelSession protocol)."""
        if self._is_closed:
            raise RuntimeError("Session is closed")
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout
        with self._turn_lock:
            fut = asyncio.run_coroutine_threadsafe(
                self._send_coro(prompt, timeout),
                self._loop,
            )
            try:
                return fut.result(timeout=timeout + 5.0)
            except (concurrent.futures.TimeoutError, Exception) as exc:
                fut.cancel()
                self._is_invalid = True
                asyncio.run_coroutine_threadsafe(self._close_process(), self._loop)
                started_at = datetime.now(timezone.utc).isoformat()
                return ModelResult(
                    invocation_id=InvocationId(f"{self.conversation_id}-{self._turn_count}"),
                    status=InvocationStatus.FAILED,
                    error=f"Session turn timed out or failed: {exc}",
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

    def ask(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        """Alias for send() to satisfy wave1-A requirements."""
        return self.send(prompt, timeout_seconds=timeout_seconds)

    async def send_async(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        """Async send implementation."""
        if self._is_closed:
            raise RuntimeError("Session is closed")
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout
        fut = asyncio.run_coroutine_threadsafe(
            self._send_coro(prompt, timeout),
            self._loop,
        )
        try:
            return await asyncio.wrap_future(fut)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as exc:
            fut.cancel()
            self._is_invalid = True
            asyncio.run_coroutine_threadsafe(self._close_process(), self._loop)
            started_at = datetime.now(timezone.utc).isoformat()
            return ModelResult(
                invocation_id=InvocationId(f"{self.conversation_id}-{self._turn_count}"),
                status=InvocationStatus.FAILED,
                error=f"Session turn timed out or failed: {exc}",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

    async def ask_async(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        """Async alias for ask()."""
        return await self.send_async(prompt, timeout_seconds=timeout_seconds)

    async def _send_coro(self, prompt: str, timeout: float) -> ModelResult:
        """Internal coroutine executed on self._loop to send a turn and read response."""
        started_at = datetime.now(timezone.utc).isoformat()
        self._turn_count += 1
        inv_id = InvocationId(f"{self.conversation_id}-{self._turn_count}")

        if self._stream_lock is None:
            self._stream_lock = asyncio.Lock()

        async with self._stream_lock:
            if self._is_closed:
                return ModelResult(
                    invocation_id=inv_id,
                    status=InvocationStatus.FAILED,
                    error="Session is closed",
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

            if self._is_invalid or not self._proc or getattr(self._proc, "returncode", None) is not None:
                try:
                    await self._restart_process()
                except Exception as exc:
                    return ModelResult(
                        invocation_id=inv_id,
                        status=InvocationStatus.FAILED,
                        error=f"Failed to restart session process: {exc}",
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )

            # Write NDJSON prompt line to stdin
            payload = json.dumps({"prompt": prompt}) + "\n"
            try:
                assert self._proc.stdin is not None
                write_res = self._proc.stdin.write(payload.encode("utf-8"))
                if asyncio.iscoroutine(write_res):
                    await write_res
                drain_coro = self._proc.stdin.drain()
                if asyncio.iscoroutine(drain_coro):
                    await drain_coro
            except Exception as exc:
                self._is_invalid = True
                await self._close_process()
                return ModelResult(
                    invocation_id=inv_id,
                    status=InvocationStatus.FAILED,
                    error=f"Failed to write to subprocess stdin: {exc}",
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

            # Read NDJSON response events from stdout
            response_lines: list[str] = []
            structured_data: dict[str, Any] | None = None
            raw_response_text: str | None = None
            has_terminal_event = False

            try:
                assert self._proc.stdout is not None
                while True:
                    line_bytes = await asyncio.wait_for(
                        self._proc.stdout.readline(),
                        timeout=timeout,
                    )
                    if not line_bytes:
                        break

                    if isinstance(line_bytes, bytes):
                        line_str = line_bytes.decode("utf-8", errors="replace").strip()
                    else:
                        line_str = str(line_bytes).strip()

                    if not line_str:
                        continue

                    try:
                        event = json.loads(line_str)
                    except json.JSONDecodeError:
                        response_lines.append(line_str)
                        continue

                    # Capture conversation ID from event
                    cid = (
                        event.get("conversation_id")
                        or event.get("conversationId")
                        or event.get("session_id")
                    )
                    if cid and (self._conversation_id is None or self._conversation_id == "unknown"):
                        self._conversation_id = ConversationId(cid)

                    evt_type = event.get("event") or event.get("step_type")
                    if evt_type == "init":
                        continue
                    elif evt_type in ("result", "turn_complete", "response"):
                        res = event.get("result") or event.get("response") or event.get("output")
                        if isinstance(res, dict):
                            structured_data = res
                            raw_response_text = json.dumps(res)
                        elif isinstance(res, str):
                            raw_response_text = res
                        elif res is not None:
                            raw_response_text = str(res)
                        has_terminal_event = True
                        break
                    elif evt_type == "error":
                        err_msg = str(event.get("error") or event.get("message") or event)
                        has_terminal_event = True
                        return ModelResult(
                            invocation_id=inv_id,
                            status=InvocationStatus.FAILED,
                            error=err_msg,
                            conversation_id=self.conversation_id,
                            started_at=started_at,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                        )
                    elif "response" in event:
                        raw_response_text = str(event["response"])
                        has_terminal_event = True
                        break
                    else:
                        if "delta" in event:
                            response_lines.append(str(event["delta"]))
                        elif "text" in event:
                            response_lines.append(str(event["text"]))
            except asyncio.TimeoutError:
                self._is_invalid = True
                await self._close_process()
                return ModelResult(
                    invocation_id=inv_id,
                    status=InvocationStatus.FAILED,
                    error=f"Session turn timed out after {timeout}s",
                    conversation_id=self.conversation_id,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                self._is_invalid = True
                await self._close_process()
                return ModelResult(
                    invocation_id=inv_id,
                    status=InvocationStatus.FAILED,
                    error=f"Error reading session stream: {exc}",
                    conversation_id=self.conversation_id,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

            # Process status and EOF verification
            proc_returncode = getattr(self._proc, "returncode", None)
            if proc_returncode is not None and not has_terminal_event:
                self._is_invalid = True
                await self._close_process()
                return ModelResult(
                    invocation_id=inv_id,
                    status=InvocationStatus.FAILED,
                    error=f"Subprocess terminated prematurely (exit code {proc_returncode})",
                    exit_code=proc_returncode,
                    conversation_id=self.conversation_id,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

            if not has_terminal_event and not response_lines and raw_response_text is None:
                self._is_invalid = True
                await self._close_process()
                return ModelResult(
                    invocation_id=inv_id,
                    status=InvocationStatus.FAILED,
                    error="Subprocess stream closed with EOF before delivering response",
                    conversation_id=self.conversation_id,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

            if raw_response_text is None:
                raw_response_text = "\n".join(response_lines).strip()

            if not raw_response_text:
                self._is_invalid = True
                await self._close_process()
                return ModelResult(
                    invocation_id=inv_id,
                    status=InvocationStatus.FAILED,
                    error="Empty response text returned from session",
                    conversation_id=self.conversation_id,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

            return ModelResult(
                invocation_id=inv_id,
                status=InvocationStatus.SUCCEEDED,
                response=raw_response_text,
                structured_data=structured_data,
                conversation_id=self.conversation_id,
                exit_code=0,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

    # =========================================================================
    # Lifecycle & Cleanup
    # =========================================================================

    def close(self) -> None:
        """Terminate the session and release resources cleanly."""
        with self._lock:
            if self._is_closed:
                return
            self._is_closed = True

        if threading.current_thread() is self._thread:
            asyncio.create_task(self._close_process())
        else:
            fut = asyncio.run_coroutine_threadsafe(self._close_process(), self._loop)
            try:
                fut.result(timeout=5.0)
            except Exception:
                pass
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                if self._thread.is_alive():
                    self._thread.join(timeout=2.0)
                if not self._loop.is_closed():
                    self._loop.close()

    async def _close_process(self) -> None:
        """Terminate the subprocess on self._loop."""
        proc = self._proc
        self._proc = None
        if not proc:
            return

        if getattr(proc, "returncode", None) is None:
            try:
                if proc.stdin:
                    close_res = proc.stdin.close()
                    if asyncio.iscoroutine(close_res):
                        await close_res
                    wait_closed = getattr(proc.stdin, "wait_closed", None)
                    if callable(wait_closed):
                        res = wait_closed()
                        if asyncio.iscoroutine(res):
                            await res
            except Exception:
                pass

            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                kill_process_tree(proc, signal.SIGTERM)
                try:
                    term_res = proc.terminate()
                    if asyncio.iscoroutine(term_res):
                        await term_res
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except (asyncio.TimeoutError, Exception):
                    kill_process_tree(proc, signal.SIGKILL)
                    try:
                        kill_res = proc.kill()
                        if asyncio.iscoroutine(kill_res):
                            await kill_res
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except Exception:
                        pass

    def __enter__(self) -> AntigravitySession:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    async def __aenter__(self) -> AntigravitySession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.close()


class FakeModelRunner:
    """Fake model runner for testing engine without Gemini quota.

    Conforms to ModelRunner protocol.
    Supports scripted responses such as:
    invocation 1 -> success
    invocation 2 -> timeout
    invocation 3 -> malformed result
    """

    def __init__(
        self,
        responses: Sequence[Any] = (),
        default_response: Any = None,
    ) -> None:
        self._responses: list[Any] = list(responses)
        self.default_response = default_response
        self.invocations: list[ModelInvocation] = []
        self.calls: list[tuple[ModelInvocation, str | None]] = []
        self.cancelled_runs: list[RunId] = []
        self.cancelled_invocations: list[tuple[RunId, InvocationId]] = []

    def add_response(self, response: Any) -> None:
        """Queue a scripted response."""
        self._responses.append(response)

    def add_scripted_result(self, result: Any) -> None:
        """Alias for add_response."""
        self.add_response(result)

    def cancel_run(self, run_id: RunId | str) -> int:
        """Record cancelled run."""
        self.cancelled_runs.append(RunId(run_id))
        return 1

    def cancel_invocation(
        self,
        run_id: RunId | str,
        invocation_id: InvocationId | str,
    ) -> bool:
        """Record cancelled invocation."""
        self.cancelled_invocations.append((RunId(run_id), InvocationId(invocation_id)))
        return True

    def run(
        self,
        invocation: ModelInvocation,
        profile_name: str | None = None,
    ) -> ModelResult:
        """Execute invocation with scripted result (ModelRunner protocol)."""
        self.invocations.append(invocation)
        self.calls.append((invocation, profile_name))

        if self._responses:
            item = self._responses.pop(0)
        elif self.default_response is not None:
            item = self.default_response
        else:
            item = f"Fake response for invocation {invocation.invocation_id}"

        started_at = datetime.now(timezone.utc).isoformat()
        completed_at = datetime.now(timezone.utc).isoformat()

        if isinstance(item, ModelResult):
            return item

        if isinstance(item, Exception):
            raise item

        if callable(item):
            res = item(invocation, profile_name)
            if isinstance(res, ModelResult):
                return res
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.SUCCEEDED,
                response=str(res),
                started_at=started_at,
                completed_at=completed_at,
            )

        if item in ("timeout", "TIMEOUT"):
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                error=f"Execution timed out after {invocation.timeout_seconds}s",
                started_at=started_at,
                completed_at=completed_at,
            )

        if item in ("malformed", "malformed_json", "malformed_result"):
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                response="{not valid json",
                error="Malformed JSON in output: Expecting value: line 1 column 2",
                exit_code=0,
                started_at=started_at,
                completed_at=completed_at,
            )

        if item in ("malformed_payload", "invalid_payload"):
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                response=json.dumps({"wrong": 123}),
                structured_data={"wrong": 123},
                error="Malformed structured payload: missing required field(s)",
                exit_code=0,
                started_at=started_at,
                completed_at=completed_at,
            )

        if item in ("nonzero", "nonzero_exit", "failure"):
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                exit_code=1,
                error="Process exited with non-zero exit code 1: internal error",
                started_at=started_at,
                completed_at=completed_at,
            )

        if item in ("empty", "empty_output"):
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                exit_code=0,
                error="Empty output returned from model runner",
                started_at=started_at,
                completed_at=completed_at,
            )

        if item in ("cancelled", "CANCELLED"):
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.CANCELLED,
                error="Invocation was cancelled",
                started_at=started_at,
                completed_at=completed_at,
            )

        if isinstance(item, dict):
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.SUCCEEDED,
                response=json.dumps(item),
                structured_data=item,
                exit_code=0,
                started_at=started_at,
                completed_at=completed_at,
            )

        return ModelResult(
            invocation_id=invocation.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            response=str(item),
            exit_code=0,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def run_async(
        self,
        invocation: ModelInvocation,
        profile_name: str | None = None,
    ) -> ModelResult:
        """Async variant for FakeModelRunner."""
        return self.run(invocation, profile_name)

    def create_session(
        self,
        profile_name: str | None = None,
        strategy: ExecutionStrategy = ExecutionStrategy.STANDARD,
        responses: Sequence[Any] = (),
        conversation_id: str | None = None,
    ) -> FakeModelSession:
        """Create a fake persistent multi-turn session."""
        return FakeModelSession(
            responses=responses or self._responses,
            conversation_id=conversation_id,
            default_response=self.default_response,
        )


class FakeModelSession:
    """Fake model session conforming to ModelSession protocol for testing without quota."""

    def __init__(
        self,
        responses: Sequence[Any] = (),
        conversation_id: str | None = None,
        default_response: Any = None,
    ) -> None:
        self._conversation_id = ConversationId(conversation_id or "fake-conv-1")
        self._responses: list[Any] = list(responses)
        self.default_response = default_response
        self.sent_prompts: list[str] = []
        self._is_closed = False
        self._turn_count = 0

    @property
    def conversation_id(self) -> ConversationId:
        return self._conversation_id

    def send(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        """Send a prompt and return scripted result (ModelSession protocol)."""
        if self._is_closed:
            raise RuntimeError("Session is closed")
        self._turn_count += 1
        self.sent_prompts.append(prompt)
        inv_id = InvocationId(f"{self._conversation_id}-{self._turn_count}")

        if self._responses:
            item = self._responses.pop(0)
        elif self.default_response is not None:
            item = self.default_response
        else:
            item = f"Fake answer to '{prompt}'"

        started_at = datetime.now(timezone.utc).isoformat()
        completed_at = datetime.now(timezone.utc).isoformat()

        if isinstance(item, ModelResult):
            return item

        if item == "timeout":
            return ModelResult(
                invocation_id=inv_id,
                status=InvocationStatus.FAILED,
                error="Turn timed out",
                conversation_id=self._conversation_id,
                started_at=started_at,
                completed_at=completed_at,
            )

        if item in ("malformed", "malformed_json"):
            return ModelResult(
                invocation_id=inv_id,
                status=InvocationStatus.FAILED,
                response="{bad json",
                error="Malformed JSON in output",
                conversation_id=self._conversation_id,
                started_at=started_at,
                completed_at=completed_at,
            )

        if isinstance(item, dict):
            return ModelResult(
                invocation_id=inv_id,
                status=InvocationStatus.SUCCEEDED,
                response=json.dumps(item),
                structured_data=item,
                conversation_id=self._conversation_id,
                started_at=started_at,
                completed_at=completed_at,
            )

        return ModelResult(
            invocation_id=inv_id,
            status=InvocationStatus.SUCCEEDED,
            response=str(item),
            conversation_id=self._conversation_id,
            exit_code=0,
            started_at=started_at,
            completed_at=completed_at,
        )

    def ask(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        """Alias for send()."""
        return self.send(prompt, timeout_seconds=timeout_seconds)

    async def send_async(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        return self.send(prompt, timeout_seconds=timeout_seconds)

    async def ask_async(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        return self.send(prompt, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self._is_closed = True

    async def close_async(self) -> None:
        self.close()

    def __enter__(self) -> FakeModelSession:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    async def __aenter__(self) -> FakeModelSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.close()
