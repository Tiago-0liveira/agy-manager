"""Public orchestration subsystem contracts.

This module defines the complete public data types, enums, dataclasses,
and protocols for the AGYM orchestration subsystem.

All contracts are self-contained and depend only on the Python standard library.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    # Identifiers
    "RunId",
    "WorkerId",
    "InvocationId",
    "ActionId",
    "LeaseId",
    "ConversationId",
    # Enums
    "TaskType",
    "ComplexityLevel",
    "RunMode",
    "WorkerRole",
    "ExecutionStrategy",
    "WorkspaceMode",
    "RunStatus",
    "InvocationStatus",
    "ActionKind",
    "FailureClass",
    "EventType",
    # Coordinator & Worker Dataclasses
    "TaskAssessment",
    "WorkerRequest",
    "WorkerResult",
    "AuditRequest",
    "AuditResult",
    "CoordinatorAction",
    "CoordinatorObservation",
    # Fleet & Lease Dataclasses
    "FleetView",
    "ProfileCapacity",
    "FleetSnapshot",
    "ProfileLease",
    # Budget Dataclasses
    "OrchestrationBudget",
    "BudgetUsage",
    # Model Dataclasses
    "ModelInvocation",
    "ModelResult",
    # State & Event Dataclasses
    "RunState",
    "OrchestrationEvent",
    # Protocols
    "ModelSession",
    "ModelRunner",
    "CoordinatorClient",
    "ProfileScheduler",
    "ProfileLeaseManager",
    "RunStore",
    "EventSink",
]


# ============================================================================
# 1. Identifier Types
# ============================================================================


class RunId(str):
    """Conceptual identifier for an orchestration run."""


class WorkerId(str):
    """Conceptual identifier for a worker within an orchestration run."""


class InvocationId(str):
    """Conceptual identifier for an individual model invocation."""


class ActionId(str):
    """Conceptual identifier for a coordinator action."""


class LeaseId(str):
    """Conceptual identifier for an acquired profile lease."""


class ConversationId(str):
    """Conceptual identifier for a multi-turn conversation/session."""


# ============================================================================
# 2. Frozen Subsystem Enums
# ============================================================================


class _CaseInsensitiveStrEnum(str, Enum):
    """Base string enum supporting case-insensitive lookup."""

    @classmethod
    def _missing_(cls, value: object) -> Any:
        if isinstance(value, str):
            normalized = value.strip().upper()
            for member in cls:
                if member.value.upper() == normalized or member.name.upper() == normalized:
                    return member
        return None

    def __str__(self) -> str:
        return self.value


class TaskType(_CaseInsensitiveStrEnum):
    """Classification of the primary user task."""

    GENERAL = "GENERAL"
    ARCHITECTURE = "ARCHITECTURE"
    DEBUGGING = "DEBUGGING"
    REVIEW = "REVIEW"
    REFACTOR = "REFACTOR"
    IMPLEMENTATION = "IMPLEMENTATION"
    RESEARCH = "RESEARCH"


class ComplexityLevel(_CaseInsensitiveStrEnum):
    """Assessed difficulty and scope of a task."""

    TRIVIAL = "TRIVIAL"
    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    LARGE = "LARGE"
    VERY_LARGE = "VERY_LARGE"


class RunMode(_CaseInsensitiveStrEnum):
    """Operating mode of the orchestration run."""

    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"


class WorkerRole(_CaseInsensitiveStrEnum):
    """Specific role assigned to an orchestrated worker."""

    GENERAL = "GENERAL"
    ARCHITECTURE = "ARCHITECTURE"
    ALTERNATIVE_DESIGN = "ALTERNATIVE_DESIGN"
    MINIMAL_CHANGE = "MINIMAL_CHANGE"
    DEBUGGING = "DEBUGGING"
    SECURITY = "SECURITY"
    TESTING = "TESTING"
    PERFORMANCE = "PERFORMANCE"
    MAINTAINABILITY = "MAINTAINABILITY"
    IMPLEMENTATION_REVIEW = "IMPLEMENTATION_REVIEW"
    AUDITOR = "AUDITOR"
    SYNTHESIZER = "SYNTHESIZER"
    EXECUTOR = "EXECUTOR"


class ExecutionStrategy(_CaseInsensitiveStrEnum):
    """Effort or execution tier requested for model reasoning."""

    STANDARD = "STANDARD"
    HIGH_EFFORT = "HIGH_EFFORT"
    BOOST = "BOOST"


class WorkspaceMode(_CaseInsensitiveStrEnum):
    """Filesystem interaction policy for a worker."""

    READ_ONLY = "READ_ONLY"
    MUTATING = "MUTATING"


class RunStatus(_CaseInsensitiveStrEnum):
    """Overall status of an orchestration run."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class InvocationStatus(_CaseInsensitiveStrEnum):
    """Status of an individual model invocation or worker task."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ActionKind(_CaseInsensitiveStrEnum):
    """Kind of action proposed by the coordinator."""

    RUN_WORKERS = "RUN_WORKERS"
    RUN_AUDITORS = "RUN_AUDITORS"
    RUN_SYNTHESIS = "RUN_SYNTHESIS"
    RUN_EXECUTOR = "RUN_EXECUTOR"
    FINALIZE = "FINALIZE"


class FailureClass(_CaseInsensitiveStrEnum):
    """Classification of execution or model failures."""

    RECOVERABLE = "RECOVERABLE"
    RETRYABLE = "RETRYABLE"
    UNRECOVERABLE = "UNRECOVERABLE"


class EventType(_CaseInsensitiveStrEnum):
    """EventType emitted throughout orchestration lifecycle."""

    RUN_CREATED = "RUN_CREATED"
    TASK_ASSESSED = "TASK_ASSESSED"

    ACTION_REQUESTED = "ACTION_REQUESTED"
    ACTION_ACCEPTED = "ACTION_ACCEPTED"
    ACTION_REJECTED = "ACTION_REJECTED"

    PROFILE_LEASED = "PROFILE_LEASED"
    PROFILE_RELEASED = "PROFILE_RELEASED"

    INVOCATION_STARTED = "INVOCATION_STARTED"
    INVOCATION_COMPLETED = "INVOCATION_COMPLETED"
    INVOCATION_FAILED = "INVOCATION_FAILED"

    ROUND_STARTED = "ROUND_STARTED"
    ROUND_COMPLETED = "ROUND_COMPLETED"

    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"


# ============================================================================
# 3. Assessment & Worker Contracts
# ============================================================================


@dataclass
class TaskAssessment:
    """Initial assessment of a task produced before execution begins.

    This object describes the task. It does not execute anything, choose
    physical profiles, or configure processes.
    """

    task_type: TaskType
    complexity: ComplexityLevel
    confidence: float
    mutation_required: bool
    repository_scope: str
    value_of_parallel_reasoning: float = 0.0
    value_of_auditing: float = 0.0
    summary: str = ""
    proposed_initial_work: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.task_type, str) and not isinstance(self.task_type, TaskType):
            self.task_type = TaskType(self.task_type)
        if isinstance(self.complexity, str) and not isinstance(self.complexity, ComplexityLevel):
            self.complexity = ComplexityLevel(self.complexity)

        self.confidence = float(self.confidence)
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {self.confidence}")

        self.value_of_parallel_reasoning = float(self.value_of_parallel_reasoning)
        if not (0.0 <= self.value_of_parallel_reasoning <= 1.0):
            raise ValueError(
                f"value_of_parallel_reasoning must be between 0.0 and 1.0, got {self.value_of_parallel_reasoning}"
            )

        self.value_of_auditing = float(self.value_of_auditing)
        if not (0.0 <= self.value_of_auditing <= 1.0):
            raise ValueError(
                f"value_of_auditing must be between 0.0 and 1.0, got {self.value_of_auditing}"
            )

        if isinstance(self.proposed_initial_work, str):
            self.proposed_initial_work = [self.proposed_initial_work]
        else:
            self.proposed_initial_work = [str(item) for item in self.proposed_initial_work]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type.value,
            "complexity": self.complexity.value,
            "confidence": self.confidence,
            "mutation_required": self.mutation_required,
            "repository_scope": self.repository_scope,
            "value_of_parallel_reasoning": self.value_of_parallel_reasoning,
            "value_of_auditing": self.value_of_auditing,
            "summary": self.summary,
            "proposed_initial_work": list(self.proposed_initial_work),
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskAssessment:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for TaskAssessment, got {type(data).__name__}")
        return cls(
            task_type=TaskType(data["task_type"]),
            complexity=ComplexityLevel(data["complexity"]),
            confidence=float(data["confidence"]),
            mutation_required=bool(data["mutation_required"]),
            repository_scope=str(data.get("repository_scope", "")),
            value_of_parallel_reasoning=float(data.get("value_of_parallel_reasoning", 0.0)),
            value_of_auditing=float(data.get("value_of_auditing", 0.0)),
            summary=str(data.get("summary", "")),
            proposed_initial_work=list(data.get("proposed_initial_work", [])),
        )

    @classmethod
    def from_json(cls, json_str: str) -> TaskAssessment:
        return cls.from_dict(json.loads(json_str))


@dataclass
class WorkerRequest:
    """Request from the coordinator to run a worker.

    Critical invariant: no physical profile names, argv, executable path,
    or process environment are specified here.
    """

    worker_id: WorkerId
    role: WorkerRole
    strategy: ExecutionStrategy = ExecutionStrategy.STANDARD
    workspace_mode: WorkspaceMode = WorkspaceMode.READ_ONLY
    objective: str = ""
    context_worker_ids: list[WorkerId] = field(default_factory=list)
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        self.worker_id = WorkerId(self.worker_id)
        if isinstance(self.role, str) and not isinstance(self.role, WorkerRole):
            self.role = WorkerRole(self.role)
        if isinstance(self.strategy, str) and not isinstance(self.strategy, ExecutionStrategy):
            self.strategy = ExecutionStrategy(self.strategy)
        if isinstance(self.workspace_mode, str) and not isinstance(self.workspace_mode, WorkspaceMode):
            self.workspace_mode = WorkspaceMode(self.workspace_mode)
        self.context_worker_ids = [WorkerId(w) for w in self.context_worker_ids]
        self.timeout_seconds = float(self.timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {self.timeout_seconds}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": str(self.worker_id),
            "role": self.role.value,
            "strategy": self.strategy.value,
            "workspace_mode": self.workspace_mode.value,
            "objective": self.objective,
            "context_worker_ids": [str(w) for w in self.context_worker_ids],
            "timeout_seconds": self.timeout_seconds,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerRequest:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for WorkerRequest, got {type(data).__name__}")
        return cls(
            worker_id=WorkerId(data["worker_id"]),
            role=WorkerRole(data["role"]),
            strategy=ExecutionStrategy(data.get("strategy", ExecutionStrategy.STANDARD)),
            workspace_mode=WorkspaceMode(data.get("workspace_mode", WorkspaceMode.READ_ONLY)),
            objective=str(data.get("objective", "")),
            context_worker_ids=[WorkerId(w) for w in data.get("context_worker_ids", [])],
            timeout_seconds=float(data.get("timeout_seconds", 300.0)),
        )

    @classmethod
    def from_json(cls, json_str: str) -> WorkerRequest:
        return cls.from_dict(json.loads(json_str))


@dataclass
class WorkerResult:
    """Result of an executed worker invocation."""

    worker_id: WorkerId
    role: WorkerRole
    status: InvocationStatus = InvocationStatus.SUCCEEDED
    invocation_id: InvocationId | None = None
    response: str | None = None
    error: str | None = None
    structured_data: dict[str, Any] | None = None
    conversation_id: ConversationId | None = None
    failure: FailureClass | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.worker_id = WorkerId(self.worker_id)
        if isinstance(self.role, str) and not isinstance(self.role, WorkerRole):
            self.role = WorkerRole(self.role)
        if isinstance(self.status, str) and not isinstance(self.status, InvocationStatus):
            self.status = InvocationStatus(self.status)
        if self.invocation_id is not None:
            self.invocation_id = InvocationId(self.invocation_id)
        if self.conversation_id is not None:
            self.conversation_id = ConversationId(self.conversation_id)
        if self.failure is not None and isinstance(self.failure, str) and not isinstance(self.failure, FailureClass):
            self.failure = FailureClass(self.failure)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": str(self.worker_id),
            "role": self.role.value,
            "status": self.status.value,
            "invocation_id": str(self.invocation_id) if self.invocation_id is not None else None,
            "response": self.response,
            "error": self.error,
            "structured_data": self.structured_data,
            "conversation_id": str(self.conversation_id) if self.conversation_id is not None else None,
            "failure": self.failure.value if self.failure is not None else None,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerResult:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for WorkerResult, got {type(data).__name__}")
        return cls(
            worker_id=WorkerId(data["worker_id"]),
            role=WorkerRole(data["role"]),
            status=InvocationStatus(data.get("status", InvocationStatus.SUCCEEDED)),
            invocation_id=InvocationId(data["invocation_id"]) if data.get("invocation_id") is not None else None,
            response=data.get("response"),
            error=data.get("error"),
            structured_data=data.get("structured_data"),
            conversation_id=ConversationId(data["conversation_id"]) if data.get("conversation_id") is not None else None,
            failure=FailureClass(data["failure"]) if data.get("failure") is not None else None,
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> WorkerResult:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 4. Audit Contracts
# ============================================================================


@dataclass
class AuditRequest:
    """Request from the coordinator to run an auditor against prior workers.

    Auditors reference prior worker IDs rather than embedding copies of results.
    """

    worker_id: WorkerId
    target_worker_ids: list[WorkerId] = field(default_factory=list)
    focus: str = ""
    strategy: ExecutionStrategy = ExecutionStrategy.STANDARD
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        self.worker_id = WorkerId(self.worker_id)
        self.target_worker_ids = [WorkerId(w) for w in self.target_worker_ids]
        if isinstance(self.strategy, str) and not isinstance(self.strategy, ExecutionStrategy):
            self.strategy = ExecutionStrategy(self.strategy)
        self.timeout_seconds = float(self.timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {self.timeout_seconds}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": str(self.worker_id),
            "target_worker_ids": [str(w) for w in self.target_worker_ids],
            "focus": self.focus,
            "strategy": self.strategy.value,
            "timeout_seconds": self.timeout_seconds,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditRequest:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for AuditRequest, got {type(data).__name__}")
        return cls(
            worker_id=WorkerId(data["worker_id"]),
            target_worker_ids=[WorkerId(w) for w in data.get("target_worker_ids", [])],
            focus=str(data.get("focus", "")),
            strategy=ExecutionStrategy(data.get("strategy", ExecutionStrategy.STANDARD)),
            timeout_seconds=float(data.get("timeout_seconds", 300.0)),
        )

    @classmethod
    def from_json(cls, json_str: str) -> AuditRequest:
        return cls.from_dict(json.loads(json_str))


@dataclass
class AuditResult:
    """Result produced by an auditor invocation."""

    worker_id: WorkerId
    invocation_id: InvocationId | None = None
    findings: list[str] = field(default_factory=list)
    response: str | None = None
    error: str | None = None
    status: InvocationStatus = InvocationStatus.SUCCEEDED
    failure: FailureClass | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.worker_id = WorkerId(self.worker_id)
        if self.invocation_id is not None:
            self.invocation_id = InvocationId(self.invocation_id)
        if isinstance(self.findings, str):
            self.findings = [self.findings]
        else:
            self.findings = [str(f) for f in self.findings]
        if isinstance(self.status, str) and not isinstance(self.status, InvocationStatus):
            self.status = InvocationStatus(self.status)
        if self.failure is not None and isinstance(self.failure, str) and not isinstance(self.failure, FailureClass):
            self.failure = FailureClass(self.failure)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": str(self.worker_id),
            "invocation_id": str(self.invocation_id) if self.invocation_id is not None else None,
            "findings": list(self.findings),
            "response": self.response,
            "error": self.error,
            "status": self.status.value,
            "failure": self.failure.value if self.failure is not None else None,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditResult:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for AuditResult, got {type(data).__name__}")
        raw_findings = data.get("findings", [])
        if isinstance(raw_findings, str):
            findings = [raw_findings]
        else:
            findings = [str(f) for f in raw_findings]
        return cls(
            worker_id=WorkerId(data["worker_id"]),
            invocation_id=InvocationId(data["invocation_id"]) if data.get("invocation_id") is not None else None,
            findings=findings,
            response=data.get("response"),
            error=data.get("error"),
            status=InvocationStatus(data.get("status", InvocationStatus.SUCCEEDED)),
            failure=FailureClass(data["failure"]) if data.get("failure") is not None else None,
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> AuditResult:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 5. Coordinator Action & Observation Contracts
# ============================================================================


@dataclass
class CoordinatorAction:
    """Action proposed by the coordinator.

    Structural validation is enforced at instantiation:
    - FINALIZE cannot contain workers or auditors, and may contain final_response.
    - Non-FINALIZE actions cannot contain final_response.
    - RUN_WORKERS requires at least one worker, no auditors, no mutating workers, and no executor/auditor roles.
    - RUN_AUDITORS requires at least one auditor and no workers.
    - RUN_SYNTHESIS requires at least one worker with role SYNTHESIZER, no mutating workers, and no auditors.
    - RUN_EXECUTOR requires exactly one worker with role EXECUTOR, no auditors.
    """

    action_id: ActionId
    kind: ActionKind
    workers: list[WorkerRequest] = field(default_factory=list)
    auditors: list[AuditRequest] = field(default_factory=list)
    reason_summary: str = ""
    final_response: str | None = None

    def __post_init__(self) -> None:
        self.action_id = ActionId(self.action_id)
        if isinstance(self.kind, str) and not isinstance(self.kind, ActionKind):
            self.kind = ActionKind(self.kind)
        self.workers = [
            WorkerRequest.from_dict(w) if isinstance(w, dict) else w
            for w in self.workers
        ]
        self.auditors = [
            AuditRequest.from_dict(a) if isinstance(a, dict) else a
            for a in self.auditors
        ]
        self.validate()

    def validate(self) -> None:
        """Validate structural constraints on the coordinator action."""
        if self.kind == ActionKind.FINALIZE:
            if self.workers:
                raise ValueError("FINALIZE action cannot contain workers")
            if self.auditors:
                raise ValueError("FINALIZE action cannot contain auditors")
        else:
            if self.final_response is not None:
                raise ValueError("final_response is only permitted for FINALIZE actions")

        if self.kind == ActionKind.RUN_WORKERS:
            if not self.workers:
                raise ValueError("RUN_WORKERS action requires at least one worker")
            if self.auditors:
                raise ValueError("RUN_WORKERS action cannot contain auditors")
            if any(w.workspace_mode == WorkspaceMode.MUTATING for w in self.workers):
                raise ValueError("Mutating workers are only permitted in RUN_EXECUTOR actions")
            if any(w.role == WorkerRole.EXECUTOR for w in self.workers):
                raise ValueError("Worker with EXECUTOR role must be scheduled in RUN_EXECUTOR")
            if any(w.role == WorkerRole.AUDITOR for w in self.workers):
                raise ValueError("Worker with AUDITOR role must be scheduled in RUN_AUDITORS")

        elif self.kind == ActionKind.RUN_AUDITORS:
            if not self.auditors:
                raise ValueError("RUN_AUDITORS action requires at least one auditor")
            if self.workers:
                raise ValueError("RUN_AUDITORS action cannot contain workers")

        elif self.kind == ActionKind.RUN_SYNTHESIS:
            if not self.workers:
                raise ValueError("RUN_SYNTHESIS action requires at least one worker")
            if self.auditors:
                raise ValueError("RUN_SYNTHESIS action cannot contain auditors")
            if any(w.role != WorkerRole.SYNTHESIZER for w in self.workers):
                raise ValueError("RUN_SYNTHESIS action workers must have role SYNTHESIZER")
            if any(w.workspace_mode == WorkspaceMode.MUTATING for w in self.workers):
                raise ValueError("Mutating workers are only permitted in RUN_EXECUTOR actions")

        elif self.kind == ActionKind.RUN_EXECUTOR:
            if len(self.workers) != 1:
                raise ValueError("RUN_EXECUTOR action must contain exactly one worker")
            if self.auditors:
                raise ValueError("RUN_EXECUTOR action cannot contain auditors")
            if self.workers[0].role != WorkerRole.EXECUTOR:
                raise ValueError("RUN_EXECUTOR worker must have role EXECUTOR")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": str(self.action_id),
            "kind": self.kind.value,
            "workers": [w.to_dict() for w in self.workers],
            "auditors": [a.to_dict() for a in self.auditors],
            "reason_summary": self.reason_summary,
            "final_response": self.final_response,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoordinatorAction:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for CoordinatorAction, got {type(data).__name__}")
        return cls(
            action_id=ActionId(data["action_id"]),
            kind=ActionKind(data["kind"]),
            workers=[
                WorkerRequest.from_dict(w) if isinstance(w, dict) else w
                for w in data.get("workers", [])
            ],
            auditors=[
                AuditRequest.from_dict(a) if isinstance(a, dict) else a
                for a in data.get("auditors", [])
            ],
            reason_summary=str(data.get("reason_summary", "")),
            final_response=data.get("final_response"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> CoordinatorAction:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 6. Fleet Information Contracts
# ============================================================================


@dataclass
class FleetView:
    """Coordinator-visible capability summary of the profile fleet.

    Exposes aggregate capacity information only. Does NOT expose profile names,
    filesystem paths, credentials, tokens, or emails.
    """

    available_profiles: int = 0
    max_parallel: int = 0
    standard_capacity: int = 0
    high_effort_capacity: int = 0
    boost_capacity: int = 0
    quota_band_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_profiles": self.available_profiles,
            "max_parallel": self.max_parallel,
            "standard_capacity": self.standard_capacity,
            "high_effort_capacity": self.high_effort_capacity,
            "boost_capacity": self.boost_capacity,
            "quota_band_counts": dict(self.quota_band_counts),
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FleetView:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for FleetView, got {type(data).__name__}")
        return cls(
            available_profiles=int(data.get("available_profiles", 0)),
            max_parallel=int(data.get("max_parallel", 0)),
            standard_capacity=int(data.get("standard_capacity", 0)),
            high_effort_capacity=int(data.get("high_effort_capacity", 0)),
            boost_capacity=int(data.get("boost_capacity", 0)),
            quota_band_counts={str(k): int(v) for k, v in data.get("quota_band_counts", {}).items()},
        )

    @classmethod
    def from_json(cls, json_str: str) -> FleetView:
        return cls.from_dict(json.loads(json_str))


@dataclass
class ProfileCapacity:
    """Internal AGYM record of an individual profile's capacity.

    This is internal to AGYM engine/scheduler and is never sent to the coordinator.
    """

    profile_name: str
    five_hour_remaining: float | None = None
    weekly_remaining: float | None = None
    leased: bool = False
    auth_ready: bool = True
    recent_failures: int = 0
    observed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "five_hour_remaining": self.five_hour_remaining,
            "weekly_remaining": self.weekly_remaining,
            "leased": self.leased,
            "auth_ready": self.auth_ready,
            "recent_failures": self.recent_failures,
            "observed_at": self.observed_at,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileCapacity:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for ProfileCapacity, got {type(data).__name__}")
        return cls(
            profile_name=str(data["profile_name"]),
            five_hour_remaining=float(data["five_hour_remaining"]) if data.get("five_hour_remaining") is not None else None,
            weekly_remaining=float(data["weekly_remaining"]) if data.get("weekly_remaining") is not None else None,
            leased=bool(data.get("leased", False)),
            auth_ready=bool(data.get("auth_ready", True)),
            recent_failures=int(data.get("recent_failures", 0)),
            observed_at=data.get("observed_at"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> ProfileCapacity:
        return cls.from_dict(json.loads(json_str))


@dataclass
class FleetSnapshot:
    """Internal AGYM snapshot of the complete fleet at a point in time."""

    profiles: list[ProfileCapacity] = field(default_factory=list)
    observed_at: str | None = None

    def __post_init__(self) -> None:
        self.profiles = [
            ProfileCapacity.from_dict(p) if isinstance(p, dict) else p
            for p in self.profiles
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles": [p.to_dict() for p in self.profiles],
            "observed_at": self.observed_at,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FleetSnapshot:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for FleetSnapshot, got {type(data).__name__}")
        return cls(
            profiles=[
                ProfileCapacity.from_dict(p) if isinstance(p, dict) else p
                for p in data.get("profiles", [])
            ],
            observed_at=data.get("observed_at"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> FleetSnapshot:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 7. Lease Contracts
# ============================================================================


@dataclass
class ProfileLease:
    """Exclusive lease on a physical profile for a specific worker.

    Stale detection policy is decoupled and managed by the lease manager.
    """

    lease_id: LeaseId
    profile_name: str
    run_id: RunId
    worker_id: WorkerId
    pid: int | None = None
    acquired_at: str = ""
    heartbeat_at: str = ""

    def __post_init__(self) -> None:
        self.lease_id = LeaseId(self.lease_id)
        self.run_id = RunId(self.run_id)
        self.worker_id = WorkerId(self.worker_id)
        if self.pid is not None:
            self.pid = int(self.pid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": str(self.lease_id),
            "profile_name": self.profile_name,
            "run_id": str(self.run_id),
            "worker_id": str(self.worker_id),
            "pid": self.pid,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileLease:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for ProfileLease, got {type(data).__name__}")
        return cls(
            lease_id=LeaseId(data["lease_id"]),
            profile_name=str(data["profile_name"]),
            run_id=RunId(data["run_id"]),
            worker_id=WorkerId(data["worker_id"]),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            acquired_at=str(data.get("acquired_at", "")),
            heartbeat_at=str(data.get("heartbeat_at", "")),
        )

    @classmethod
    def from_json(cls, json_str: str) -> ProfileLease:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 8. Budget Contracts
# ============================================================================


@dataclass
class OrchestrationBudget:
    """Hard orchestration limits enforced by the engine."""

    max_parallel: int = 4
    max_invocations: int = 20
    max_rounds: int = 10
    max_boost_invocations: int = 2
    max_retries: int = 3
    max_runtime_seconds: float = 1800.0
    min_quota_remaining: float = 10.0
    max_consecutive_rejections: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_parallel": self.max_parallel,
            "max_invocations": self.max_invocations,
            "max_rounds": self.max_rounds,
            "max_boost_invocations": self.max_boost_invocations,
            "max_retries": self.max_retries,
            "max_runtime_seconds": self.max_runtime_seconds,
            "min_quota_remaining": self.min_quota_remaining,
            "max_consecutive_rejections": self.max_consecutive_rejections,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrchestrationBudget:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for OrchestrationBudget, got {type(data).__name__}")
        return cls(
            max_parallel=int(data.get("max_parallel", 4)),
            max_invocations=int(data.get("max_invocations", 20)),
            max_rounds=int(data.get("max_rounds", 10)),
            max_boost_invocations=int(data.get("max_boost_invocations", 2)),
            max_retries=int(data.get("max_retries", 3)),
            max_runtime_seconds=float(data.get("max_runtime_seconds", 1800.0)),
            min_quota_remaining=float(data.get("min_quota_remaining", 10.0)),
            max_consecutive_rejections=int(data.get("max_consecutive_rejections", 3)),
        )

    @classmethod
    def from_json(cls, json_str: str) -> OrchestrationBudget:
        return cls.from_dict(json.loads(json_str))


@dataclass
class BudgetUsage:
    """Accumulated usage counters tracked during a run."""

    invocations: int = 0
    rounds: int = 0
    boost_invocations: int = 0
    retries: int = 0
    runtime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocations": self.invocations,
            "rounds": self.rounds,
            "boost_invocations": self.boost_invocations,
            "retries": self.retries,
            "runtime_seconds": self.runtime_seconds,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetUsage:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for BudgetUsage, got {type(data).__name__}")
        return cls(
            invocations=int(data.get("invocations", 0)),
            rounds=int(data.get("rounds", 0)),
            boost_invocations=int(data.get("boost_invocations", 0)),
            retries=int(data.get("retries", 0)),
            runtime_seconds=float(data.get("runtime_seconds", 0.0)),
        )

    @classmethod
    def from_json(cls, json_str: str) -> BudgetUsage:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 9. Coordinator Observation Contract
# ============================================================================


@dataclass
class CoordinatorObservation:
    """Observation returned to the coordinator after an action executes."""

    completed_results: list[WorkerResult | AuditResult] = field(default_factory=list)
    failed_results: list[WorkerResult | AuditResult] = field(default_factory=list)
    rejected_requests: list[WorkerRequest | AuditRequest] = field(default_factory=list)
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)
    fleet_view: FleetView = field(default_factory=FleetView)
    round_number: int = 0

    def __post_init__(self) -> None:
        self.completed_results = [
            self._deserialize_result(r) if isinstance(r, dict) else r
            for r in self.completed_results
        ]
        self.failed_results = [
            self._deserialize_result(r) if isinstance(r, dict) else r
            for r in self.failed_results
        ]
        self.rejected_requests = [
            self._deserialize_request(r) if isinstance(r, dict) else r
            for r in self.rejected_requests
        ]
        if isinstance(self.budget_usage, dict):
            self.budget_usage = BudgetUsage.from_dict(self.budget_usage)
        if isinstance(self.fleet_view, dict):
            self.fleet_view = FleetView.from_dict(self.fleet_view)
        self.round_number = int(self.round_number)

    @staticmethod
    def _deserialize_result(data: dict[str, Any]) -> WorkerResult | AuditResult:
        if "findings" in data:
            return AuditResult.from_dict(data)
        return WorkerResult.from_dict(data)

    @staticmethod
    def _deserialize_request(data: dict[str, Any]) -> WorkerRequest | AuditRequest:
        if "target_worker_ids" in data or "focus" in data:
            return AuditRequest.from_dict(data)
        return WorkerRequest.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_results": [r.to_dict() for r in self.completed_results],
            "failed_results": [r.to_dict() for r in self.failed_results],
            "rejected_requests": [r.to_dict() for r in self.rejected_requests],
            "budget_usage": self.budget_usage.to_dict(),
            "fleet_view": self.fleet_view.to_dict(),
            "round_number": self.round_number,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoordinatorObservation:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for CoordinatorObservation, got {type(data).__name__}")
        return cls(
            completed_results=[
                cls._deserialize_result(r) if isinstance(r, dict) else r
                for r in data.get("completed_results", [])
            ],
            failed_results=[
                cls._deserialize_result(r) if isinstance(r, dict) else r
                for r in data.get("failed_results", [])
            ],
            rejected_requests=[
                cls._deserialize_request(r) if isinstance(r, dict) else r
                for r in data.get("rejected_requests", [])
            ],
            budget_usage=(
                BudgetUsage.from_dict(data["budget_usage"])
                if isinstance(data.get("budget_usage"), dict)
                else BudgetUsage()
            ),
            fleet_view=(
                FleetView.from_dict(data["fleet_view"])
                if isinstance(data.get("fleet_view"), dict)
                else FleetView()
            ),
            round_number=int(data.get("round_number", 0)),
        )

    @classmethod
    def from_json(cls, json_str: str) -> CoordinatorObservation:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 10. Model Invocation Contracts
# ============================================================================


@dataclass
class ModelInvocation:
    """Low-level execution payload dispatched to a model runner."""

    invocation_id: InvocationId
    run_id: RunId
    worker_id: WorkerId
    role: WorkerRole
    strategy: ExecutionStrategy = ExecutionStrategy.STANDARD
    workspace_mode: WorkspaceMode = WorkspaceMode.READ_ONLY
    prompt: str = ""
    output_schema: dict[str, Any] | None = None
    timeout_seconds: float = 300.0
    conversation_id: ConversationId | None = None

    def __post_init__(self) -> None:
        self.invocation_id = InvocationId(self.invocation_id)
        self.run_id = RunId(self.run_id)
        self.worker_id = WorkerId(self.worker_id)
        if isinstance(self.role, str) and not isinstance(self.role, WorkerRole):
            self.role = WorkerRole(self.role)
        if isinstance(self.strategy, str) and not isinstance(self.strategy, ExecutionStrategy):
            self.strategy = ExecutionStrategy(self.strategy)
        if isinstance(self.workspace_mode, str) and not isinstance(self.workspace_mode, WorkspaceMode):
            self.workspace_mode = WorkspaceMode(self.workspace_mode)
        self.timeout_seconds = float(self.timeout_seconds)
        if self.conversation_id is not None:
            self.conversation_id = ConversationId(self.conversation_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": str(self.invocation_id),
            "run_id": str(self.run_id),
            "worker_id": str(self.worker_id),
            "role": self.role.value,
            "strategy": self.strategy.value,
            "workspace_mode": self.workspace_mode.value,
            "prompt": self.prompt,
            "output_schema": self.output_schema,
            "timeout_seconds": self.timeout_seconds,
            "conversation_id": str(self.conversation_id) if self.conversation_id is not None else None,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelInvocation:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for ModelInvocation, got {type(data).__name__}")
        return cls(
            invocation_id=InvocationId(data["invocation_id"]),
            run_id=RunId(data["run_id"]),
            worker_id=WorkerId(data["worker_id"]),
            role=WorkerRole(data["role"]),
            strategy=ExecutionStrategy(data.get("strategy", ExecutionStrategy.STANDARD)),
            workspace_mode=WorkspaceMode(data.get("workspace_mode", WorkspaceMode.READ_ONLY)),
            prompt=str(data.get("prompt", "")),
            output_schema=data.get("output_schema"),
            timeout_seconds=float(data.get("timeout_seconds", 300.0)),
            conversation_id=(
                ConversationId(data["conversation_id"])
                if data.get("conversation_id") is not None
                else None
            ),
        )

    @classmethod
    def from_json(cls, json_str: str) -> ModelInvocation:
        return cls.from_dict(json.loads(json_str))


@dataclass
class ModelResult:
    """Low-level execution result returned from a model runner."""

    invocation_id: InvocationId
    status: InvocationStatus = InvocationStatus.SUCCEEDED
    response: str | None = None
    structured_data: dict[str, Any] | None = None
    conversation_id: ConversationId | None = None
    usage: dict[str, Any] | None = None
    exit_code: int | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.invocation_id = InvocationId(self.invocation_id)
        if isinstance(self.status, str) and not isinstance(self.status, InvocationStatus):
            self.status = InvocationStatus(self.status)
        if self.conversation_id is not None:
            self.conversation_id = ConversationId(self.conversation_id)
        if self.exit_code is not None:
            self.exit_code = int(self.exit_code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": str(self.invocation_id),
            "status": self.status.value,
            "response": self.response,
            "structured_data": self.structured_data,
            "conversation_id": str(self.conversation_id) if self.conversation_id is not None else None,
            "usage": self.usage,
            "exit_code": self.exit_code,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelResult:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for ModelResult, got {type(data).__name__}")
        return cls(
            invocation_id=InvocationId(data["invocation_id"]),
            status=InvocationStatus(data.get("status", InvocationStatus.SUCCEEDED)),
            response=data.get("response"),
            structured_data=data.get("structured_data"),
            conversation_id=(
                ConversationId(data["conversation_id"])
                if data.get("conversation_id") is not None
                else None
            ),
            usage=data.get("usage"),
            exit_code=int(data["exit_code"]) if data.get("exit_code") is not None else None,
            error=data.get("error"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> ModelResult:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 11. Run State Contract
# ============================================================================


@dataclass
class RunState:
    """Serializable persistence state of an orchestration run.

    Contains only serializable data types; no process handles, sockets,
    or synchronization locks.
    """

    run_id: RunId
    task: str
    mode: RunMode = RunMode.PLAN
    status: RunStatus = RunStatus.CREATED
    assessment: TaskAssessment | None = None
    coordinator_conversation_id: ConversationId | None = None
    round_number: int = 0
    budget: OrchestrationBudget = field(default_factory=OrchestrationBudget)
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)
    invocation_ids: list[InvocationId] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    final_result: str | None = None

    def __post_init__(self) -> None:
        self.run_id = RunId(self.run_id)
        if isinstance(self.mode, str) and not isinstance(self.mode, RunMode):
            self.mode = RunMode(self.mode)
        if isinstance(self.status, str) and not isinstance(self.status, RunStatus):
            self.status = RunStatus(self.status)
        if isinstance(self.assessment, dict):
            self.assessment = TaskAssessment.from_dict(self.assessment)
        if self.coordinator_conversation_id is not None:
            self.coordinator_conversation_id = ConversationId(self.coordinator_conversation_id)
        self.round_number = int(self.round_number)
        if isinstance(self.budget, dict):
            self.budget = OrchestrationBudget.from_dict(self.budget)
        if isinstance(self.budget_usage, dict):
            self.budget_usage = BudgetUsage.from_dict(self.budget_usage)
        self.invocation_ids = [InvocationId(i) for i in self.invocation_ids]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "task": self.task,
            "mode": self.mode.value,
            "status": self.status.value,
            "assessment": self.assessment.to_dict() if self.assessment is not None else None,
            "coordinator_conversation_id": (
                str(self.coordinator_conversation_id)
                if self.coordinator_conversation_id is not None
                else None
            ),
            "round_number": self.round_number,
            "budget": self.budget.to_dict(),
            "budget_usage": self.budget_usage.to_dict(),
            "invocation_ids": [str(i) for i in self.invocation_ids],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "final_result": self.final_result,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for RunState, got {type(data).__name__}")
        return cls(
            run_id=RunId(data["run_id"]),
            task=str(data["task"]),
            mode=RunMode(data.get("mode", RunMode.PLAN)),
            status=RunStatus(data.get("status", RunStatus.CREATED)),
            assessment=(
                TaskAssessment.from_dict(data["assessment"])
                if isinstance(data.get("assessment"), dict)
                else data.get("assessment")
            ),
            coordinator_conversation_id=(
                ConversationId(data["coordinator_conversation_id"])
                if data.get("coordinator_conversation_id") is not None
                else None
            ),
            round_number=int(data.get("round_number", 0)),
            budget=(
                OrchestrationBudget.from_dict(data["budget"])
                if isinstance(data.get("budget"), dict)
                else OrchestrationBudget()
            ),
            budget_usage=(
                BudgetUsage.from_dict(data["budget_usage"])
                if isinstance(data.get("budget_usage"), dict)
                else BudgetUsage()
            ),
            invocation_ids=[InvocationId(i) for i in data.get("invocation_ids", [])],
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            final_result=data.get("final_result"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> RunState:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 12. Orchestration Event Contract
# ============================================================================


@dataclass
class OrchestrationEvent:
    """Discrete event emitted along the orchestration lifecycle."""

    event_id: str
    run_id: RunId
    type: EventType
    timestamp: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.event_id = str(self.event_id)
        self.run_id = RunId(self.run_id)
        if isinstance(self.type, str) and not isinstance(self.type, EventType):
            self.type = EventType(self.type)
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": str(self.run_id),
            "type": self.type.value,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrchestrationEvent:
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for OrchestrationEvent, got {type(data).__name__}")
        return cls(
            event_id=str(data["event_id"]),
            run_id=RunId(data["run_id"]),
            type=EventType(data["type"]),
            timestamp=str(data.get("timestamp", "")),
            payload=dict(data.get("payload", {})),
        )

    @classmethod
    def from_json(cls, json_str: str) -> OrchestrationEvent:
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# 13. Subsystem Protocol Interfaces
# ============================================================================


@runtime_checkable
class ModelSession(Protocol):
    """Protocol for a multi-turn conversational session with a model."""

    @property
    def conversation_id(self) -> ConversationId:
        """The identifier of this ongoing conversation."""
        ...

    def send(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        """Send a prompt and receive the model's result."""
        ...

    def close(self) -> None:
        """Terminate the session and release resources."""
        ...


