"""Domain models and contracts for AGYM Council.

Defines Pydantic v2 domain entities, enumerations, workflow schemas,
turn contracts, and audit/event records.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class AccountAuthStatus(str, Enum):
    UNVERIFIED = "unverified"
    READY = "ready"
    NEEDS_LOGIN = "needs_login"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> AccountAuthStatus | None:
        if isinstance(value, str):
            norm = value.lower().replace("-", "_")
            for member in cls:
                if member.value == norm:
                    return member
        return None


class ProviderType(str, Enum):
    ANTIGRAVITY = "antigravity"
    FAKE = "fake"
    CUSTOM = "custom"

    @classmethod
    def _missing_(cls, value: object) -> ProviderType | None:
        if isinstance(value, str):
            norm = value.lower()
            for member in cls:
                if member.value == norm:
                    return member
        return None


class StageKind(str, Enum):
    INDEPENDENT = "independent"
    CRITIQUE = "critique"
    REVISE = "revise"
    SYNTHESIZE = "synthesize"
    AUDIT = "audit"

    @classmethod
    def _missing_(cls, value: object) -> StageKind | None:
        if isinstance(value, str):
            norm = value.lower()
            for member in cls:
                if member.value == norm:
                    return member
        return None


class StageContextPolicy(str, Enum):
    FRESH = "fresh"
    CONTINUE = "continue"

    @classmethod
    def _missing_(cls, value: object) -> StageContextPolicy | None:
        if isinstance(value, str):
            norm = value.lower()
            for member in cls:
                if member.value == norm:
                    return member
        return None


class StageReleasePolicy(str, Enum):
    AFTER_ALL_REQUIRED = "after_all_required"

    @classmethod
    def _missing_(cls, value: object) -> StageReleasePolicy | None:
        if isinstance(value, str):
            norm = value.lower().replace("-", "_")
            for member in cls:
                if member.value == norm:
                    return member
        return None


class StageFailurePolicy(str, Enum):
    NEEDS_ATTENTION = "needs_attention"

    @classmethod
    def _missing_(cls, value: object) -> StageFailurePolicy | None:
        if isinstance(value, str):
            norm = value.lower().replace("-", "_")
            for member in cls:
                if member.value == norm:
                    return member
        return None


class ExecutionMode(str, Enum):
    SUPPLIED_EVIDENCE = "supplied_evidence"
    WORKSPACE = "workspace"

    @classmethod
    def _missing_(cls, value: object) -> ExecutionMode | None:
        if isinstance(value, str):
            norm = value.lower().replace("-", "_")
            for member in cls:
                if member.value == norm:
                    return member
        return None


class RunStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    COMPLETED = "COMPLETED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @classmethod
    def _missing_(cls, value: object) -> RunStatus | None:
        if isinstance(value, str):
            norm = value.upper().replace("-", "_")
            for member in cls:
                if member.value == norm:
                    return member
        return None


class AttemptStatus(str, Enum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> AttemptStatus | None:
        if isinstance(value, str):
            norm = value.upper().replace("-", "_")
            for member in cls:
                if member.value == norm:
                    return member
        return None


class StageStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"

    @classmethod
    def _missing_(cls, value: object) -> StageStatus | None:
        if isinstance(value, str):
            norm = value.upper().replace("-", "_")
            for member in cls:
                if member.value == norm:
                    return member
        return None


class WorkerStatus(str, Enum):
    IDLE = "IDLE"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @classmethod
    def _missing_(cls, value: object) -> WorkerStatus | None:
        if isinstance(value, str):
            norm = value.upper().replace("-", "_")
            for member in cls:
                if member.value == norm:
                    return member
        return None


class TurnStatus(str, Enum):
    SUCCESS = "success"
    NEEDS_AUTH = "needs_auth"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    PERMISSION_BLOCKED = "permission_blocked"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MALFORMED_OUTPUT = "malformed_output"
    UNKNOWN_COMPLETION = "unknown_completion"

    @classmethod
    def _missing_(cls, value: object) -> TurnStatus | None:
        if isinstance(value, str):
            norm = value.lower().replace("-", "_")
            for member in cls:
                if member.value == norm or member.value.replace("_", "-") == value.lower():
                    return member
        return None


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"
    CRITICAL = "critical"

    @classmethod
    def _missing_(cls, value: object) -> IssueSeverity | None:
        if isinstance(value, str):
            norm = value.lower()
            for member in cls:
                if member.value == norm:
                    return member
        return None


# ---------------------------------------------------------------------------
# Workflow Configuration Models
# ---------------------------------------------------------------------------


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Unique input identifier")
    description: str = Field(..., min_length=1, description="Description of the input")
    required: bool = Field(default=True, description="Whether input is mandatory")
    value: str | None = Field(default=None, description="Bound input value or artifact reference")


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_concurrency: int = Field(default=4, gt=0, description="Max concurrent workers system-wide")
    per_account_concurrency: int = Field(default=1, gt=0, description="Max concurrent workers on single account")
    max_model_calls: int = Field(default=24, gt=0, description="Max total model calls budget")
    max_retries_per_task: int = Field(default=1, ge=0, description="Max retries per task")
    max_wall_seconds: int = Field(default=7200, gt=0, description="Max wall clock time in seconds")
    max_revisions: int = Field(default=2, ge=0, description="Max revision cycles permitted before terminating or proceeding")
    automatic_account_switching: bool = Field(default=False, description="Must remain False for isolation")

    @field_validator("automatic_account_switching")
    @classmethod
    def validate_no_switching(cls, v: bool) -> bool:
        if v is True:
            raise ValueError("automatic_account_switching must be False to preserve account isolation")
        return v


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Worker identifier")
    name: str = Field(..., min_length=1, description="Human friendly worker name")
    account_ref: str = Field(..., min_length=1, description="Account or profile reference")
    model: str = Field(..., min_length=1, description="Model identifier or draft placeholder")
    role: str = Field(..., min_length=1, description="Role label")
    instructions: str = Field(..., min_length=1, description="System instructions")
    task: str = Field(..., min_length=1, description="Specific assigned duty")


class StageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Stage identifier")
    kind: StageKind = Field(..., description="Intent of the stage")
    workers: list[str] = Field(..., min_length=1, description="List of participant worker IDs")
    input_stages: list[str] = Field(default_factory=list, description="Prior stages whose outputs are released")
    instruction: str = Field(..., min_length=1, description="Prompt guidance for this stage")
    context: StageContextPolicy = Field(default=StageContextPolicy.FRESH, description="fresh or continue")
    release: StageReleasePolicy = Field(default=StageReleasePolicy.AFTER_ALL_REQUIRED)
    failure_policy: StageFailurePolicy = Field(default=StageFailurePolicy.NEEDS_ATTENTION)
    required_sections: list[str] = Field(
        default_factory=lambda: ["findings", "evidence_or_assumptions", "uncertainties", "next_action"],
        min_length=1,
        description="Required JSON output sections",
    )

    @field_validator("workers")
    @classmethod
    def validate_unique_stage_workers(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError(f"Duplicate workers detected in stage: {v}")
        return v


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, description="Schema version, must be 1")
    draft: bool = Field(default=True, description="True if draft preset, False if resolved for execution")
    name: str = Field(..., min_length=1, description="Workflow name")
    goal: str = Field(..., min_length=1, description="Goal of the workflow")
    inputs: list[WorkflowInput] = Field(..., min_length=1, description="Declared workflow inputs")
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    execution_mode: ExecutionMode = Field(default=ExecutionMode.SUPPLIED_EVIDENCE)
    workers: list[WorkerConfig] = Field(..., min_length=1, description="Declared workers")
    stages: list[StageConfig] = Field(..., min_length=1, description="Ordered workflow stages")
    final_stage: str = Field(..., min_length=1, description="Final stage ID matching stages[-1].id")

    @model_validator(mode="after")
    def validate_workflow_graph(self) -> WorkflowConfig:
        if self.schema_version != 1:
            raise ValueError("Unsupported schema version; must be 1")

        # 1. Unique Input IDs
        input_ids = [inp.id for inp in self.inputs]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("Duplicate input IDs found in inputs")

        # 2. Unique Worker IDs
        worker_ids = [w.id for w in self.workers]
        if len(worker_ids) != len(set(worker_ids)):
            raise ValueError("Duplicate worker IDs found in workers")
        worker_set = set(worker_ids)

        # 3. Stage Validations & Acyclic DAG
        seen_stages: set[str] = set()
        total_calls = 0

        for stage in self.stages:
            if stage.id in seen_stages:
                raise ValueError(f"Duplicate stage ID '{stage.id}'")

            # Check workers exist in workflow.workers
            unknown_workers = set(stage.workers) - worker_set
            if unknown_workers:
                raise ValueError(f"Stage '{stage.id}' references unknown worker(s): {sorted(unknown_workers)}")

            # Check input_stages: must strictly reference previously completed stages
            invalid_inputs = set(stage.input_stages) - seen_stages
            if invalid_inputs:
                raise ValueError(
                    f"Stage '{stage.id}' input_stages refers to future or missing stage(s): {sorted(invalid_inputs)}"
                )

            seen_stages.add(stage.id)
            total_calls += len(stage.workers)

        # 4. Final Stage Check
        if not self.stages or self.final_stage != self.stages[-1].id:
            raise ValueError(
                f"Invalid final stage '{self.final_stage}'; must match the last stage '{self.stages[-1].id}'"
            )

        # 5. Budget Check
        if total_calls > self.limits.max_model_calls:
            raise ValueError(
                f"Required calls ({total_calls}) exceed budget before retries ({self.limits.max_model_calls})"
            )

        # 6. Live Execution Readiness Check (when draft == False)
        if not self.draft:
            for w in self.workers:
                if "<" in w.model or ">" in w.model or not w.model.strip():
                    raise ValueError(
                        f"Worker '{w.id}' model '{w.model}' is a placeholder; must be bound before execution"
                    )
            for inp in self.inputs:
                if inp.required and (inp.value is None or not str(inp.value).strip()):
                    raise ValueError(f"Required input '{inp.id}' has no value bound for execution")

        return self


# ---------------------------------------------------------------------------
# Output Section & Structured Content Models
# ---------------------------------------------------------------------------


class CouncilOutputSections(BaseModel):
    model_config = ConfigDict(extra="allow")

    findings: str = Field(default="", description="Findings, observations, and answers")
    evidence_or_assumptions: str = Field(
        default="", description="Supporting citations and explicit assumptions"
    )
    uncertainties: str = Field(default="", description="Risks, unknowns, and dissenting views")
    next_action: str = Field(default="", description="Recommended action or next step")

    def validate_required(self, required_sections: list[str]) -> list[str]:
        """Validate that all requested sections are present and non-empty.

        Returns:
            List of section names that are missing or empty.
        """
        missing: list[str] = []
        for req in required_sections:
            val = getattr(self, req, None)
            if val is None:
                extra = getattr(self, "__pydantic_extra__", {}) or {}
                val = extra.get(req)
            if val is None or not str(val).strip():
                missing.append(req)
        return missing

    def to_markdown(self) -> str:
        """Render sections as Markdown text."""
        parts = [
            f"## Findings\n{self.findings}\n",
            f"## Evidence or Assumptions\n{self.evidence_or_assumptions}\n",
            f"## Uncertainties\n{self.uncertainties}\n",
            f"## Next Action\n{self.next_action}\n",
        ]
        extra = getattr(self, "__pydantic_extra__", {}) or {}
        for k, v in extra.items():
            title = k.replace("_", " ").title()
            parts.append(f"## {title}\n{v}\n")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Turn Contracts (Provider Adapter Interface)
# ---------------------------------------------------------------------------


class PermissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_tools: list[str] = Field(default_factory=list)
    allow_network: bool = Field(default=False)
    allow_workspace_write: bool = Field(default=False)
    allow_command_execution: bool = Field(default=False)


class TurnAllowance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=600, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    call_budget: int = Field(default=1, gt=0)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    stage_id: str = Field(..., min_length=1)
    worker_id: str = Field(..., min_length=1)
    account_ref: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    prompt: str = Field(default="")
    role: str = Field(default="")
    conversation_handle: str | None = Field(default=None)
    conversation_id: str | None = Field(default=None)
    system_prompt: str | None = Field(default=None)
    input_artifacts: list[Any] = Field(default_factory=list)
    working_directory: str | None = Field(default=None)
    permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)
    allowance: TurnAllowance = Field(default_factory=TurnAllowance)
    required_sections: list[str] = Field(
        default_factory=lambda: ["findings", "evidence_or_assumptions", "uncertainties", "next_action"]
    )
    execution_mode: str = Field(default="supplied_evidence")
    limits: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def sync_conversation(self) -> TurnRequest:
        if self.conversation_id and not self.conversation_handle:
            self.conversation_handle = self.conversation_id
        elif self.conversation_handle and not self.conversation_id:
            self.conversation_id = self.conversation_handle
        return self


class UsageMetrics(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)


class TurnEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(..., min_length=1)
    sequence: int = Field(..., ge=1)
    event_type: str = Field(..., min_length=1)  # started, delta, heartbeat, completed, error
    delta: str = Field(default="")
    timestamp: str = Field(default_factory=_utc_now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


class TurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(..., min_length=1)
    status: TurnStatus = Field(...)
    conversation_handle: str | None = Field(default=None)
    conversation_id: str | None = Field(default=None)
    output_text: str | None = Field(default=None)
    parsed_sections: CouncilOutputSections | None = Field(default=None)
    structured_output: dict[str, Any] | None = Field(default=None)
    artifact_refs: list[str] = Field(default_factory=list)
    stdout_artifact_id: str | None = Field(default=None)
    stderr_artifact_id: str | None = Field(default=None)
    raw_stdout_artifact_id: str | None = Field(default=None)
    raw_stderr_artifact_id: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    error_details: str | None = Field(default=None)
    error_code: str | None = Field(default=None)
    exit_code: int | None = Field(default=None)
    observed_model: str | None = Field(default=None)
    usage: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = Field(default=None)
    finished_at: str | None = Field(default=None)

    @model_validator(mode="after")
    def sync_result_fields(self) -> TurnResult:
        if self.conversation_id and not self.conversation_handle:
            self.conversation_handle = self.conversation_id
        elif self.conversation_handle and not self.conversation_id:
            self.conversation_id = self.conversation_handle

        if self.stdout_artifact_id and not self.raw_stdout_artifact_id:
            self.raw_stdout_artifact_id = self.stdout_artifact_id
        elif self.raw_stdout_artifact_id and not self.stdout_artifact_id:
            self.stdout_artifact_id = self.raw_stdout_artifact_id

        if self.stderr_artifact_id and not self.raw_stderr_artifact_id:
            self.raw_stderr_artifact_id = self.stderr_artifact_id
        elif self.raw_stderr_artifact_id and not self.stderr_artifact_id:
            self.stderr_artifact_id = self.raw_stderr_artifact_id

        if self.error_message and not self.error_details:
            self.error_details = self.error_message
        elif self.error_details and not self.error_message:
            self.error_message = self.error_details

        return self


# ---------------------------------------------------------------------------
# Provider Support & Reconciliation Models
# ---------------------------------------------------------------------------


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider_name: str = Field(default="fake")
    structured_output: bool = Field(default=True)
    resume_conversation: bool = Field(default=True)
    cancellation: bool = Field(default=True)
    tool_execution: bool = Field(default=False)
    token_usage: bool = Field(default=True)
    model_discovery: bool = Field(default=True)
    access_restrictions: bool = Field(default=False)

    supports_streaming: bool = Field(default=True)
    supports_structured_output: bool = Field(default=True)
    supports_resume: bool = Field(default=True)
    supports_cancellation: bool = Field(default=True)
    supports_tools: bool = Field(default=False)
    supports_usage_metrics: bool = Field(default=True)
    supports_model_discovery: bool = Field(default=True)
    enforces_workspace_isolation: bool = Field(default=False)

    @model_validator(mode="after")
    def sync_capabilities(self) -> ProviderCapabilities:
        if "supports_structured_output" in self.__dict__:
            self.structured_output = self.supports_structured_output
        if "supports_resume" in self.__dict__:
            self.resume_conversation = self.supports_resume
        if "supports_cancellation" in self.__dict__:
            self.cancellation = self.supports_cancellation
        if "supports_tools" in self.__dict__:
            self.tool_execution = self.supports_tools
        if "supports_usage_metrics" in self.__dict__:
            self.token_usage = self.supports_usage_metrics
        if "supports_model_discovery" in self.__dict__:
            self.model_discovery = self.supports_model_discovery
        return self


class AccountStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ref: str = Field(default="")
    status: AccountAuthStatus | str = Field(...)
    details: str | None = Field(default=None)
    message: str = Field(default="")
    cli_version: str | None = Field(default=None)
    advisory_usage: dict[str, Any] = Field(default_factory=dict)
    checked_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def sync_message(self) -> AccountStatus:
        if self.details and not self.message:
            self.message = self.details
        elif self.message and not self.details:
            self.details = self.message
        return self


class ModelDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    provider: str = Field(default="fake")
    context_window: int = Field(default=128000, gt=0)
    capabilities: list[str] = Field(default_factory=list)


class AttemptSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    stage_id: str = Field(..., min_length=1)
    worker_id: str = Field(..., min_length=1)
    account_ref: str = Field(..., min_length=1)
    pid: int | None = Field(default=None)
    process_start_time: float | None = Field(default=None)
    status: AttemptStatus | str = Field(...)
    started_at: str | None = Field(default=None)


class AttemptReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(..., min_length=1)
    reconciled_status: AttemptStatus | str = Field(...)
    details: str = Field(default="")
    reason: str = Field(default="")
    recovered_artifacts: list[str] = Field(default_factory=list)
    result: TurnResult | None = Field(default=None)

    @model_validator(mode="after")
    def sync_details_reason(self) -> AttemptReconciliation:
        if self.details and not self.reason:
            self.reason = self.details
        elif self.reason and not self.details:
            self.details = self.reason
        return self


# ---------------------------------------------------------------------------
# Domain Persistence & Runtime State Models
# ---------------------------------------------------------------------------


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider: ProviderType = Field(default=ProviderType.ANTIGRAVITY)
    profile_ref: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    enabled: bool = Field(default=True)
    auth_status: AccountAuthStatus = Field(default=AccountAuthStatus.UNVERIFIED)
    last_auth_check: str | None = Field(default=None)
    cli_version: str | None = Field(default=None)
    usage_advisory: dict[str, Any] | None = Field(default=None)
    usage_collected_at: str | None = Field(default=None)
    concurrency_limit: int = Field(default=1, gt=0)
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


class AgentTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version: int = Field(default=1, ge=1)
    name: str = Field(..., min_length=1)
    purpose: str = Field(..., min_length=1)
    instructions: str = Field(..., min_length=1)
    working_style: str | None = Field(default=None)
    output_expectations: list[str] = Field(
        default_factory=lambda: ["findings", "evidence_or_assumptions", "uncertainties", "next_action"]
    )
    preferred_model: str | None = Field(default=None)
    suggested_capabilities: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="SHA-256 hash or artifact UUID")
    run_id: str = Field(..., min_length=1)
    stage_id: str | None = Field(default=None)
    worker_id: str | None = Field(default=None)
    attempt_id: str | None = Field(default=None)
    name: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    size_bytes: int = Field(..., ge=0)
    sha256: str = Field(..., min_length=64, max_length=64)
    released: bool = Field(default=False)
    mime_type: str = Field(default="text/plain")
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="before")
    @classmethod
    def map_db_columns(cls, data: Any) -> Any:
        if isinstance(data, dict):
            mapped = dict(data)
            if "artifact_id" in mapped and "id" not in mapped:
                mapped["id"] = mapped.pop("artifact_id")
            elif "artifact_id" in mapped:
                mapped.pop("artifact_id")

            if "content_hash" in mapped and "sha256" not in mapped:
                mapped["sha256"] = mapped.pop("content_hash")
            elif "content_hash" in mapped:
                mapped.pop("content_hash")

            if "storage_path" in mapped and "path" not in mapped:
                mapped["path"] = mapped.pop("storage_path")
            elif "storage_path" in mapped:
                mapped.pop("storage_path")

            if "byte_size" in mapped and "size_bytes" not in mapped:
                mapped["size_bytes"] = mapped.pop("byte_size")
            elif "byte_size" in mapped:
                mapped.pop("byte_size")

            if "media_type" in mapped and "mime_type" not in mapped:
                mapped["mime_type"] = mapped.pop("media_type")
            elif "media_type" in mapped:
                mapped.pop("media_type")

            if "released" in mapped:
                mapped["released"] = bool(mapped["released"])

            for extra_col in ("metadata_json", "released_at"):
                mapped.pop(extra_col, None)

            return mapped
        return data

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, v: str) -> str:
        if len(v) != 64 or not all(c in "0123456789abcdefABCDEF" for c in v):
            raise ValueError(f"Invalid SHA-256 hex digest: {v!r}")
        return v.lower()



class Attempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = Field(..., min_length=1)
    stage_id: str = Field(..., min_length=1)
    worker_id: str = Field(..., min_length=1)
    attempt_number: int = Field(default=1, ge=1)
    account_ref: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    status: AttemptStatus = Field(default=AttemptStatus.QUEUED)
    pid: int | None = Field(default=None)
    process_start_time: float | None = Field(default=None)
    working_directory: str | None = Field(default=None)
    prompt_artifact_id: str | None = Field(default=None)
    result_artifact_id: str | None = Field(default=None)
    stdout_artifact_id: str | None = Field(default=None)
    stderr_artifact_id: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    exit_code: int | None = Field(default=None)
    started_at: str | None = Field(default=None)
    finished_at: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def map_db_columns(cls, data: Any) -> Any:
        if isinstance(data, dict):
            mapped = dict(data)
            if "attempt_id" in mapped and "id" not in mapped:
                mapped["id"] = mapped.pop("attempt_id")
            elif "attempt_id" in mapped:
                mapped.pop("attempt_id")

            if "model_used" in mapped and "model" not in mapped:
                mapped["model"] = mapped.pop("model_used") or "unknown"
            elif "model_used" in mapped:
                mapped.pop("model_used")

            if "workspace_dir" in mapped and "working_directory" not in mapped:
                mapped["working_directory"] = mapped.pop("workspace_dir")
            elif "workspace_dir" in mapped:
                mapped.pop("workspace_dir")

            if "error_details" in mapped and "error_message" not in mapped:
                mapped["error_message"] = mapped.pop("error_details")
            elif "error_details" in mapped:
                mapped.pop("error_details")

            for extra_col in ("input_digest", "rendered_prompt", "usage_json", "created_at", "updated_at"):
                mapped.pop(extra_col, None)

            return mapped
        return data



class StageInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    stage_order: int = Field(..., ge=0)
    kind: StageKind = Field(...)
    status: StageStatus = Field(default=StageStatus.PENDING)
    released_at: str | None = Field(default=None)
    error_message: str | None = Field(default=None)


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    config: WorkflowConfig = Field(...)
    status: RunStatus = Field(default=RunStatus.READY)
    current_stage_id: str | None = Field(default=None)
    model_calls_made: int = Field(default=0, ge=0)
    started_at: str | None = Field(default=None)
    finished_at: str | None = Field(default=None)
    dissent_recorded: bool = Field(default=False)
    final_deliverable_artifact_id: str | None = Field(default=None)
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


# ---------------------------------------------------------------------------
# Audit, Event & Idempotency Models
# ---------------------------------------------------------------------------


class CouncilEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1)
    sequence: int = Field(..., ge=1, description="Monotonically increasing sequence number")
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = Field(..., min_length=1)
    stage_id: str | None = Field(default=None)
    worker_id: str | None = Field(default=None)
    attempt_id: str | None = Field(default=None)
    timestamp: str = Field(default_factory=_utc_now_iso)
    type: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class IssueRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = Field(..., min_length=1)
    stage_id: str | None = Field(default=None)
    worker_id: str | None = Field(default=None)
    attempt_id: str | None = Field(default=None)
    severity: IssueSeverity = Field(default=IssueSeverity.WARNING)
    category: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_utc_now_iso)


class IdempotencyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1)
    action: str = Field(default="")
    method: str = Field(default="POST", min_length=1)
    path: str = Field(default="")
    request_hash: str = Field(..., min_length=1)
    response_status: int = Field(default=200, ge=100, le=599)
    response_body: str = Field(...)
    created_at: str = Field(default_factory=_utc_now_iso)


# ---------------------------------------------------------------------------
# Serialization and Redaction Helpers
# ---------------------------------------------------------------------------


def scrub_sensitive_strings(text: str) -> str:
    """Scrub absolute home paths, auth tokens, and credential patterns from text content."""
    # Mask common credential tokens / bearer tokens
    text = re.sub(r"(bearer\s+)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    text = re.sub(r"(token=)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    text = re.sub(r"(access_token=)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    text = re.sub(r"(refresh_token=)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    text = re.sub(r"(api_key=)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    text = re.sub(r"(apikey=)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    text = re.sub(r"(auth_code=)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_AUTH_CODE]", text, flags=re.IGNORECASE)
    text = re.sub(r"(code=)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_AUTH_CODE]", text, flags=re.IGNORECASE)
    text = re.sub(r"(ya29\.)[A-Za-z0-9_\-\./%+]{15,}", r"\1[REDACTED_TOKEN]", text)
    # Mask home directory paths (/home/username or C:\Users\username)
    text = re.sub(r"/home/[a-zA-Z0-9_\-]+", "/home/[USER]", text)
    text = re.sub(r"[A-Za-z]:\\Users\\[a-zA-Z0-9_\-]+", lambda m: "C:\\Users\\[USER]", text)
    return text


def redact_secrets(obj: Any) -> Any:
    """Recursively scrub sensitive keys and token patterns from dict/list/string structures.

    Masks passwords, auth codes, secret tokens, and bearer credentials with [REDACTED].
    """
    secret_keys = {
        "token",
        "auth_code",
        "password",
        "secret",
        "cookie",
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
    }
    if isinstance(obj, str):
        return scrub_sensitive_strings(obj)
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                cleaned[k] = redact_secrets(v)
            elif any(s in k.lower() for s in secret_keys):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = redact_secrets(v)
        return cleaned
    if isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    return obj


def load_workflow_from_file(path: Path | str) -> WorkflowConfig:
    """Load and validate a WorkflowConfig from a JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return WorkflowConfig.model_validate(data)


def export_workflow_to_json(workflow: WorkflowConfig, indent: int = 2) -> str:
    """Export a WorkflowConfig to a formatted JSON string."""
    data = workflow.model_dump(mode="json")
    return json.dumps(data, indent=indent)
