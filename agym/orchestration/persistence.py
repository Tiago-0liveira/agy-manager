"""AGYM orchestration run persistence.

Implements durable filesystem persistence for orchestration runs:
- Atomic run creation, state snapshots, and updates.
- Append-only event logging (events.jsonl) with deterministic ordering.
- Invocation and output persistence (invocations/*.json, outputs/*.txt).
- Assessment and final result persistence (assessment.json, final.json).
- Coordinator resume information (coordinator.json).
- Interrupted state transitions with active invocation tracking.
- Crash recovery inspection satisfying all post-crash observability needs.
- Conforms to RunStore and EventSink subsystem protocols.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    AuditRequest,
    AuditResult,
    BudgetUsage,
    ComplexityLevel,
    ConversationId,
    CoordinatorAction,
    CoordinatorObservation,
    EventSink,
    EventType,
    ExecutionStrategy,
    FailureClass,
    FleetView,
    InvocationId,
    InvocationStatus,
    ModelInvocation,
    ModelResult,
    OrchestrationBudget,
    OrchestrationEvent,
    RunId,
    RunMode,
    RunState,
    RunStatus,
    RunStore,
    TaskAssessment,
    TaskType,
    WorkerId,
    WorkerRequest,
    WorkerResult,
    WorkerRole,
    WorkspaceMode,
)

logger = logging.getLogger(__name__)

__all__ = [
    # Store and sinks
    "FileRunStore",
    # Data classes
    "CoordinatorInfo",
    "InvocationRecord",
    "CrashInspection",
    # Exceptions
    "PersistenceError",
    "RunAlreadyExistsError",
    "RunNotFoundError",
    "CorruptRunError",
    "InvocationNotFoundError",
    # Atomic file helpers
    "atomic_write_json",
    "atomic_write_text",
    "get_default_runs_dir",
    # Top-level functions
    "create_run",
    "load_run",
    "save_run",
    "list_runs",
    "save_result",
    "get_results",
    "emit_event",
    "get_events",
    "write_output",
    "read_output",
    "save_assessment",
    "get_assessment",
    "save_coordinator_info",
    "get_coordinator_info",
    "mark_run_interrupted",
    "finalize_run",
    "inspect_run",
]


# ============================================================================
# 1. Custom Exceptions
# ============================================================================


class PersistenceError(RuntimeError):
    """Base exception for orchestration persistence errors."""


class RunAlreadyExistsError(PersistenceError):
    """Raised when attempting to create a run that already exists."""


class RunNotFoundError(PersistenceError):
    """Raised when a specified run is not found."""


class CorruptRunError(PersistenceError):
    """Raised when run persistence artifacts are missing or unparseable."""


class InvocationNotFoundError(PersistenceError):
    """Raised when a requested invocation does not exist."""


# ============================================================================
# 2. Atomic File Helpers
# ============================================================================


def _chmod_private_dir(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass


def _chmod_private_file(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def atomic_write_json(path: Path, payload: Any, indent: int = 2) -> None:
    """Atomically write JSON data using temp file, flush, and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=indent, sort_keys=True)
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        _chmod_private_file(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write text data using temp file, flush, and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        _chmod_private_file(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def get_default_runs_dir() -> Path:
    """Determine the root directory for orchestration runs."""
    override = os.environ.get("AGYM_DATA_HOME")
    if override:
        root = Path(override).expanduser().resolve()
    else:
        from agym.profiles import _default_data_root

        root = _default_data_root()
    return root / "orchestrator" / "runs"


# ============================================================================
# 3. Persistence Auxiliary Dataclasses
# ============================================================================


@dataclass
class CoordinatorInfo:
    """Coordinator execution state required for resume."""

    conversation_id: ConversationId | None = None
    round_number: int = 0
    last_accepted_action: CoordinatorAction | None = None
    latest_observation: CoordinatorObservation | None = None

    def __post_init__(self) -> None:
        if self.conversation_id is not None:
            self.conversation_id = ConversationId(self.conversation_id)
        self.round_number = int(self.round_number)
        if isinstance(self.last_accepted_action, dict):
            self.last_accepted_action = CoordinatorAction.from_dict(self.last_accepted_action)
        if isinstance(self.latest_observation, dict):
            self.latest_observation = CoordinatorObservation.from_dict(self.latest_observation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": (
                str(self.conversation_id) if self.conversation_id is not None else None
            ),
            "round_number": self.round_number,
            "last_accepted_action": (
                self.last_accepted_action.to_dict()
                if self.last_accepted_action is not None
                else None
            ),
            "latest_observation": (
                self.latest_observation.to_dict()
                if self.latest_observation is not None
                else None
            ),
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoordinatorInfo:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for CoordinatorInfo, got {type(data).__name__}")
        raw_action = data.get("last_accepted_action")
        action = (
            CoordinatorAction.from_dict(raw_action)
            if isinstance(raw_action, dict)
            else raw_action
        )
        raw_obs = data.get("latest_observation")
        obs = (
            CoordinatorObservation.from_dict(raw_obs)
            if isinstance(raw_obs, dict)
            else raw_obs
        )
        return cls(
            conversation_id=(
                ConversationId(data["conversation_id"])
                if data.get("conversation_id") is not None
                else None
            ),
            round_number=int(data.get("round_number", 0)),
            last_accepted_action=action,
            latest_observation=obs,
        )

    @classmethod
    def from_json(cls, json_str: str) -> CoordinatorInfo:
        return cls.from_dict(json.loads(json_str))


@dataclass
class InvocationRecord:
    """Durable record of an individual worker invocation."""

    invocation_id: InvocationId
    run_id: RunId
    worker_id: WorkerId
    role: WorkerRole
    status: InvocationStatus = InvocationStatus.RUNNING
    strategy: ExecutionStrategy = ExecutionStrategy.STANDARD
    workspace_mode: WorkspaceMode = WorkspaceMode.READ_ONLY
    prompt: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    conversation_id: ConversationId | None = None
    output_file: str | None = None
    response: str | None = None
    structured_data: dict[str, Any] | None = None
    failure: FailureClass | None = None
    error: str | None = None
    exit_code: int | None = None
    usage: dict[str, Any] | None = None
    findings: list[str] = field(default_factory=list)
    active_at_interruption: bool = False

    def __post_init__(self) -> None:
        self.invocation_id = InvocationId(self.invocation_id)
        self.run_id = RunId(self.run_id)
        self.worker_id = WorkerId(self.worker_id)
        if isinstance(self.role, str) and not isinstance(self.role, WorkerRole):
            self.role = WorkerRole(self.role)
        if isinstance(self.status, str) and not isinstance(self.status, InvocationStatus):
            self.status = InvocationStatus(self.status)
        if isinstance(self.strategy, str) and not isinstance(self.strategy, ExecutionStrategy):
            self.strategy = ExecutionStrategy(self.strategy)
        if isinstance(self.workspace_mode, str) and not isinstance(self.workspace_mode, WorkspaceMode):
            self.workspace_mode = WorkspaceMode(self.workspace_mode)
        if self.conversation_id is not None:
            self.conversation_id = ConversationId(self.conversation_id)
        if self.failure is not None and isinstance(self.failure, str) and not isinstance(self.failure, FailureClass):
            self.failure = FailureClass(self.failure)
        if isinstance(self.findings, str):
            self.findings = [self.findings]
        else:
            self.findings = [str(f) for f in self.findings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": str(self.invocation_id),
            "run_id": str(self.run_id),
            "worker_id": str(self.worker_id),
            "role": self.role.value,
            "status": self.status.value,
            "strategy": self.strategy.value,
            "workspace_mode": self.workspace_mode.value,
            "prompt": self.prompt,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "conversation_id": (
                str(self.conversation_id) if self.conversation_id is not None else None
            ),
            "output_file": self.output_file,
            "response": self.response,
            "structured_data": self.structured_data,
            "failure": self.failure.value if self.failure is not None else None,
            "error": self.error,
            "exit_code": self.exit_code,
            "usage": self.usage,
            "findings": list(self.findings),
            "active_at_interruption": self.active_at_interruption,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvocationRecord:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for InvocationRecord, got {type(data).__name__}")
        raw_findings = data.get("findings", [])
        if isinstance(raw_findings, str):
            findings = [raw_findings]
        else:
            findings = [str(f) for f in raw_findings]
        return cls(
            invocation_id=InvocationId(data["invocation_id"]),
            run_id=RunId(data["run_id"]),
            worker_id=WorkerId(data["worker_id"]),
            role=WorkerRole(data["role"]),
            status=InvocationStatus(data.get("status", InvocationStatus.RUNNING)),
            strategy=ExecutionStrategy(data.get("strategy", ExecutionStrategy.STANDARD)),
            workspace_mode=WorkspaceMode(data.get("workspace_mode", WorkspaceMode.READ_ONLY)),
            prompt=str(data.get("prompt", "")),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            conversation_id=(
                ConversationId(data["conversation_id"])
                if data.get("conversation_id") is not None
                else None
            ),
            output_file=data.get("output_file"),
            response=data.get("response"),
            structured_data=data.get("structured_data"),
            failure=FailureClass(data["failure"]) if data.get("failure") is not None else None,
            error=data.get("error"),
            exit_code=int(data["exit_code"]) if data.get("exit_code") is not None else None,
            usage=data.get("usage"),
            findings=findings,
            active_at_interruption=bool(data.get("active_at_interruption", False)),
        )

    @classmethod
    def from_json(cls, json_str: str) -> InvocationRecord:
        return cls.from_dict(json.loads(json_str))


@dataclass
class CrashInspection:
    """Post-crash reconstruction of run state from filesystem artifacts alone."""

    run_id: RunId
    task: str
    round_number: int
    status: RunStatus
    completed_agents: list[WorkerId] = field(default_factory=list)
    failed_agents: list[WorkerId] = field(default_factory=list)
    in_progress_agents: list[WorkerId] = field(default_factory=list)
    coordinator_conversation_id: ConversationId | None = None
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)


# ============================================================================
# 4. FileRunStore Implementation
# ============================================================================


class FileRunStore:
    """File-backed storage and event sink for orchestration runs.

    Adheres strictly to the RunStore and EventSink protocols.
    Manages run layout:
      <AGYM_DATA_HOME>/orchestrator/runs/<run-id>/
          run.json
          task.txt
          assessment.json
          events.jsonl
          invocations/
              <invocation-id>.json
          outputs/
              <invocation-id>.txt
          coordinator.json
          final.json
    """

    def __init__(self, base_dir: Path | str | None = None) -> None:
        if base_dir is not None:
            self.base_dir = Path(base_dir).resolve()
        else:
            self.base_dir = get_default_runs_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(self.base_dir)

    def run_dir(self, run_id: RunId | str) -> Path:
        """Get the directory path for a specific run ID."""
        return self.base_dir / str(run_id)

    # ------------------------------------------------------------------------
    # Run Creation & Atomic Checks
    # ------------------------------------------------------------------------

    def is_partially_created(self, run_id: RunId | str) -> bool:
        """Check if a run directory exists in a partial/unfinalized creation state."""
        target = self.run_dir(run_id)
        if not target.exists():
            prefix = f".tmp_{run_id}_"
            try:
                return any(p.name.startswith(prefix) for p in self.base_dir.iterdir() if p.is_dir())
            except OSError:
                return False
        run_file = target / "run.json"
        if not run_file.exists():
            return True
        try:
            return run_file.stat().st_size == 0
        except OSError:
            return True

    def create_run(
        self,
        run_id: RunId | str,
        task: str,
        mode: RunMode = RunMode.PLAN,
        budget: OrchestrationBudget | None = None,
        created_at: str | None = None,
    ) -> RunState:
        """Create a new orchestration run atomically.

        Fails with RunAlreadyExistsError if the run ID already exists.
        """
        rid = RunId(run_id)
        final_dir = self.run_dir(rid)

        if final_dir.exists():
            raise RunAlreadyExistsError(f"Run '{rid}' already exists at {final_dir}")

        now = created_at or datetime.now(timezone.utc).isoformat()
        b = budget if budget is not None else OrchestrationBudget()
        state = RunState(
            run_id=rid,
            task=task,
            mode=mode,
            status=RunStatus.CREATED,
            budget=b,
            budget_usage=BudgetUsage(),
            created_at=now,
            updated_at=now,
        )

        stage_dir = self.base_dir / f".tmp_{rid}_{uuid.uuid4().hex}"
        try:
            stage_dir.mkdir(parents=True, exist_ok=False)
            _chmod_private_dir(stage_dir)

            (stage_dir / "invocations").mkdir(parents=True, exist_ok=True)
            _chmod_private_dir(stage_dir / "invocations")

            (stage_dir / "outputs").mkdir(parents=True, exist_ok=True)
            _chmod_private_dir(stage_dir / "outputs")

            atomic_write_text(stage_dir / "task.txt", task)
            atomic_write_json(stage_dir / "run.json", state.to_dict())

            # Initialize empty events.jsonl and write RUN_CREATED event
            initial_event = OrchestrationEvent(
                event_id=f"evt-{uuid.uuid4().hex[:8]}",
                run_id=rid,
                type=EventType.RUN_CREATED,
                timestamp=now,
                payload={"task": task, "mode": mode.value},
            )
            with open(stage_dir / "events.jsonl", "w", encoding="utf-8") as ef:
                ef.write(initial_event.to_json() + "\n")
                ef.flush()

            try:
                os.rename(stage_dir, final_dir)
            except OSError as exc:
                if final_dir.exists():
                    raise RunAlreadyExistsError(f"Run '{rid}' already exists at {final_dir}") from exc
                raise
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)

        return state

    # ------------------------------------------------------------------------
    # Run Loading and Saving (RunStore protocol)
    # ------------------------------------------------------------------------

    def save_run(self, state: RunState) -> None:
        """Persist or update a run state.

        Part of RunStore protocol.
        """
        target_dir = self.run_dir(state.run_id)
        if not target_dir.exists():
            raise RunNotFoundError(f"Run '{state.run_id}' does not exist at {target_dir}")

        state.updated_at = datetime.now(timezone.utc).isoformat()
        atomic_write_json(target_dir / "run.json", state.to_dict())

        # Ensure task.txt stays in sync if non-empty
        if state.task:
            task_file = target_dir / "task.txt"
            if not task_file.exists():
                atomic_write_text(task_file, state.task)

        # Ensure assessment.json is persisted if present
        if state.assessment is not None:
            assessment_file = target_dir / "assessment.json"
            if not assessment_file.exists():
                atomic_write_json(assessment_file, state.assessment.to_dict())

        # Ensure final.json is persisted if completed
        if state.status == RunStatus.COMPLETED and state.final_result is not None:
            final_file = target_dir / "final.json"
            if not final_file.exists():
                atomic_write_json(
                    final_file,
                    {
                        "run_id": str(state.run_id),
                        "final_result": state.final_result,
                        "status": state.status.value,
                        "completed_at": state.updated_at,
                    },
                )

    def get_run(self, run_id: RunId) -> RunState | None:
        """Retrieve a run state by ID. Returns None if not found.

        Part of RunStore protocol.
        """
        target_dir = self.run_dir(run_id)
        if not target_dir.exists():
            return None

        run_file = target_dir / "run.json"
        if not run_file.exists():
            return None

        try:
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return RunState.from_dict(data)
        except Exception as exc:
            logger.warning("Failed to load run state for %s: %s", run_id, exc)
            return None

    def load_run(self, run_id: RunId | str) -> RunState:
        """Strictly load a run state by ID.

        Raises RunNotFoundError if the run directory doesn't exist,
        or CorruptRunError if run.json is missing or corrupted.
        """
        rid = RunId(run_id)
        target_dir = self.run_dir(rid)
        if not target_dir.exists():
            raise RunNotFoundError(f"Run '{rid}' not found at {target_dir}")

        run_file = target_dir / "run.json"
        if not run_file.exists():
            raise CorruptRunError(f"Run directory exists for '{rid}' but run.json is missing")

        try:
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = RunState.from_dict(data)
        except Exception as exc:
            raise CorruptRunError(f"Run state for '{rid}' is corrupted: {exc}") from exc

        # Recover task if missing in run.json
        if not state.task:
            task_file = target_dir / "task.txt"
            if task_file.exists():
                try:
                    state.task = task_file.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    pass

        # Recover assessment if missing in run.json
        if state.assessment is None:
            assessment_file = target_dir / "assessment.json"
            if assessment_file.exists():
                try:
                    with open(assessment_file, "r", encoding="utf-8") as af:
                        state.assessment = TaskAssessment.from_dict(json.load(af))
                except Exception:
                    pass

        # Recover final result if missing in run.json
        if state.final_result is None:
            final_file = target_dir / "final.json"
            if final_file.exists():
                try:
                    with open(final_file, "r", encoding="utf-8") as ff:
                        fdata = json.load(ff)
                        res = fdata.get("final_result")
                        state.final_result = json.dumps(res) if isinstance(res, dict) else str(res)
                except Exception:
                    pass

        # Reconcile invocation_ids with directory contents
        invocations_dir = target_dir / "invocations"
        if invocations_dir.exists():
            known = set(state.invocation_ids)
            for inv_path in invocations_dir.glob("*.json"):
                inv_id = InvocationId(inv_path.stem)
                if inv_id not in known:
                    state.invocation_ids.append(inv_id)
                    known.add(inv_id)

        # Recover coordinator conversation ID if present in coordinator.json
        coordinator_file = target_dir / "coordinator.json"
        if coordinator_file.exists():
            try:
                with open(coordinator_file, "r", encoding="utf-8") as cf:
                    cdata = json.load(cf)
                    if cdata.get("conversation_id") and not state.coordinator_conversation_id:
                        state.coordinator_conversation_id = ConversationId(cdata["conversation_id"])
                    if cdata.get("round_number", 0) > state.round_number:
                        state.round_number = int(cdata["round_number"])
            except Exception:
                pass

        return state

    def list_runs(self, limit: int = 100) -> list[RunState]:
        """List recent run states sorted by creation date descending.

        Part of RunStore protocol.
        """
        runs: list[RunState] = []
        if not self.base_dir.exists():
            return runs

        for child in self.base_dir.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            run_file = child / "run.json"
            if not run_file.exists():
                continue
            try:
                with open(run_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                runs.append(RunState.from_dict(data))
            except Exception:
                continue

        # Sort descending by created_at
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    # ------------------------------------------------------------------------
    # Events Logging (EventSink protocol)
    # ------------------------------------------------------------------------

    def emit(self, event: OrchestrationEvent) -> None:
        """Emit an event by appending to events.jsonl.

        Does not rewrite full run state. Preserves deterministic order.
        Part of EventSink protocol.
        """
        target_dir = self.run_dir(event.run_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(target_dir)

        events_file = target_dir / "events.jsonl"
        with open(events_file, "a", encoding="utf-8") as handle:
            handle.write(event.to_json() + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass

    def get_events(self, run_id: RunId) -> list[OrchestrationEvent]:
        """Retrieve stored events for a run in deterministic append order.

        Part of EventSink protocol.
        """
        events: list[OrchestrationEvent] = []
        events_file = self.run_dir(run_id) / "events.jsonl"
        if not events_file.exists():
            return events

        with open(events_file, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    events.append(OrchestrationEvent.from_dict(data))
                except Exception as exc:
                    logger.warning("Skipping corrupted event line for %s: %s", run_id, exc)

        return events

    # ------------------------------------------------------------------------
    # Invocation Persistence
    # ------------------------------------------------------------------------

    def record_invocation_started(
        self,
        run_id: RunId | str,
        invocation: ModelInvocation | WorkerRequest | dict[str, Any],
        invocation_id: InvocationId | str | None = None,
    ) -> InvocationRecord:
        """Persist an invocation before execution and emit INVOCATION_STARTED."""
        rid = RunId(run_id)
        target_dir = self.run_dir(rid)
        if not target_dir.exists():
            raise RunNotFoundError(f"Run '{rid}' not found")

        invocations_dir = target_dir / "invocations"
        invocations_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(invocations_dir)

        if isinstance(invocation, ModelInvocation):
            iid = invocation.invocation_id
            wid = invocation.worker_id
            role = invocation.role
            strategy = invocation.strategy
            ws_mode = invocation.workspace_mode
            prompt = invocation.prompt
            conv_id = invocation.conversation_id
        elif isinstance(invocation, WorkerRequest):
            iid = InvocationId(invocation_id or f"inv-{invocation.worker_id}-{uuid.uuid4().hex[:6]}")
            wid = invocation.worker_id
            role = invocation.role
            strategy = invocation.strategy
            ws_mode = invocation.workspace_mode
            prompt = invocation.objective
            conv_id = None
        elif isinstance(invocation, dict):
            iid = InvocationId(invocation_id or invocation.get("invocation_id") or f"inv-{uuid.uuid4().hex[:8]}")
            wid = WorkerId(invocation.get("worker_id", "worker-unknown"))
            role = WorkerRole(invocation.get("role", WorkerRole.GENERAL))
            strategy = ExecutionStrategy(invocation.get("strategy", ExecutionStrategy.STANDARD))
            ws_mode = WorkspaceMode(invocation.get("workspace_mode", WorkspaceMode.READ_ONLY))
            prompt = str(invocation.get("prompt", invocation.get("objective", "")))
            conv_id = (
                ConversationId(invocation["conversation_id"])
                if invocation.get("conversation_id") is not None
                else None
            )
        else:
            raise ValueError(f"Unsupported invocation payload type: {type(invocation)}")

        now = datetime.now(timezone.utc).isoformat()
        record = InvocationRecord(
            invocation_id=iid,
            run_id=rid,
            worker_id=wid,
            role=role,
            status=InvocationStatus.RUNNING,
            strategy=strategy,
            workspace_mode=ws_mode,
            prompt=prompt,
            started_at=now,
            conversation_id=conv_id,
            output_file=f"outputs/{iid}.txt",
        )

        inv_file = invocations_dir / f"{iid}.json"
        atomic_write_json(inv_file, record.to_dict())

        # Emit INVOCATION_STARTED
        event = OrchestrationEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=rid,
            type=EventType.INVOCATION_STARTED,
            timestamp=now,
            payload={
                "invocation_id": str(iid),
                "worker_id": str(wid),
                "role": role.value,
            },
        )
        self.emit(event)

        # Update run.json invocation_ids if run state exists
        try:
            state = self.load_run(rid)
            if iid not in state.invocation_ids:
                state.invocation_ids.append(iid)
                self.save_run(state)
        except Exception:
            pass

        return record

    def record_invocation_completed(
        self,
        run_id: RunId | str,
        invocation_id: InvocationId | str,
        result: WorkerResult | AuditResult | ModelResult | dict[str, Any] | None = None,
        output_text: str | None = None,
    ) -> InvocationRecord:
        """Persist invocation output and completion metadata, and emit INVOCATION_COMPLETED."""
        rid = RunId(run_id)
        iid = InvocationId(invocation_id)
        target_dir = self.run_dir(rid)
        inv_file = target_dir / "invocations" / f"{iid}.json"

        # Load existing record or create baseline
        if inv_file.exists():
            with open(inv_file, "r", encoding="utf-8") as f:
                record = InvocationRecord.from_dict(json.load(f))
        else:
            wid = WorkerId(getattr(result, "worker_id", "worker-unknown") if result else "worker-unknown")
            role = getattr(result, "role", WorkerRole.GENERAL) if result else WorkerRole.GENERAL
            record = InvocationRecord(
                invocation_id=iid,
                run_id=rid,
                worker_id=wid,
                role=role,
                output_file=f"outputs/{iid}.txt",
            )

        now = datetime.now(timezone.utc).isoformat()
        record.status = InvocationStatus.SUCCEEDED
        record.completed_at = now

        # Extract output text and save outputs/<id>.txt
        resolved_output = output_text
        if resolved_output is None and result is not None:
            resolved_output = getattr(result, "response", None)
            if resolved_output is None and isinstance(result, dict):
                resolved_output = result.get("response")

        if resolved_output is not None:
            self.write_output(rid, iid, resolved_output)
            record.response = resolved_output

        # Extract structured data, findings, usage
        if result is not None:
            if hasattr(result, "structured_data") and result.structured_data:
                record.structured_data = result.structured_data
            elif isinstance(result, dict) and result.get("structured_data"):
                record.structured_data = result.get("structured_data")

            if hasattr(result, "findings") and result.findings:
                record.findings = list(result.findings)
            elif isinstance(result, dict) and result.get("findings"):
                record.findings = list(result.get("findings", []))

            if hasattr(result, "usage") and result.usage:
                record.usage = result.usage
            elif isinstance(result, dict) and result.get("usage"):
                record.usage = result.get("usage")

        atomic_write_json(inv_file, record.to_dict())

        # Emit INVOCATION_COMPLETED
        event = OrchestrationEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=rid,
            type=EventType.INVOCATION_COMPLETED,
            timestamp=now,
            payload={
                "invocation_id": str(iid),
                "worker_id": str(record.worker_id),
                "status": InvocationStatus.SUCCEEDED.value,
            },
        )
        self.emit(event)

        return record

    def record_invocation_failed(
        self,
        run_id: RunId | str,
        invocation_id: InvocationId | str,
        error: str | None = None,
        failure: FailureClass | str | None = None,
        exit_code: int | None = None,
        output_text: str | None = None,
    ) -> InvocationRecord:
        """Persist invocation failure metadata and emit INVOCATION_FAILED."""
        rid = RunId(run_id)
        iid = InvocationId(invocation_id)
        target_dir = self.run_dir(rid)
        inv_file = target_dir / "invocations" / f"{iid}.json"

        if inv_file.exists():
            with open(inv_file, "r", encoding="utf-8") as f:
                record = InvocationRecord.from_dict(json.load(f))
        else:
            record = InvocationRecord(
                invocation_id=iid,
                run_id=rid,
                worker_id=WorkerId("worker-unknown"),
                role=WorkerRole.GENERAL,
                output_file=f"outputs/{iid}.txt",
            )

        now = datetime.now(timezone.utc).isoformat()
        record.status = InvocationStatus.FAILED
        record.completed_at = now
        record.error = error
        record.failure = FailureClass(failure) if failure else FailureClass.UNRECOVERABLE
        record.exit_code = exit_code

        if output_text is not None:
            self.write_output(rid, iid, output_text)
            record.response = output_text

        atomic_write_json(inv_file, record.to_dict())

        # Emit INVOCATION_FAILED
        event = OrchestrationEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=rid,
            type=EventType.INVOCATION_FAILED,
            timestamp=now,
            payload={
                "invocation_id": str(iid),
                "worker_id": str(record.worker_id),
                "error": error,
                "failure": record.failure.value if record.failure else None,
            },
        )
        self.emit(event)

        return record

    def get_invocation(
        self,
        run_id: RunId | str,
        invocation_id: InvocationId | str,
    ) -> InvocationRecord:
        """Retrieve an invocation record by ID."""
        rid = RunId(run_id)
        iid = InvocationId(invocation_id)
        inv_file = self.run_dir(rid) / "invocations" / f"{iid}.json"
        if not inv_file.exists():
            raise InvocationNotFoundError(f"Invocation '{iid}' not found for run '{rid}'")

        with open(inv_file, "r", encoding="utf-8") as f:
            return InvocationRecord.from_dict(json.load(f))

    # ------------------------------------------------------------------------
    # RunStore Results Protocol Methods
    # ------------------------------------------------------------------------

    def save_result(self, run_id: RunId, result: WorkerResult | AuditResult) -> None:
        """Persist an individual worker or audit result.

        Part of RunStore protocol.
        """
        rid = RunId(run_id)
        iid = result.invocation_id or InvocationId(f"inv-{result.worker_id}")

        if result.status == InvocationStatus.SUCCEEDED:
            self.record_invocation_completed(
                rid,
                iid,
                result=result,
                output_text=result.response,
            )
        elif result.status == InvocationStatus.FAILED:
            self.record_invocation_failed(
                rid,
                iid,
                error=getattr(result, "error", None) or result.response or "Invocation failed",
                failure=result.failure,
                output_text=result.response,
            )
        else:
            # PENDING, RUNNING, CANCELLED
            record = InvocationRecord(
                invocation_id=iid,
                run_id=rid,
                worker_id=result.worker_id,
                role=getattr(result, "role", WorkerRole.GENERAL),
                status=result.status,
                started_at=result.started_at,
                completed_at=result.completed_at,
                conversation_id=getattr(result, "conversation_id", None),
                failure=result.failure,
                response=result.response,
            )
            inv_file = self.run_dir(rid) / "invocations" / f"{iid}.json"
            atomic_write_json(inv_file, record.to_dict())

    def get_results(self, run_id: RunId) -> list[WorkerResult | AuditResult]:
        """Retrieve all results for a run.

        Part of RunStore protocol.
        Malformed optional output files are handled gracefully without destroying the run.
        """
        rid = RunId(run_id)
        results: list[WorkerResult | AuditResult] = []
        inv_dir = self.run_dir(rid) / "invocations"
        if not inv_dir.exists():
            return results

        for child in inv_dir.glob("*.json"):
            try:
                with open(child, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                logger.warning("Skipping unreadable invocation file %s: %s", child, exc)
                continue

            iid = child.stem
            # Gracefully attempt to read output text if response is missing
            response = data.get("response")
            if not response:
                out_path = self.run_dir(rid) / "outputs" / f"{iid}.txt"
                if out_path.exists():
                    try:
                        response = out_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        response = None

            findings = data.get("findings", [])
            role_val = data.get("role", "")
            if findings or role_val == WorkerRole.AUDITOR.value:
                res = AuditResult(
                    worker_id=WorkerId(data.get("worker_id", iid)),
                    invocation_id=InvocationId(iid),
                    findings=findings,
                    response=response,
                    error=data.get("error"),
                    status=InvocationStatus(data.get("status", InvocationStatus.SUCCEEDED)),
                    failure=FailureClass(data["failure"]) if data.get("failure") is not None else None,
                    started_at=data.get("started_at"),
                    completed_at=data.get("completed_at"),
                )
            else:
                res = WorkerResult(
                    worker_id=WorkerId(data.get("worker_id", iid)),
                    role=WorkerRole(role_val) if role_val else WorkerRole.GENERAL,
                    status=InvocationStatus(data.get("status", InvocationStatus.SUCCEEDED)),
                    invocation_id=InvocationId(iid),
                    response=response,
                    error=data.get("error"),
                    structured_data=data.get("structured_data"),
                    conversation_id=(
                        ConversationId(data["conversation_id"])
                        if data.get("conversation_id") is not None
                        else None
                    ),
                    failure=FailureClass(data["failure"]) if data.get("failure") is not None else None,
                    started_at=data.get("started_at"),
                    completed_at=data.get("completed_at"),
                )
            results.append(res)

        return results

    # ------------------------------------------------------------------------
    # Output Files
    # ------------------------------------------------------------------------

    def write_output(
        self,
        run_id: RunId | str,
        invocation_id: InvocationId | str,
        text: str,
    ) -> Path:
        """Write invocation output text atomically to outputs/<id>.txt."""
        rid = RunId(run_id)
        iid = InvocationId(invocation_id)
        out_file = self.run_dir(rid) / "outputs" / f"{iid}.txt"
        atomic_write_text(out_file, text)
        return out_file

    def read_output(
        self,
        run_id: RunId | str,
        invocation_id: InvocationId | str,
    ) -> str | None:
        """Read invocation output text safely.

        Malformed or unreadable content is handled gracefully with replacement.
        """
        rid = RunId(run_id)
        iid = InvocationId(invocation_id)
        out_file = self.run_dir(rid) / "outputs" / f"{iid}.txt"
        if not out_file.exists():
            return None
        try:
            return out_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read output file for %s/%s: %s", rid, iid, exc)
            return None

    # ------------------------------------------------------------------------
    # Assessment & Coordinator State
    # ------------------------------------------------------------------------

    def save_assessment(
        self,
        run_id: RunId | str,
        assessment: TaskAssessment,
    ) -> None:
        """Persist task assessment to assessment.json, update run.json, and emit TASK_ASSESSED."""
        rid = RunId(run_id)
        target_dir = self.run_dir(rid)
        if not target_dir.exists():
            raise RunNotFoundError(f"Run '{rid}' not found")

        atomic_write_json(target_dir / "assessment.json", assessment.to_dict())

        # Update run.json
        state = self.load_run(rid)
        state.assessment = assessment
        self.save_run(state)

        # Emit TASK_ASSESSED event
        event = OrchestrationEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=rid,
            type=EventType.TASK_ASSESSED,
            payload={
                "task_type": assessment.task_type.value,
                "complexity": assessment.complexity.value,
                "summary": assessment.summary,
            },
        )
        self.emit(event)

    def get_assessment(self, run_id: RunId | str) -> TaskAssessment | None:
        """Read task assessment from assessment.json or run.json."""
        rid = RunId(run_id)
        target_dir = self.run_dir(rid)
        assessment_file = target_dir / "assessment.json"
        if assessment_file.exists():
            try:
                with open(assessment_file, "r", encoding="utf-8") as f:
                    return TaskAssessment.from_dict(json.load(f))
            except Exception as exc:
                logger.warning("Failed to parse assessment.json for %s: %s", rid, exc)

        # Fallback to run.json
        run_file = target_dir / "run.json"
        if run_file.exists():
            try:
                state = self.load_run(rid)
                return state.assessment
            except Exception:
                pass
        return None

    def save_coordinator_info(
        self,
        run_id: RunId | str,
        conversation_id: ConversationId | str | CoordinatorInfo | None,
        round_number: int = 0,
        last_accepted_action: CoordinatorAction | dict[str, Any] | None = None,
        latest_observation: CoordinatorObservation | dict[str, Any] | None = None,
    ) -> CoordinatorInfo:
        """Persist coordinator resume information into coordinator.json and run.json."""
        rid = RunId(run_id)
        target_dir = self.run_dir(rid)
        if not target_dir.exists():
            raise RunNotFoundError(f"Run '{rid}' not found")

        if isinstance(conversation_id, CoordinatorInfo):
            info = conversation_id
            cid = info.conversation_id
            round_number = info.round_number
            act = info.last_accepted_action
            obs = info.latest_observation
        else:
            cid = ConversationId(conversation_id) if conversation_id is not None else None
            act = (
                CoordinatorAction.from_dict(last_accepted_action)
                if isinstance(last_accepted_action, dict)
                else last_accepted_action
            )
            obs = (
                CoordinatorObservation.from_dict(latest_observation)
                if isinstance(latest_observation, dict)
                else latest_observation
            )
            info = CoordinatorInfo(
                conversation_id=cid,
                round_number=round_number,
                last_accepted_action=act,
                latest_observation=obs,
            )

        atomic_write_json(target_dir / "coordinator.json", info.to_dict())

        # Update run.json coordinator_conversation_id if present
        run_file = target_dir / "run.json"
        if run_file.exists():
            try:
                with open(run_file, "r", encoding="utf-8") as f:
                    rdata = json.load(f)
                rdata["coordinator_conversation_id"] = str(cid) if cid else None
                atomic_write_json(run_file, rdata)
            except Exception:
                pass

        return info

    def get_coordinator_info(self, run_id: RunId | str) -> CoordinatorInfo | None:
        """Load coordinator resume information from coordinator.json."""
        rid = RunId(run_id)
        coord_file = self.run_dir(rid) / "coordinator.json"
        if coord_file.exists():
            try:
                with open(coord_file, "r", encoding="utf-8") as f:
                    return CoordinatorInfo.from_dict(json.load(f))
            except Exception as exc:
                logger.warning("Failed to parse coordinator.json for %s: %s", rid, exc)

        # Fallback to run.json
        try:
            state = self.load_run(rid)
            if state.coordinator_conversation_id or state.round_number > 0:
                return CoordinatorInfo(
                    conversation_id=state.coordinator_conversation_id,
                    round_number=state.round_number,
                )
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------------
    # State Transitions: Interrupted & Finalized
    # ------------------------------------------------------------------------

    def mark_run_interrupted(
        self,
        run_id: RunId | str,
        reason: str = "",
    ) -> RunState:
        """Explicitly transition run to INTERRUPTED.

        Persists enough information to determine which invocations were still active.
        """
        rid = RunId(run_id)
        state = self.load_run(rid)
        now = datetime.now(timezone.utc).isoformat()

        active_invocation_ids: list[InvocationId] = []
        inv_dir = self.run_dir(rid) / "invocations"
        if inv_dir.exists():
            for child in inv_dir.glob("*.json"):
                try:
                    with open(child, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    record = InvocationRecord.from_dict(data)
                    if record.status in (InvocationStatus.RUNNING, InvocationStatus.PENDING):
                        active_invocation_ids.append(record.invocation_id)
                        record.active_at_interruption = True
                        record.completed_at = now
                        record.error = reason or "Run interrupted"
                        atomic_write_json(child, record.to_dict())
                except Exception as exc:
                    logger.warning("Error processing invocation %s during interruption: %s", child, exc)

        state.status = RunStatus.INTERRUPTED
        state.updated_at = now
        self.save_run(state)

        # Emit RUN_INTERRUPTED event with active invocations list
        event = OrchestrationEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=rid,
            type=EventType.RUN_INTERRUPTED,
            timestamp=now,
            payload={
                "reason": reason,
                "active_invocations": [str(i) for i in active_invocation_ids],
            },
        )
        self.emit(event)

        return state

    def finalize_run(
        self,
        run_id: RunId | str,
        final_result: str | dict[str, Any],
        status: RunStatus | str = RunStatus.COMPLETED,
    ) -> RunState:
        """Store final result independently in final.json and transition run state to COMPLETED."""
        rid = RunId(run_id)
        state = self.load_run(rid)
        now = datetime.now(timezone.utc).isoformat()
        status_enum = status if isinstance(status, RunStatus) else RunStatus(status)

        final_data = {
            "run_id": str(rid),
            "final_result": final_result,
            "completed_at": now,
            "status": status_enum.value,
        }
        atomic_write_json(self.run_dir(rid) / "final.json", final_data)

        state.status = status_enum
        state.final_result = (
            json.dumps(final_result) if isinstance(final_result, dict) else str(final_result)
        )
        state.updated_at = now
        self.save_run(state)

        # Emit RUN_COMPLETED event
        event = OrchestrationEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=rid,
            type=EventType.RUN_COMPLETED,
            timestamp=now,
            payload={
                "completed_at": now,
                "final_result": state.final_result,
                "summary": str(final_result)[:200],
            },
        )
        self.emit(event)

        return state

    # ------------------------------------------------------------------------
    # Post-Crash Inspection (Definition of Done)
    # ------------------------------------------------------------------------

    def inspect_run(self, run_id: RunId | str) -> CrashInspection:
        """Inspect the filesystem alone after a crash or interruption to understand run state.

        Answers:
        - what task was running
        - what round it reached
        - which agents completed
        - which failed
        - which were in progress
        - which conversation the coordinator used
        - what the budget usage was
        """
        rid = RunId(run_id)
        target_dir = self.run_dir(rid)
        if not target_dir.exists():
            raise RunNotFoundError(f"Run '{rid}' not found at {target_dir}")

        # 1. Task & RunStatus
        task = ""
        status = RunStatus.CREATED
        budget_usage = BudgetUsage()
        round_number = 0
        coordinator_conv_id: ConversationId | None = None

        run_file = target_dir / "run.json"
        if run_file.exists():
            try:
                with open(run_file, "r", encoding="utf-8") as f:
                    rdata = json.load(f)
                task = str(rdata.get("task", ""))
                status = RunStatus(rdata.get("status", RunStatus.CREATED))
                round_number = int(rdata.get("round_number", 0))
                if rdata.get("coordinator_conversation_id"):
                    coordinator_conv_id = ConversationId(rdata["coordinator_conversation_id"])
                if isinstance(rdata.get("budget_usage"), dict):
                    budget_usage = BudgetUsage.from_dict(rdata["budget_usage"])
            except Exception:
                pass

        if not task:
            task_file = target_dir / "task.txt"
            if task_file.exists():
                try:
                    task = task_file.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    pass

        # 2. Coordinator info check
        coord_file = target_dir / "coordinator.json"
        if coord_file.exists():
            try:
                with open(coord_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                if cdata.get("conversation_id"):
                    coordinator_conv_id = ConversationId(cdata["conversation_id"])
                if cdata.get("round_number", 0) > round_number:
                    round_number = int(cdata["round_number"])
            except Exception:
                pass

        # 3. Agents: completed, failed, in progress
        completed_agents: list[WorkerId] = []
        failed_agents: list[WorkerId] = []
        in_progress_agents: list[WorkerId] = []

        inv_dir = target_dir / "invocations"
        if inv_dir.exists():
            for child in inv_dir.glob("*.json"):
                try:
                    with open(child, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    wid = WorkerId(data.get("worker_id", child.stem))
                    istatus = data.get("status")
                    active_at_interruption = bool(data.get("active_at_interruption", False))

                    if istatus == InvocationStatus.SUCCEEDED.value:
                        completed_agents.append(wid)
                    elif istatus == InvocationStatus.FAILED.value and not active_at_interruption:
                        failed_agents.append(wid)
                    elif istatus in (InvocationStatus.RUNNING.value, InvocationStatus.PENDING.value) or active_at_interruption:
                        in_progress_agents.append(wid)
                    elif istatus == InvocationStatus.CANCELLED.value:
                        in_progress_agents.append(wid)
                except Exception:
                    continue

        return CrashInspection(
            run_id=rid,
            task=task,
            round_number=round_number,
            status=status,
            completed_agents=completed_agents,
            failed_agents=failed_agents,
            in_progress_agents=in_progress_agents,
            coordinator_conversation_id=coordinator_conv_id,
            budget_usage=budget_usage,
        )


# ============================================================================
# 5. Top-Level Module Functions
# ============================================================================

_DEFAULT_STORE: FileRunStore | None = None


def _get_store(store: FileRunStore | None = None) -> FileRunStore:
    if store is not None:
        return store
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = FileRunStore()
    return _DEFAULT_STORE


def create_run(
    run_id: RunId | str,
    task: str,
    mode: RunMode = RunMode.PLAN,
    budget: OrchestrationBudget | None = None,
    created_at: str | None = None,
    store: FileRunStore | None = None,
) -> RunState:
    """Create a new run directory layout and initial run.json."""
    return _get_store(store).create_run(
        run_id=run_id,
        task=task,
        mode=mode,
        budget=budget,
        created_at=created_at,
    )


def load_run(run_id: RunId | str, store: FileRunStore | None = None) -> RunState:
    """Load run state from disk, recovering artifacts."""
    return _get_store(store).load_run(run_id)


def save_run(state: RunState, store: FileRunStore | None = None) -> None:
    """Save updated run state snapshot."""
    _get_store(store).save_run(state)


def list_runs(limit: int = 100, store: FileRunStore | None = None) -> list[RunState]:
    """List recent runs."""
    return _get_store(store).list_runs(limit=limit)


def save_result(
    run_id: RunId | str,
    result: WorkerResult | AuditResult,
    store: FileRunStore | None = None,
) -> None:
    """Save an individual worker or audit result."""
    _get_store(store).save_result(RunId(run_id), result)


def get_results(
    run_id: RunId | str,
    store: FileRunStore | None = None,
) -> list[WorkerResult | AuditResult]:
    """Get all results for a run."""
    return _get_store(store).get_results(RunId(run_id))


def emit_event(event: OrchestrationEvent, store: FileRunStore | None = None) -> None:
    """Append an event to events.jsonl."""
    _get_store(store).emit(event)


def get_events(
    run_id: RunId | str,
    store: FileRunStore | None = None,
) -> list[OrchestrationEvent]:
    """Get all events for a run in deterministic sequence."""
    return _get_store(store).get_events(RunId(run_id))


def write_output(
    run_id: RunId | str,
    invocation_id: InvocationId | str,
    text: str,
    store: FileRunStore | None = None,
) -> Path:
    """Write invocation output text."""
    return _get_store(store).write_output(run_id, invocation_id, text)


def read_output(
    run_id: RunId | str,
    invocation_id: InvocationId | str,
    store: FileRunStore | None = None,
) -> str | None:
    """Read invocation output text."""
    return _get_store(store).read_output(run_id, invocation_id)


def save_assessment(
    run_id: RunId | str,
    assessment: TaskAssessment,
    store: FileRunStore | None = None,
) -> None:
    """Persist task assessment."""
    _get_store(store).save_assessment(run_id, assessment)


def get_assessment(
    run_id: RunId | str,
    store: FileRunStore | None = None,
) -> TaskAssessment | None:
    """Load task assessment."""
    return _get_store(store).get_assessment(run_id)


def save_coordinator_info(
    run_id: RunId | str,
    conversation_id: ConversationId | str | CoordinatorInfo | None,
    round_number: int = 0,
    last_accepted_action: CoordinatorAction | dict[str, Any] | None = None,
    latest_observation: CoordinatorObservation | dict[str, Any] | None = None,
    store: FileRunStore | None = None,
) -> CoordinatorInfo:
    """Save coordinator resume information."""
    return _get_store(store).save_coordinator_info(
        run_id=run_id,
        conversation_id=conversation_id,
        round_number=round_number,
        last_accepted_action=last_accepted_action,
        latest_observation=latest_observation,
    )


def get_coordinator_info(
    run_id: RunId | str,
    store: FileRunStore | None = None,
) -> CoordinatorInfo | None:
    """Load coordinator resume information."""
    return _get_store(store).get_coordinator_info(run_id)


def mark_run_interrupted(
    run_id: RunId | str,
    reason: str = "",
    store: FileRunStore | None = None,
) -> RunState:
    """Mark run as INTERRUPTED, recording active invocations."""
    return _get_store(store).mark_run_interrupted(run_id, reason)


def finalize_run(
    run_id: RunId | str,
    final_result: str | dict[str, Any],
    store: FileRunStore | None = None,
    status: RunStatus | str = RunStatus.COMPLETED,
) -> RunState:
    """Store final result independently in final.json and mark COMPLETED."""
    return _get_store(store).finalize_run(run_id, final_result, status=status)


def inspect_run(
    run_id: RunId | str,
    store: FileRunStore | None = None,
) -> CrashInspection:
    """Reconstruct run state from filesystem artifacts alone."""
    return _get_store(store).inspect_run(run_id)
