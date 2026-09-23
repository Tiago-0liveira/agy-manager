"""Orchestration communication protocol, schemas, parser, and validation.

This module defines how the coordinator communicates with AGYM.
It provides:
- Protocol error exceptions for malformed responses, forbidden fields, schema errors, and validation errors.
- Strict detection and rejection of forbidden fields (profile, profile_name, command, argv, shell, environment).
- JSON Schemas for coordinator actions, task assessments, worker requests, and initial responses.
- Robust parsers for coordinator actions and initial responses from raw model outputs.
- Semantic and structural validation of coordinator actions.
- Standardized infrastructure failure definitions and constructors.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    AuditRequest,
    BudgetUsage,
    ComplexityLevel,
    CoordinatorAction,
    ExecutionStrategy,
    FailureClass,
    InvocationStatus,
    TaskAssessment,
    TaskType,
    WorkerId,
    WorkerRequest,
    WorkerResult,
    WorkerRole,
    WorkspaceMode,
)

__all__ = [
    # Exceptions
    "ProtocolError",
    "MalformedResponseError",
    "ForbiddenFieldError",
    "ProtocolSchemaError",
    "ActionValidationError",
    # Constants & Schemas
    "FORBIDDEN_FIELDS",
    "COORDINATOR_ACTION_ALLOWED_FIELDS",
    "WORKER_REQUEST_ALLOWED_FIELDS",
    "AUDIT_REQUEST_ALLOWED_FIELDS",
    "TASK_ASSESSMENT_ALLOWED_FIELDS",
    "INITIAL_RESPONSE_ALLOWED_FIELDS",
    "TASK_ASSESSMENT_SCHEMA",
    "WORKER_REQUEST_SCHEMA",
    "AUDIT_REQUEST_SCHEMA",
    "COORDINATOR_ACTION_SCHEMA",
    "INITIAL_RESPONSE_SCHEMA",
    "get_coordinator_action_schema",
    "get_task_assessment_schema",
    "get_initial_response_schema",
    # Responses & Parsers
    "InitialCoordinatorResponse",
    "extract_json_payload",
    "check_forbidden_fields",
    "check_allowed_fields",
    "parse_coordinator_action",
    "parse_initial_response",
    "validate_coordinator_action",
    # Infrastructure Failures
    "InfrastructureFailureReason",
    "InfrastructureFailure",
    "create_worker_failed_failure",
    "create_profile_unavailable_failure",
    "create_quota_exhausted_failure",
    "create_action_rejected_failure",
    "create_budget_rejected_failure",
    "create_timeout_failure",
    "failure_to_worker_result",
]


# ============================================================================
# 1. Protocol Exceptions
# ============================================================================


class ProtocolError(Exception):
    """Base exception for all orchestration protocol errors."""


class MalformedResponseError(ProtocolError, ValueError):
    """Raised when coordinator response cannot be parsed as JSON or extracted."""


class ForbiddenFieldError(ProtocolError, ValueError):
    """Raised when coordinator response contains forbidden fields (profile, command, etc.)."""


class ProtocolSchemaError(ProtocolError, ValueError):
    """Raised when coordinator response violates the expected schema or contains unknown fields."""


class ActionValidationError(ProtocolError, ValueError):
    """Raised when coordinator action violates structural or semantic rules."""


# ============================================================================
# 2. Field Allow / Deny Lists
# ============================================================================

FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "profile",
    "profile_name",
    "command",
    "argv",
    "shell",
    "environment",
    "env",
    "cmd",
    "process",
})

COORDINATOR_ACTION_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "action_id",
    "kind",
    "workers",
    "auditors",
    "reason_summary",
    "final_response",
})

WORKER_REQUEST_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "worker_id",
    "role",
    "strategy",
    "workspace_mode",
    "objective",
    "context_worker_ids",
    "timeout_seconds",
})

AUDIT_REQUEST_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "worker_id",
    "target_worker_ids",
    "focus",
    "strategy",
    "timeout_seconds",
})

TASK_ASSESSMENT_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "task_type",
    "complexity",
    "confidence",
    "mutation_required",
    "repository_scope",
    "value_of_parallel_reasoning",
    "value_of_auditing",
    "summary",
    "proposed_initial_work",
})

INITIAL_RESPONSE_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "assessment",
    "action",
})


# ============================================================================
# 3. JSON Schemas
# ============================================================================

TASK_ASSESSMENT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "TaskAssessment",
    "type": "object",
    "required": [
        "task_type",
        "complexity",
        "confidence",
        "mutation_required",
        "repository_scope",
    ],
    "properties": {
        "task_type": {
            "type": "string",
            "enum": [t.value for t in TaskType],
            "description": "Classification of the primary task.",
        },
        "complexity": {
            "type": "string",
            "enum": [c.value for c in ComplexityLevel],
            "description": "Assessed difficulty and scope of the task.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence score between 0.0 and 1.0.",
        },
        "mutation_required": {
            "type": "boolean",
            "description": "Whether filesystem modifications are required.",
        },
        "repository_scope": {
            "type": "string",
            "description": "Relevant paths or subsystem scope within repository.",
        },
        "value_of_parallel_reasoning": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.0,
            "description": "Utility of dispatching parallel analytical workers.",
        },
        "value_of_auditing": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.0,
            "description": "Utility of independent audit review before execution.",
        },
        "summary": {
            "type": "string",
            "default": "",
            "description": "Executive summary of the task analysis.",
        },
        "proposed_initial_work": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "High-level initial steps proposed.",
        },
    },
    "additionalProperties": False,
}

WORKER_REQUEST_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "WorkerRequest",
    "type": "object",
    "required": ["worker_id", "role"],
    "properties": {
        "worker_id": {
            "type": "string",
            "description": "Unique identifier for this worker within the action.",
        },
        "role": {
            "type": "string",
            "enum": [r.value for r in WorkerRole],
            "description": "Assigned specialization role.",
        },
        "strategy": {
            "type": "string",
            "enum": [s.value for s in ExecutionStrategy],
            "default": ExecutionStrategy.STANDARD.value,
            "description": "Effort tier (STANDARD, HIGH_EFFORT, BOOST).",
        },
        "workspace_mode": {
            "type": "string",
            "enum": [w.value for w in WorkspaceMode],
            "default": WorkspaceMode.READ_ONLY.value,
            "description": "Workspace policy (READ_ONLY, MUTATING). MUTATING is only valid for EXECUTOR.",
        },
        "objective": {
            "type": "string",
            "default": "",
            "description": "Specific instruction and deliverable for this worker.",
        },
        "context_worker_ids": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "IDs of prior completed workers whose outputs should be in context.",
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0.0,
            "default": 300.0,
            "description": "Execution timeout in seconds.",
        },
    },
    "additionalProperties": False,
}

AUDIT_REQUEST_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AuditRequest",
    "type": "object",
    "required": ["worker_id", "target_worker_ids"],
    "properties": {
        "worker_id": {
            "type": "string",
            "description": "Unique identifier for this auditor within the action.",
        },
        "target_worker_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "IDs of prior completed workers to audit.",
        },
        "focus": {
            "type": "string",
            "default": "",
            "description": "Audit focus (e.g., edge cases, security, correctness, regression).",
        },
        "strategy": {
            "type": "string",
            "enum": [s.value for s in ExecutionStrategy],
            "default": ExecutionStrategy.STANDARD.value,
            "description": "Effort tier for the audit.",
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0.0,
            "default": 300.0,
            "description": "Execution timeout in seconds.",
        },
    },
    "additionalProperties": False,
}

COORDINATOR_ACTION_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "CoordinatorAction",
    "type": "object",
    "required": ["action_id", "kind"],
    "properties": {
        "action_id": {
            "type": "string",
            "description": "Unique identifier for this proposed coordinator action.",
        },
        "kind": {
            "type": "string",
            "enum": [k.value for k in ActionKind],
            "description": "Kind of action (RUN_WORKERS, RUN_AUDITORS, RUN_SYNTHESIS, RUN_EXECUTOR, FINALIZE).",
        },
        "workers": {
            "type": "array",
            "items": WORKER_REQUEST_SCHEMA,
            "default": [],
            "description": "Worker specifications for RUN_WORKERS, RUN_SYNTHESIS, or RUN_EXECUTOR.",
        },
        "auditors": {
            "type": "array",
            "items": AUDIT_REQUEST_SCHEMA,
            "default": [],
            "description": "Auditor specifications for RUN_AUDITORS.",
        },
        "reason_summary": {
            "type": "string",
            "default": "",
            "description": "Rationale for this decision.",
        },
        "final_response": {
            "type": ["string", "null"],
            "default": None,
            "description": "Final user-facing result. Only allowed for FINALIZE action.",
        },
    },
    "additionalProperties": False,
}

INITIAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "InitialCoordinatorResponse",
    "type": "object",
    "required": ["assessment", "action"],
    "properties": {
        "assessment": TASK_ASSESSMENT_SCHEMA,
        "action": COORDINATOR_ACTION_SCHEMA,
    },
    "additionalProperties": False,
}


def get_coordinator_action_schema() -> dict[str, Any]:
    """Return JSON schema for CoordinatorAction."""
    return dict(COORDINATOR_ACTION_SCHEMA)


def get_task_assessment_schema() -> dict[str, Any]:
    """Return JSON schema for TaskAssessment."""
    return dict(TASK_ASSESSMENT_SCHEMA)


def get_initial_response_schema() -> dict[str, Any]:
    """Return JSON schema for the initial Round 0 coordinator response."""
    return dict(INITIAL_RESPONSE_SCHEMA)


# ============================================================================
# 4. Initial Coordinator Response Container
# ============================================================================


@dataclass
class InitialCoordinatorResponse:
    """Coordinator's mandatory initial response combining TaskAssessment and CoordinatorAction."""

    assessment: TaskAssessment
    action: CoordinatorAction

    def __iter__(self):
        return iter((self.assessment, self.action))

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment": self.assessment.to_dict(),
            "action": self.action.to_dict(),
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ============================================================================
# 5. Extraction & Forbidden Field Verification
# ============================================================================