@runtime_checkable
class ModelRunner(Protocol):
    """Protocol for executing model invocations."""

    def run(
        self,
        invocation: ModelInvocation,
        profile_name: str | None = None,
    ) -> ModelResult:
        """Execute a single model invocation."""
        ...


@runtime_checkable
class CoordinatorClient(Protocol):
    """Protocol for coordinator task assessment and action decisions."""

    def assess_task(self, task: str, fleet_view: FleetView) -> TaskAssessment:
        """Assess the nature, scope, and strategy for a new task."""
        ...

    def decide_action(
        self,
        observation: CoordinatorObservation,
        conversation_id: ConversationId | None = None,
    ) -> CoordinatorAction:
        """Propose the next orchestration action based on the observation."""
        ...


@runtime_checkable
class ProfileScheduler(Protocol):
    """Protocol for querying fleet capacity and selecting profiles for requests."""

    def get_fleet_view(self) -> FleetView:
        """Get coordinator-safe view of current fleet capacity."""
        ...

    def get_fleet_snapshot(self) -> FleetSnapshot:
        """Get internal snapshot of all profile capacities."""
        ...

    def select_profile(
        self,
        request: WorkerRequest | AuditRequest,
    ) -> str | None:
        """Select an optimal profile name for the given request, or None if unavailable."""
        ...