def extract_json_payload(raw_response: str) -> str:
    """Extract a JSON payload string from a raw model response.

    Supports:
    - Pure JSON strings
    - Markdown fenced code blocks (```json ... ``` or ``` ... ```)
    - Responses with leading/trailing commentary surrounding a JSON object

    Raises:
        MalformedResponseError: if no valid JSON object structure can be extracted.
    """
    if not isinstance(raw_response, str):
        raise MalformedResponseError(f"Expected string response, got {type(raw_response).__name__}")

    cleaned = raw_response.strip()
    if not cleaned:
        raise MalformedResponseError("Received empty response from coordinator")

    # Fast path: already direct valid JSON object
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return cleaned
        except Exception:
            pass

    # Look for markdown code fences with json or generic block
    fence_pattern = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
    matches = fence_pattern.findall(cleaned)
    for match in matches:
        match_str = match.strip()
        if match_str.startswith("{") and match_str.endswith("}"):
            try:
                parsed = json.loads(match_str)
                if isinstance(parsed, dict):
                    return match_str
            except Exception:
                continue

    # Fallback: scan for first '{' and matching last '}'
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = cleaned[first_brace : last_brace + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return candidate
        except Exception:
            pass

    raise MalformedResponseError(
        f"Failed to extract a valid JSON object from model response: {cleaned[:200]!r}"
    )


def check_forbidden_fields(data: Any, path: str = "") -> None:
    """Recursively check for forbidden fields in structured coordinator input.

    Rejects any presence of profile names, shell commands, argv, or environment settings.

    Raises:
        ForbiddenFieldError: if any forbidden field is detected anywhere in the payload.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            norm_key = str(key).strip().lower()
            current_path = f"{path}.{key}" if path else str(key)
            if norm_key in FORBIDDEN_FIELDS:
                raise ForbiddenFieldError(
                    f"Forbidden field '{key}' detected at '{current_path}'. "
                    "The coordinator may not specify profiles, commands, argv, shell, or environment."
                )
            check_forbidden_fields(value, current_path)
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            check_forbidden_fields(item, f"{path}[{idx}]")


def check_allowed_fields(data: dict[str, Any], allowed_fields: frozenset[str], context: str) -> None:
    """Validate that only permitted keys exist in a dictionary.

    Raises:
        ProtocolSchemaError: if any unexpected field is encountered.
    """
    if not isinstance(data, dict):
        raise ProtocolSchemaError(f"Expected dict for {context}, got {type(data).__name__}")
    for key in data.keys():
        if key not in allowed_fields:
            raise ProtocolSchemaError(
                f"Unexpected field '{key}' in {context}. Allowed fields are: {sorted(allowed_fields)}"
            )


# ============================================================================
# 6. Parser Implementation
# ============================================================================


def parse_coordinator_action(
    raw_response: str | dict[str, Any],
    known_worker_ids: set[WorkerId] | set[str] | None = None,
) -> CoordinatorAction:
    """Parse and validate a CoordinatorAction from a raw response or dictionary.

    Args:
        raw_response: Raw model output string or parsed dict.
        known_worker_ids: Optional set of prior worker IDs to validate context/target references against.

    Returns:
        A validated CoordinatorAction instance.

    Raises:
        MalformedResponseError: If JSON decoding fails or structure is not a dict.
        ForbiddenFieldError: If forbidden fields (profile, command, etc.) are present.
        ProtocolSchemaError: If fields are unexpected or enum types are invalid.
        ActionValidationError: If structural or semantic rules are violated.
    """
    if isinstance(raw_response, str):
        json_str = extract_json_payload(raw_response)
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise MalformedResponseError(f"Invalid JSON: {exc}") from exc
    elif isinstance(raw_response, dict):
        data = raw_response
    else:
        raise MalformedResponseError(
            f"Expected str or dict for coordinator action, got {type(raw_response).__name__}"
        )

    if not isinstance(data, dict):
        raise MalformedResponseError(f"Expected JSON object (dict), got {type(data).__name__}")

    # 1. Reject forbidden fields anywhere in the payload
    check_forbidden_fields(data)

    # 2. Strict schema check on top-level action fields
    check_allowed_fields(data, COORDINATOR_ACTION_ALLOWED_FIELDS, "CoordinatorAction")

    # 3. Check required action fields
    action_id_raw = data.get("action_id")
    if not action_id_raw or not isinstance(action_id_raw, str):
        raise ProtocolSchemaError("Missing or invalid 'action_id' in CoordinatorAction")

    kind_raw = data.get("kind")
    if not kind_raw:
        raise ProtocolSchemaError("Missing required 'kind' in CoordinatorAction")
    try:
        ActionKind(kind_raw)
    except Exception as exc:
        raise ProtocolSchemaError(f"Unknown or invalid action kind '{kind_raw}'") from exc

    # 4. Check worker requests if present
    workers_raw = data.get("workers", [])
    if not isinstance(workers_raw, list):
        raise ProtocolSchemaError("'workers' must be a list")
    for idx, w in enumerate(workers_raw):
        if not isinstance(w, dict):
            raise ProtocolSchemaError(f"WorkerRequest at index {idx} must be a dict")
        check_forbidden_fields(w, f"workers[{idx}]")
        check_allowed_fields(w, WORKER_REQUEST_ALLOWED_FIELDS, f"WorkerRequest[{idx}]")
        if "worker_id" not in w or not w["worker_id"]:
            raise ProtocolSchemaError(f"WorkerRequest at index {idx} missing 'worker_id'")
        if "role" not in w:
            raise ProtocolSchemaError(f"WorkerRequest at index {idx} missing 'role'")
        try:
            WorkerRole(w["role"])
        except Exception as exc:
            raise ProtocolSchemaError(f"Unknown worker role '{w.get('role')}' at index {idx}") from exc
        if "strategy" in w:
            try:
                ExecutionStrategy(w["strategy"])
            except Exception as exc:
                raise ProtocolSchemaError(f"Unknown execution strategy '{w.get('strategy')}' at index {idx}") from exc
        if "workspace_mode" in w:
            try:
                WorkspaceMode(w["workspace_mode"])
            except Exception as exc:
                raise ProtocolSchemaError(f"Unknown workspace mode '{w.get('workspace_mode')}' at index {idx}") from exc

    # 5. Check audit requests if present
    auditors_raw = data.get("auditors", [])
    if not isinstance(auditors_raw, list):
        raise ProtocolSchemaError("'auditors' must be a list")
    for idx, a in enumerate(auditors_raw):
        if not isinstance(a, dict):
            raise ProtocolSchemaError(f"AuditRequest at index {idx} must be a dict")
        check_forbidden_fields(a, f"auditors[{idx}]")
        check_allowed_fields(a, AUDIT_REQUEST_ALLOWED_FIELDS, f"AuditRequest[{idx}]")
        if "worker_id" not in a or not a["worker_id"]:
            raise ProtocolSchemaError(f"AuditRequest at index {idx} missing 'worker_id'")
        if "target_worker_ids" not in a or not isinstance(a["target_worker_ids"], list):
            raise ProtocolSchemaError(f"AuditRequest at index {idx} missing list 'target_worker_ids'")
        if "strategy" in a:
            try:
                ExecutionStrategy(a["strategy"])
            except Exception as exc:
                raise ProtocolSchemaError(f"Unknown strategy '{a.get('strategy')}' in auditor {idx}") from exc

    # 6. Instantiate contract dataclass (converts and triggers internal validation)
    try:
        action = CoordinatorAction.from_dict(data)
    except ValueError as exc:
        raise ActionValidationError(str(exc)) from exc

    # 7. Semantic and context-dependent validation
    validate_coordinator_action(action, known_worker_ids=known_worker_ids)

    return action


def parse_initial_response(
    raw_response: str | dict[str, Any],
    known_worker_ids: set[WorkerId] | set[str] | None = None,
) -> InitialCoordinatorResponse:
    """Parse and validate the coordinator's initial Round 0 response.

    The response MUST contain both a valid TaskAssessment and a valid CoordinatorAction.
    Prose-only decisions without this structure are explicitly rejected.

    Args:
        raw_response: Raw model output string or parsed dict.
        known_worker_ids: Optional set of prior worker IDs (usually empty on round 0).

    Returns:
        InitialCoordinatorResponse containing assessment and action.

    Raises:
        MalformedResponseError: If JSON decoding fails or structure is not a dict.
        ForbiddenFieldError: If forbidden fields are present.
        ProtocolSchemaError: If either assessment or action is missing, or schema violated.
        ActionValidationError: If coordinator action violates rules.
    """
    if isinstance(raw_response, str):
        json_str = extract_json_payload(raw_response)
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise MalformedResponseError(f"Invalid JSON: {exc}") from exc
    elif isinstance(raw_response, dict):
        data = raw_response
    else:
        raise MalformedResponseError(
            f"Expected str or dict for initial response, got {type(raw_response).__name__}"
        )

    if not isinstance(data, dict):
        raise MalformedResponseError(f"Expected JSON object (dict), got {type(data).__name__}")

    # 1. Check forbidden fields
    check_forbidden_fields(data)

    # 2. Check allowed top-level keys
    check_allowed_fields(data, INITIAL_RESPONSE_ALLOWED_FIELDS, "InitialCoordinatorResponse")

    # 3. Check presence of both assessment and action
    if "assessment" not in data or not isinstance(data["assessment"], dict):
        raise ProtocolSchemaError(
            "Initial coordinator response must contain an 'assessment' object. "
            "Arbitrary prose-only decisions are not permitted."
        )
    if "action" not in data or not isinstance(data["action"], dict):
        raise ProtocolSchemaError(
            "Initial coordinator response must contain an 'action' object. "
            "Arbitrary prose-only decisions are not permitted."
        )

    # 4. Check assessment schema
    assessment_dict = data["assessment"]
    check_forbidden_fields(assessment_dict, "assessment")
    check_allowed_fields(assessment_dict, TASK_ASSESSMENT_ALLOWED_FIELDS, "TaskAssessment")

    task_type_raw = assessment_dict.get("task_type")
    if not task_type_raw:
        raise ProtocolSchemaError("TaskAssessment missing required 'task_type'")
    try:
        TaskType(task_type_raw)
    except Exception as exc:
        raise ProtocolSchemaError(f"Unknown task_type '{task_type_raw}' in TaskAssessment") from exc

    complexity_raw = assessment_dict.get("complexity")
    if not complexity_raw:
        raise ProtocolSchemaError("TaskAssessment missing required 'complexity'")
    try:
        ComplexityLevel(complexity_raw)
    except Exception as exc:
        raise ProtocolSchemaError(f"Unknown complexity '{complexity_raw}' in TaskAssessment") from exc

    try:
        assessment = TaskAssessment.from_dict(assessment_dict)
    except ValueError as exc:
        raise ProtocolSchemaError(f"TaskAssessment validation error: {exc}") from exc

    # 5. Parse and validate action
    action = parse_coordinator_action(data["action"], known_worker_ids=known_worker_ids)

    return InitialCoordinatorResponse(assessment=assessment, action=action)


# ============================================================================
# 7. Action Validation
# ============================================================================


def validate_coordinator_action(
    action: CoordinatorAction,
    known_worker_ids: set[WorkerId] | set[str] | None = None,
) -> None:
    """Perform structural and semantic validation on a CoordinatorAction.

    Checks enforced:
    - RUN_WORKERS has at least one worker and no auditors.
    - RUN_AUDITORS has at least one auditor and no workers.
    - FINALIZE has no workers and no auditors.
    - RUN_EXECUTOR contains exactly one worker with role EXECUTOR and no auditors.
    - MUTATING mode is only valid for EXECUTOR role in RUN_EXECUTOR.
    - Duplicate WorkerIds anywhere in the action are rejected.
    - Unknown referenced worker IDs are rejected where context is supplied.
    - Non-FINALIZE actions may not include final_response.

    Note: Budget limits are enforced by the engine, not by this protocol validator.

    Raises:
        ActionValidationError: If any validation rule fails.
    """
    # 1. Base contract validation (rules on action kind)
    try:
        action.validate()
    except ValueError as exc:
        raise ActionValidationError(str(exc)) from exc

    # 2. Strict empty worker/auditor wave checks
    if action.kind == ActionKind.RUN_WORKERS:
        if not action.workers:
            raise ActionValidationError("RUN_WORKERS action requires at least one worker")
        if action.auditors:
            raise ActionValidationError("RUN_WORKERS action cannot contain auditors")

    elif action.kind == ActionKind.RUN_AUDITORS:
        if not action.auditors:
            raise ActionValidationError("RUN_AUDITORS action requires at least one auditor")
        if action.workers:
            raise ActionValidationError("RUN_AUDITORS action cannot contain workers")

    elif action.kind == ActionKind.RUN_SYNTHESIS:
        if not action.workers:
            raise ActionValidationError("RUN_SYNTHESIS action requires at least one worker")
        if action.auditors:
            raise ActionValidationError("RUN_SYNTHESIS action cannot contain auditors")
        for w in action.workers:
            if w.role != WorkerRole.SYNTHESIZER:
                raise ActionValidationError(
                    f"RUN_SYNTHESIS action workers must have role SYNTHESIZER, got {w.role}"
                )

    elif action.kind == ActionKind.RUN_EXECUTOR:
        if len(action.workers) != 1:
            raise ActionValidationError("RUN_EXECUTOR action must contain exactly one worker")
        if action.auditors:
            raise ActionValidationError("RUN_EXECUTOR action cannot contain auditors")
        if action.workers[0].role != WorkerRole.EXECUTOR:
            raise ActionValidationError("RUN_EXECUTOR worker must have role EXECUTOR")

    elif action.kind == ActionKind.FINALIZE:
        if action.workers:
            raise ActionValidationError("FINALIZE action cannot contain workers")
        if action.auditors:
            raise ActionValidationError("FINALIZE action cannot contain auditors")

    # 3. Mutating workspace mode constraints
    mutating_workers = [w for w in action.workers if w.workspace_mode == WorkspaceMode.MUTATING]
    if len(mutating_workers) > 1:
        raise ActionValidationError(
            f"Multiple mutating workers ({len(mutating_workers)}) are not permitted in an action"
        )
    if mutating_workers:
        if action.kind != ActionKind.RUN_EXECUTOR:
            raise ActionValidationError(
                "Mutating workers are only permitted in RUN_EXECUTOR actions"
            )
        if mutating_workers[0].role != WorkerRole.EXECUTOR:
            raise ActionValidationError(
                "Worker with MUTATING workspace_mode must have role EXECUTOR"
            )

    # 4. Duplicate WorkerId rejection
    seen_ids: set[str] = set()
    for w in action.workers:
        w_id_str = str(w.worker_id)
        if w_id_str in seen_ids:
            raise ActionValidationError(f"Duplicate worker_id '{w_id_str}' in action")
        seen_ids.add(w_id_str)

    for a in action.auditors:
        a_id_str = str(a.worker_id)
        if a_id_str in seen_ids:
            raise ActionValidationError(f"Duplicate worker_id '{a_id_str}' in action")
        seen_ids.add(a_id_str)

    # 5. Unknown result references rejection where context is supplied
    if known_worker_ids is not None:
        known_str_ids = {str(k) for k in known_worker_ids}

        for w in action.workers:
            for ctx_id in w.context_worker_ids:
                if str(ctx_id) not in known_str_ids:
                    raise ActionValidationError(
                        f"Worker '{w.worker_id}' references unknown worker '{ctx_id}' in context_worker_ids"
                    )

        for a in action.auditors:
            for tgt_id in a.target_worker_ids:
                if str(tgt_id) not in known_str_ids:
                    raise ActionValidationError(
                        f"Auditor '{a.worker_id}' references unknown worker '{tgt_id}' in target_worker_ids"
                    )


# ============================================================================
# 8. Standardized Infrastructure Failures
# ============================================================================


class InfrastructureFailureReason(str, Enum):
    """Standardized reasons for infrastructure problems returned to the coordinator."""

    WORKER_FAILED = "worker_failed"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    QUOTA_EXHAUSTED = "quota_exhausted"
    ACTION_REJECTED = "action_rejected"
    BUDGET_REJECTED = "budget_rejected"
    TIMEOUT = "timeout"


@dataclass
class InfrastructureFailure:
    """Standardized representation of an infrastructure problem.

    Returned to the coordinator so it can semantically decide whether to:
    - retry
    - replace worker
    - reduce scope
    - continue
    - finalize
    """

    reason: InfrastructureFailureReason
    message: str
    worker_id: WorkerId | None = None
    action_id: ActionId | None = None
    details: dict[str, Any] = field(default_factory=dict)
    failure_class: FailureClass = FailureClass.UNRECOVERABLE
    suggested_action: str = "finalize"  # "retry", "replace_worker", "reduce_scope", "continue", "finalize"

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "message": self.message,
            "worker_id": str(self.worker_id) if self.worker_id is not None else None,
            "action_id": str(self.action_id) if self.action_id is not None else None,
            "details": dict(self.details),
            "failure_class": self.failure_class.value,
            "suggested_action": self.suggested_action,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def create_worker_failed_failure(
    worker_id: WorkerId | str,
    error: str,
    retryable: bool = True,
) -> InfrastructureFailure:
    """Construct standardized failure when a worker invocation fails."""
    return InfrastructureFailure(
        reason=InfrastructureFailureReason.WORKER_FAILED,
        message=f"Worker '{worker_id}' failed execution: {error}",
        worker_id=WorkerId(worker_id),
        failure_class=FailureClass.RETRYABLE if retryable else FailureClass.UNRECOVERABLE,
        suggested_action="retry" if retryable else "replace_worker",
    )


def create_profile_unavailable_failure(
    worker_id: WorkerId | str,
    role: WorkerRole | str | None = None,
) -> InfrastructureFailure:
    """Construct standardized failure when no profile is available for lease."""
    return InfrastructureFailure(
        reason=InfrastructureFailureReason.PROFILE_UNAVAILABLE,
        message=f"No AGYM profile currently available to execute worker '{worker_id}'",
        worker_id=WorkerId(worker_id),
        details={"role": str(role) if role is not None else None},
        failure_class=FailureClass.RECOVERABLE,
        suggested_action="reduce_scope",
    )


def create_quota_exhausted_failure(
    worker_id: WorkerId | str,
    strategy: ExecutionStrategy | str | None = None,
) -> InfrastructureFailure:
    """Construct standardized failure when profile quota is exhausted."""
    return InfrastructureFailure(
        reason=InfrastructureFailureReason.QUOTA_EXHAUSTED,
        message=f"Quota exhausted for worker '{worker_id}' at strategy tier {strategy or 'STANDARD'}",
        worker_id=WorkerId(worker_id),
        details={"strategy": str(strategy) if strategy is not None else None},
        failure_class=FailureClass.RECOVERABLE,
        suggested_action="reduce_scope",
    )


def create_action_rejected_failure(
    action_id: ActionId | str,
    reason: str,
) -> InfrastructureFailure:
    """Construct standardized failure when engine rejects coordinator action."""
    return InfrastructureFailure(
        reason=InfrastructureFailureReason.ACTION_REJECTED,
        message=f"Coordinator action '{action_id}' was rejected: {reason}",
        action_id=ActionId(action_id),
        failure_class=FailureClass.RECOVERABLE,
        suggested_action="replace_worker",
    )


def create_budget_rejected_failure(
    reason: str,
    budget_usage: BudgetUsage | None = None,
) -> InfrastructureFailure:
    """Construct standardized failure when action exceeds orchestration budget."""
    return InfrastructureFailure(
        reason=InfrastructureFailureReason.BUDGET_REJECTED,
        message=f"Action rejected by budget constraints: {reason}",
        details={"budget_usage": budget_usage.to_dict() if budget_usage is not None else None},
        failure_class=FailureClass.UNRECOVERABLE,
        suggested_action="finalize",
    )


def create_timeout_failure(
    worker_id: WorkerId | str,
    timeout_seconds: float,
) -> InfrastructureFailure:
    """Construct standardized failure when a worker invocation times out."""
    return InfrastructureFailure(
        reason=InfrastructureFailureReason.TIMEOUT,
        message=f"Worker '{worker_id}' timed out after {timeout_seconds} seconds",
        worker_id=WorkerId(worker_id),
        details={"timeout_seconds": timeout_seconds},
        failure_class=FailureClass.RETRYABLE,
        suggested_action="retry",
    )


def failure_to_worker_result(
    failure: InfrastructureFailure,
    role: WorkerRole = WorkerRole.GENERAL,
) -> WorkerResult:
    """Convert an InfrastructureFailure into a WorkerResult for coordinator observation."""
    return WorkerResult(
        worker_id=failure.worker_id or WorkerId("unknown"),
        role=role,
        status=InvocationStatus.FAILED,
        response=failure.message,
        failure=failure.failure_class,
        structured_data={
            "failure_reason": failure.reason.value,
            "suggested_action": failure.suggested_action,
            "details": failure.details,
        },
    )