@runtime_checkable
class ProfileLeaseManager(Protocol):
    """Protocol for managing exclusive leases on profiles."""

    def acquire(
        self,
        profile_name: str,
        run_id: RunId,
        worker_id: WorkerId,
        pid: int | None = None,
    ) -> ProfileLease:
        """Acquire an exclusive lease for a profile."""
        ...

    def heartbeat(self, lease_id: LeaseId) -> bool:
        """Renew heartbeat for an active lease."""
        ...

    def release(self, lease_id: LeaseId) -> bool:
        """Release a lease when work finishes."""
        ...

    def list_leases(self) -> list[ProfileLease]:
        """List all active leases."""
        ...

    def revoke_stale(self, timeout_seconds: float) -> list[ProfileLease]:
        """Revoke leases with heartbeat older than timeout."""
        ...


@runtime_checkable
class RunStore(Protocol):
    """Protocol for persistent storage of run states and worker results."""

    def save_run(self, state: RunState) -> None:
        """Persist or update a run state."""
        ...

    def get_run(self, run_id: RunId) -> RunState | None:
        """Retrieve a run state by ID."""
        ...

    def list_runs(self, limit: int = 100) -> list[RunState]:
        """List recent run states."""
        ...

    def save_result(self, run_id: RunId, result: WorkerResult | AuditResult) -> None:
        """Persist an individual worker or audit result."""
        ...

    def get_results(self, run_id: RunId) -> list[WorkerResult | AuditResult]:
        """Retrieve all results for a run."""
        ...


@runtime_checkable
class EventSink(Protocol):
    """Protocol for logging or broadcasting orchestration events."""

    def emit(self, event: OrchestrationEvent) -> None:
        """Emit an orchestration lifecycle event."""
        ...

    def get_events(self, run_id: RunId) -> list[OrchestrationEvent]:
        """Retrieve stored events for a run."""
        ...
