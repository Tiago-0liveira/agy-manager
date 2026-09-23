"""Deterministic orchestration execution engine for AGYM.

This module implements the deterministic AGYM orchestration engine:
- Authoritative execution of validated coordinator actions
- Strict budget, workspace, and protocol validation
- Transactional profile allocation and lease lifecycle management
- Parallel worker and auditor dispatch with isolated task registries
- Automatic conservative mechanical retries on alternative profiles
- Safe interruption handling (Ctrl+C / cancellation) and lease revocation
- Robust run resumption with unfinished invocation classification
- Non-mutating dry-run planning for previewing execution
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Sequence

from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    AuditRequest,
    AuditResult,
    BudgetUsage,
    ComplexityLevel,
    ConversationId,
    CoordinatorAction,
    CoordinatorClient,
    CoordinatorObservation,
    EventType,
    ExecutionStrategy,
    FailureClass,
    FleetView,
    InvocationId,
    InvocationStatus,
    ModelInvocation,
    ModelResult,
    ModelRunner,
    OrchestrationBudget,
    OrchestrationEvent,
    ProfileLease,
    ProfileLeaseManager,
    ProfileScheduler,
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
from agym.orchestration.leases import is_pid_alive
from agym.orchestration.prompts import (
    build_synthesis_prompt,
    format_coordinator_observation,
)
from agym.orchestration.protocol import (
    ActionValidationError,
    InitialCoordinatorResponse,
    ProtocolError,
    create_action_rejected_failure,
    create_budget_rejected_failure,
    create_profile_unavailable_failure,
    create_quota_exhausted_failure,
    create_timeout_failure,
    create_worker_failed_failure,
    failure_to_worker_result,
    parse_coordinator_action,
    parse_initial_response,
    validate_coordinator_action,
)
from agym.orchestration.runner import AntigravityRunner, classify_failure
from agym.orchestration.scheduler import InsufficientCapacityError

logger = logging.getLogger(__name__)

__all__ = [
    "OrchestrationEngine",
    "DryRunPlan",
    "EngineError",
    "BudgetExceededError",
    "ActionRejectedError",
    "build_worker_prompt",
    "build_auditor_prompt",
]


# ============================================================================
# 1. Custom Exceptions
# ============================================================================


class EngineError(RuntimeError):
    """Base exception for orchestration engine errors."""


class BudgetExceededError(EngineError):
    """Raised when an action or run exceeds hard budget constraints."""


class ActionRejectedError(EngineError):
    """Raised when an unrecoverable coordinator action rejection occurs."""


# ============================================================================
# 2. Dry Run Data Structure
# ============================================================================


@dataclass
class DryRunPlan:
    """Structured plan returned by engine dry-run mode."""

    run_id: RunId
    task: str
    mode: RunMode
    assessment: TaskAssessment | None = None
    action: CoordinatorAction | None = None
    is_valid: bool = True
    validation_error: str | None = None
    fleet_view: FleetView = field(default_factory=FleetView)
    budget: OrchestrationBudget = field(default_factory=OrchestrationBudget)
    planned_workers: list[dict[str, Any]] = field(default_factory=list)
    planned_auditors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "task": self.task,
            "mode": self.mode.value,
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "action": self.action.to_dict() if self.action else None,
            "is_valid": self.is_valid,
            "validation_error": self.validation_error,
            "fleet_view": self.fleet_view.to_dict(),
            "budget": self.budget.to_dict(),
            "planned_workers": list(self.planned_workers),
            "planned_auditors": list(self.planned_auditors),
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def format_display(self) -> str:
        """Format structured dry-run plan for CLI/UI display."""
        lines = [
            f"=== Dry Run Plan [{self.run_id}] ===",
            f"Task: {self.task}",
            f"Mode: {self.mode.value}",
            f"Validation Status: {'VALID' if self.is_valid else 'INVALID'}",
        ]
        if self.validation_error:
            lines.append(f"Validation Issue: {self.validation_error}")
        if self.assessment:
            lines.extend([
                f"Assessed Type: {self.assessment.task_type.value}",
                f"Assessed Complexity: {self.assessment.complexity.value}",
                f"Confidence: {self.assessment.confidence:.2f}",
                f"Mutation Required: {self.assessment.mutation_required}",
            ])
            if self.assessment.summary:
                lines.append(f"Assessment Summary: {self.assessment.summary}")
        if self.action:
            lines.append(f"Proposed Action: {self.action.kind.value} (ID: {self.action.action_id})")
            if self.planned_workers:
                lines.append(f"Planned Workers ({len(self.planned_workers)}):")
                for w in self.planned_workers:
                    lines.append(
                        f"  - [{w.get('worker_id')}] Role: {w.get('role')} | "
                        f"Strategy: {w.get('strategy')} | Mode: {w.get('workspace_mode')}"
                    )
            if self.planned_auditors:
                lines.append(f"Planned Auditors ({len(self.planned_auditors)}):")
                for a in self.planned_auditors:
                    lines.append(
                        f"  - [{a.get('worker_id')}] Targets: {a.get('target_worker_ids')} | "
                        f"Focus: {a.get('focus')}"
                    )
        lines.append(
            f"Fleet Capacity: {self.fleet_view.available_profiles} available profiles "
            f"(max parallel: {self.fleet_view.max_parallel})"
        )
        return "\n".join(lines)


# ============================================================================
# 3. Prompt Builders
# ============================================================================


def build_worker_prompt(
    task: str,
    role: WorkerRole,
    objective: str = "",
    repository_scope: str = "",
    context_results: list[WorkerResult] | None = None,
) -> str:
    """Build a prompt for an analytical or specialized worker.

    Ensures workers receive only:
    - Original user task
    - Assigned specialization role
    - Specific objective
    - Repository scope
    - Explicitly requested context outputs (context_worker_ids)
    Workers never see outputs of sibling workers in the same wave unless explicitly provided.
    """
    sections = [
        f"# Task: {role.value} Worker",
        "",
        "## Original Task",
        task.strip(),
        "",
        f"## Your Role: {role.value}",
    ]
    if objective and objective.strip():
        sections.extend(["", "## Specific Objective", objective.strip()])
    if repository_scope and repository_scope.strip():
        sections.extend(["", "## Repository Scope", repository_scope.strip()])
    if context_results:
        sections.extend(["", "## Context from Prior Workers"])
        for cr in context_results:
            sections.append(f"### Worker {cr.worker_id} ({cr.role.value}):")
            if cr.response:
                sections.append(cr.response.strip())
            if cr.structured_data:
                sections.append(f"```json\n{json.dumps(cr.structured_data, indent=2)}\n```")
    sections.extend([
        "",
        "## Instructions",
        f"Perform your analysis and execution strictly in accordance with your role ({role.value}).",
        "Deliver clear, high-quality technical output addressing your objective.",
    ])
    return "\n".join(sections)


def build_auditor_prompt(
    task: str,
    focus: str = "",
    repository_scope: str = "",
    target_results: list[WorkerResult] | None = None,
) -> str:
    """Build a prompt for an independent auditor inspecting prior worker outputs."""
    sections = [
        "# Task: Auditor Review",
        "",
        "## Original Task",
        task.strip(),
        "",
        "## Role: AUDITOR",
    ]
    if focus and focus.strip():
        sections.extend(["", "## Audit Focus", focus.strip()])
    if repository_scope and repository_scope.strip():
        sections.extend(["", "## Repository Scope", repository_scope.strip()])
    if target_results:
        sections.extend(["", "## Target Worker Outputs to Audit"])
        for tr in target_results:
            sections.append(f"### Target Worker {tr.worker_id} ({tr.role.value}):")
            if tr.response:
                sections.append(tr.response.strip())
            if tr.structured_data:
                sections.append(f"```json\n{json.dumps(tr.structured_data, indent=2)}\n```")
    sections.extend([
        "",
        "## Instructions",
        "Critically evaluate the target worker outputs. Highlight potential bugs, security concerns,",
        "regressions, omissions, edge cases, and architectural weaknesses.",
        "List your concrete findings as bullet points.",
    ])
    return "\n".join(sections)


# ============================================================================
# 4. Deterministic Orchestration Engine
# ============================================================================


class OrchestrationEngine:
    """Deterministic execution engine for AGYM orchestration runs.

    Coordinates execution between the coordinator brain and AGYM host safety rules:
    - Creates and persists run states atomically
    - Queries scheduler for fleet capacity
    - Validates actions against protocol, budget, mode, and fleet limits
    - Transactionally acquires and releases exclusive profile leases
    - Executes parallel waves with per-run task and process registries
    - Handles Ctrl+C and cancellation cleanly with full resource cleanup
    - Supports resuming interrupted or crashed runs
    - Supports dry-run inspection without worker dispatch
    """

    def __init__(
        self,
        scheduler: ProfileScheduler | None = None,
        lease_manager: ProfileLeaseManager | None = None,
        store: RunStore | None = None,
        runner: ModelRunner | None = None,
        coordinator: CoordinatorClient | None = None,
        default_budget: OrchestrationBudget | None = None,
    ) -> None:
        if scheduler is not None:
            self.scheduler = scheduler
        else:
            from agym.orchestration.scheduler import ProfileScheduler as DefaultScheduler
            self.scheduler = DefaultScheduler()

        if lease_manager is not None:
            self.lease_manager = lease_manager
        elif hasattr(self.scheduler, "lease_manager") and getattr(self.scheduler, "lease_manager") is not None:
            self.lease_manager = getattr(self.scheduler, "lease_manager")
        else:
            from agym.orchestration.leases import ProfileLeaseManager as DefaultLeaseManager
            self.lease_manager = DefaultLeaseManager()

        if store is not None:
            self.store = store
        else:
            from agym.orchestration.persistence import FileRunStore
            self.store = FileRunStore()

        if runner is not None:
            self.runner = runner
        else:
            self.runner = AntigravityRunner()

        self.coordinator = coordinator
        self.default_budget = default_budget or OrchestrationBudget()

        # Per-run active registries (Do not use global process tracking)
        self._active_tasks: dict[RunId, dict[InvocationId, asyncio.Task[Any]]] = {}
        self._active_leases: dict[RunId, list[ProfileLease]] = {}

    # ========================================================================
    # Event Emission Helper
    # ========================================================================

    def _emit_event(
        self,
        event_type: EventType,
        run_id: RunId,
        payload: dict[str, Any] | None = None,
    ) -> OrchestrationEvent:
        """Emit an orchestration lifecycle event to the run store / event sink."""
        now = datetime.now(timezone.utc).isoformat()
        evt = OrchestrationEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            type=event_type,
            timestamp=now,
            payload=dict(payload or {}),
        )
        if hasattr(self.store, "emit"):
            try:
                self.store.emit(evt)
            except Exception as exc:
                logger.warning("Failed to emit event %s for run %s: %s", event_type, run_id, exc)
        return evt

    # ========================================================================
    # Action & Budget Validation
    # ========================================================================

    def _validate_action(
        self,
        action: CoordinatorAction,
        state: RunState,
        fleet_view: FleetView,
        known_worker_ids: set[WorkerId],
    ) -> tuple[bool, str | None]:
        """Validate coordinator action against protocol, budget, mode, and capacity.

        Returns (is_valid, rejection_reason).
        """
        # 1. Structural protocol validation
        try:
            validate_coordinator_action(action, known_worker_ids=known_worker_ids)
        except ActionValidationError as exc:
            return False, f"Action validation failed: {exc}"
        except Exception as exc:
            return False, f"Unexpected validation error: {exc}"

        # 2. Mode and Workspace Mode Validation
        if state.mode == RunMode.PLAN:
            if action.kind == ActionKind.RUN_EXECUTOR:
                return False, "RUN_EXECUTOR action is not permitted in PLAN mode"
            for w in action.workers:
                if w.workspace_mode == WorkspaceMode.MUTATING:
                    return False, f"Mutating workspace mode is forbidden in PLAN mode (worker '{w.worker_id}')"
        elif state.mode == RunMode.IMPLEMENT:
            if action.kind == ActionKind.RUN_EXECUTOR:
                # Executor safety: exactly one mutating worker, role EXECUTOR
                if len(action.workers) != 1 or action.workers[0].role != WorkerRole.EXECUTOR:
                    return False, "RUN_EXECUTOR action must contain exactly one worker with EXECUTOR role"

        # 3. Finalize action does not require worker budget checks
        if action.kind == ActionKind.FINALIZE:
            return True, None

        total_requested = len(action.workers) + len(action.auditors)
        if total_requested <= 0:
            return False, f"Action kind {action.kind.value} requested 0 workers/auditors"

        # 4. Budget Enforcement: max_parallel
        if total_requested > state.budget.max_parallel:
            return (
                False,
                f"Action requests {total_requested} parallel invocations, "
                f"exceeding max_parallel limit of {state.budget.max_parallel}",
            )

        # 5. Budget Enforcement: max_invocations
        if state.budget_usage.invocations + total_requested > state.budget.max_invocations:
            return (
                False,
                f"Action would exceed max_invocations limit of {state.budget.max_invocations} "
                f"(used: {state.budget_usage.invocations}, requested: {total_requested})",
            )

        # 6. Budget Enforcement: max_rounds
        if state.round_number >= state.budget.max_rounds:
            return (
                False,
                f"Maximum rounds limit of {state.budget.max_rounds} reached (current round: {state.round_number})",
            )

        # 7. Budget Enforcement: max_boost_invocations
        boost_count = sum(
            1 for w in action.workers if getattr(w, "strategy", None) == ExecutionStrategy.BOOST
        ) + sum(
            1 for a in action.auditors if getattr(a, "strategy", None) == ExecutionStrategy.BOOST
        )
        if state.budget_usage.boost_invocations + boost_count > state.budget.max_boost_invocations:
            return (
                False,
                f"Action requests {boost_count} BOOST invocations, "
                f"exceeding max_boost_invocations limit of {state.budget.max_boost_invocations} "
                f"(used: {state.budget_usage.boost_invocations})",
            )

        # 8. Budget Enforcement: max_runtime_seconds
        if state.created_at:
            try:
                cdt = datetime.fromisoformat(state.created_at)
                if cdt.tzinfo is None:
                    cdt = cdt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - cdt).total_seconds()
                if elapsed > state.budget.max_runtime_seconds:
                    return (
                        False,
                        f"Max runtime limit of {state.budget.max_runtime_seconds:.1f}s exceeded "
                        f"(elapsed: {elapsed:.1f}s)",
                    )
            except Exception:
                pass

        # 9. Fleet Capacity Pre-Check
        if fleet_view.available_profiles < total_requested:
            return (
                False,
                f"Insufficient available profiles in fleet: needed {total_requested}, "
                f"available {fleet_view.available_profiles}",
            )
        if boost_count > fleet_view.boost_capacity:
            return (
                False,
                f"Insufficient boost tier capacity in fleet: needed {boost_count}, "
                f"available {fleet_view.boost_capacity}",
            )

        return True, None

    # ========================================================================
    # Resource Cleanup & Interruption
    # ========================================================================

    def _cleanup_leases(self, run_id: RunId) -> None:
        """Release all active profile leases owned by this run."""
        tracked_leases = list(self._active_leases.pop(run_id, []))
        for lease in tracked_leases:
            try:
                self.lease_manager.release(lease.lease_id, run_id=run_id)
                self._emit_event(
                    EventType.PROFILE_RELEASED,
                    run_id,
                    {
                        "lease_id": str(lease.lease_id),
                        "profile_name": lease.profile_name,
                        "worker_id": str(lease.worker_id),
                    },
                )
            except Exception as exc:
                logger.warning("Error releasing tracked lease %s: %s", lease.lease_id, exc)

        # Sweep lease manager for any remaining leases matching run_id
        try:
            active_leases = self.lease_manager.list_leases()
            for lease in active_leases:
                if str(lease.run_id) == str(run_id):
                    try:
                        self.lease_manager.release(lease.lease_id, run_id=run_id)
                        self._emit_event(
                            EventType.PROFILE_RELEASED,
                            run_id,
                            {
                                "lease_id": str(lease.lease_id),
                                "profile_name": lease.profile_name,
                                "worker_id": str(lease.worker_id),
                            },
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    def _handle_interruption(
        self,
        run_id: RunId,
        state: RunState,
        reason: str = "Run interrupted (Ctrl+C)",
        coordinator: CoordinatorClient | None = None,
    ) -> None:
        """Handle cancellation / SIGINT / Ctrl+C safely."""
        logger.warning("Interruption received for run %s: %s", run_id, reason)

        # 1. Cancel tracked asyncio tasks
        task_dict = self._active_tasks.pop(run_id, {})
        for task in task_dict.values():
            if not task.done():
                task.cancel()

        # 2. Terminate runner processes
        if hasattr(self.runner, "cancel_run"):
            try:
                self.runner.cancel_run(run_id)
            except Exception as exc:
                logger.warning("Error cancelling runner processes for %s: %s", run_id, exc)

        # 3. Release leases
        self._cleanup_leases(run_id)

        # 4. Close coordinator if it maintains a session
        coord = coordinator or self.coordinator
        if coord is not None and hasattr(coord, "close") and callable(coord.close):
            try:
                coord.close()
            except Exception:
                pass

        # 5. Persist state and append RUN_INTERRUPTED
        now = datetime.now(timezone.utc).isoformat()
        state.status = RunStatus.INTERRUPTED
        state.updated_at = now

        if hasattr(self.store, "mark_run_interrupted"):
            try:
                self.store.mark_run_interrupted(run_id, reason=reason)
            except Exception:
                self.store.save_run(state)
        else:
            self.store.save_run(state)
            self._emit_event(EventType.RUN_INTERRUPTED, run_id, {"reason": reason})

    # ========================================================================
    # Observation Construction
    # ========================================================================

    def _build_observation(
        self,
        state: RunState,
        completed: list[WorkerResult | AuditResult],
        failed: list[WorkerResult | AuditResult],
        rejected: list[WorkerRequest | AuditRequest],
    ) -> CoordinatorObservation:
        """Construct the CoordinatorObservation after a wave completes or an action is rejected."""
        fleet_view = self.scheduler.get_fleet_view()
        return CoordinatorObservation(
            completed_results=list(completed),
            failed_results=list(failed),
            rejected_requests=list(rejected),
            budget_usage=BudgetUsage(
                invocations=state.budget_usage.invocations,
                rounds=state.budget_usage.rounds,
                boost_invocations=state.budget_usage.boost_invocations,
                retries=state.budget_usage.retries,
                runtime_seconds=state.budget_usage.runtime_seconds,
            ),
            fleet_view=fleet_view,
            round_number=state.round_number,
        )

    # ========================================================================
    # Wave Execution
    # ========================================================================

    async def _execute_single_worker(
        self,
        worker_req: WorkerRequest,
        lease: ProfileLease,
        state: RunState,
        repository_scope: str = "",
    ) -> WorkerResult:
        """Execute a single worker invocation with automatic conservative retries."""
        iid = InvocationId(f"inv-{worker_req.worker_id}-{uuid.uuid4().hex[:6]}")
        current_lease = lease

        # Build role-specific prompt
        if worker_req.role == WorkerRole.SYNTHESIZER:
            all_results = self.store.get_results(state.run_id)
            prompt = build_synthesis_prompt(
                task=state.task,
                assessment=state.assessment,
                worker_results=all_results,
                objective=worker_req.objective,
                repository_facts=repository_scope,
            )
        else:
            context_results: list[WorkerResult] = []
            if worker_req.context_worker_ids:
                all_results = self.store.get_results(state.run_id)
                ctx_map = {
                    str(r.worker_id): r
                    for r in all_results
                    if isinstance(r, WorkerResult) and r.status == InvocationStatus.SUCCEEDED
                }
                for cid in worker_req.context_worker_ids:
                    if str(cid) in ctx_map:
                        context_results.append(ctx_map[str(cid)])

            prompt = build_worker_prompt(
                task=state.task,
                role=worker_req.role,
                objective=worker_req.objective,
                repository_scope=repository_scope,
                context_results=context_results,
            )

        invocation = ModelInvocation(
            invocation_id=iid,
            run_id=state.run_id,
            worker_id=worker_req.worker_id,
            role=worker_req.role,
            strategy=worker_req.strategy,
            workspace_mode=worker_req.workspace_mode,
            prompt=prompt,
            timeout_seconds=worker_req.timeout_seconds,
        )

        # Record invocation started before launch
        if hasattr(self.store, "record_invocation_started"):
            try:
                self.store.record_invocation_started(state.run_id, invocation, invocation_id=iid)
            except Exception as exc:
                logger.warning("Failed to record invocation started: %s", exc)
        else:
            self._emit_event(
                EventType.INVOCATION_STARTED,
                state.run_id,
                {
                    "invocation_id": str(iid),
                    "worker_id": str(worker_req.worker_id),
                    "role": worker_req.role.value,
                },
            )

        attempt = 0
        excluded_profiles: set[str] = set()

        try:
            while True:
                attempt += 1
                started_at = datetime.now(timezone.utc).isoformat()

                try:
                    if hasattr(self.runner, "run_async") and asyncio.iscoroutinefunction(self.runner.run_async):
                        model_res = await self.runner.run_async(invocation, profile_name=current_lease.profile_name)
                    else:
                        model_res = await asyncio.to_thread(self.runner.run, invocation, current_lease.profile_name)
                except (KeyboardInterrupt, asyncio.CancelledError) as exc:
                    raise asyncio.CancelledError("Interrupted by user (Ctrl+C)") from exc
                except Exception as exc:
                    model_res = ModelResult(
                        invocation_id=iid,
                        status=InvocationStatus.FAILED,
                        error=str(exc),
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )

                state.budget_usage.invocations += 1
                if worker_req.strategy == ExecutionStrategy.BOOST:
                    state.budget_usage.boost_invocations += 1

                # Success path
                if model_res.status == InvocationStatus.SUCCEEDED:
                    if hasattr(self.scheduler, "record_success"):
                        self.scheduler.record_success(current_lease.profile_name)

                    worker_res = WorkerResult(
                        worker_id=worker_req.worker_id,
                        role=worker_req.role,
                        status=InvocationStatus.SUCCEEDED,
                        invocation_id=iid,
                        response=model_res.response,
                        structured_data=model_res.structured_data,
                        conversation_id=model_res.conversation_id,
                        started_at=model_res.started_at,
                        completed_at=model_res.completed_at,
                    )
                    if hasattr(self.store, "record_invocation_completed"):
                        self.store.record_invocation_completed(state.run_id, iid, result=worker_res)
                    else:
                        self._emit_event(
                            EventType.INVOCATION_COMPLETED,
                            state.run_id,
                            {"invocation_id": str(iid), "worker_id": str(worker_req.worker_id)},
                        )
                    self.store.save_result(state.run_id, worker_res)
                    return worker_res

                # Failure classification
                failure_class = classify_failure(model_res)

                # Check if mechanical retry on another profile is warranted and permitted
                can_retry = (
                    failure_class == FailureClass.RETRYABLE
                    and state.budget_usage.retries < state.budget.max_retries
                )

                if can_retry and hasattr(self.scheduler, "allocate"):
                    state.budget_usage.retries += 1
                    excluded_profiles.add(current_lease.profile_name)
                    if hasattr(self.scheduler, "record_failure"):
                        self.scheduler.record_failure(current_lease.profile_name)

                    # Release old lease
                    self.lease_manager.release(current_lease.lease_id, run_id=state.run_id)
                    if state.run_id in self._active_leases and current_lease in self._active_leases[state.run_id]:
                        self._active_leases[state.run_id].remove(current_lease)
                    self._emit_event(
                        EventType.PROFILE_RELEASED,
                        state.run_id,
                        {
                            "lease_id": str(current_lease.lease_id),
                            "profile_name": current_lease.profile_name,
                            "worker_id": str(current_lease.worker_id),
                        },
                    )

                    # Attempt to allocate alternate profile
                    min_q = (
                        state.budget.min_quota_remaining / 100.0
                        if state.budget.min_quota_remaining > 1.0
                        else state.budget.min_quota_remaining
                    )
                    alt_leases = self.scheduler.allocate(
                        [worker_req],
                        run_id=state.run_id,
                        excluded_profiles=excluded_profiles,
                        min_quota=min_q,
                        raise_on_insufficient=False,
                    )
                    if alt_leases:
                        current_lease = alt_leases[0]
                        self._active_leases.setdefault(state.run_id, []).append(current_lease)
                        self._emit_event(
                            EventType.PROFILE_LEASED,
                            state.run_id,
                            {
                                "lease_id": str(current_lease.lease_id),
                                "profile_name": current_lease.profile_name,
                                "worker_id": str(current_lease.worker_id),
                                "run_id": str(state.run_id),
                            },
                        )
                        continue

                # Unrecoverable failure or retries exhausted
                worker_res = WorkerResult(
                    worker_id=worker_req.worker_id,
                    role=worker_req.role,
                    status=InvocationStatus.FAILED,
                    invocation_id=iid,
                    response=model_res.error or model_res.response,
                    structured_data=model_res.structured_data,
                    conversation_id=model_res.conversation_id,
                    failure=failure_class,
                    started_at=model_res.started_at,
                    completed_at=model_res.completed_at,
                )
                if hasattr(self.store, "record_invocation_failed"):
                    self.store.record_invocation_failed(
                        state.run_id,
                        iid,
                        error=model_res.error or model_res.response or "Worker execution failed",
                        failure=failure_class,
                    )
                else:
                    self._emit_event(
                        EventType.INVOCATION_FAILED,
                        state.run_id,
                        {
                            "invocation_id": str(iid),
                            "worker_id": str(worker_req.worker_id),
                            "error": model_res.error,
                        },
                    )
                self.store.save_result(state.run_id, worker_res)
                return worker_res
        finally:
            # Guarantees release in finally
            self.lease_manager.release(current_lease.lease_id, run_id=state.run_id)
            if state.run_id in self._active_leases and current_lease in self._active_leases[state.run_id]:
                self._active_leases[state.run_id].remove(current_lease)
            self._emit_event(
                EventType.PROFILE_RELEASED,
                state.run_id,
                {
                    "lease_id": str(current_lease.lease_id),
                    "profile_name": current_lease.profile_name,
                    "worker_id": str(current_lease.worker_id),
                },
            )

    async def _execute_single_auditor(
        self,
        audit_req: AuditRequest,
        lease: ProfileLease,
        state: RunState,
        repository_scope: str = "",
    ) -> AuditResult:
        """Execute a single auditor invocation with automatic lease release."""
        iid = InvocationId(f"inv-{audit_req.worker_id}-{uuid.uuid4().hex[:6]}")
        current_lease = lease

        # Resolve target worker outputs from prior store results
        all_results = self.store.get_results(state.run_id)
        target_results: list[WorkerResult] = []
        target_map = {
            str(r.worker_id): r
            for r in all_results
            if isinstance(r, WorkerResult) and r.status == InvocationStatus.SUCCEEDED
        }
        for tid in audit_req.target_worker_ids:
            if str(tid) in target_map:
                target_results.append(target_map[str(tid)])

        prompt = build_auditor_prompt(
            task=state.task,
            focus=audit_req.focus,
            repository_scope=repository_scope,
            target_results=target_results,
        )

        invocation = ModelInvocation(
            invocation_id=iid,
            run_id=state.run_id,
            worker_id=audit_req.worker_id,
            role=WorkerRole.AUDITOR,
            strategy=audit_req.strategy,
            workspace_mode=WorkspaceMode.READ_ONLY,
            prompt=prompt,
            timeout_seconds=audit_req.timeout_seconds,
        )

        if hasattr(self.store, "record_invocation_started"):
            try:
                self.store.record_invocation_started(state.run_id, invocation, invocation_id=iid)
            except Exception as exc:
                logger.warning("Failed to record auditor invocation started: %s", exc)
        else:
            self._emit_event(
                EventType.INVOCATION_STARTED,
                state.run_id,
                {
                    "invocation_id": str(iid),
                    "worker_id": str(audit_req.worker_id),
                    "role": WorkerRole.AUDITOR.value,
                },
            )

        try:
            started_at = datetime.now(timezone.utc).isoformat()
            try:
                if hasattr(self.runner, "run_async") and asyncio.iscoroutinefunction(self.runner.run_async):
                    model_res = await self.runner.run_async(invocation, profile_name=current_lease.profile_name)
                else:
                    model_res = await asyncio.to_thread(self.runner.run, invocation, current_lease.profile_name)
            except (KeyboardInterrupt, asyncio.CancelledError) as exc:
                raise asyncio.CancelledError("Interrupted by user (Ctrl+C)") from exc
            except Exception as exc:
                model_res = ModelResult(
                    invocation_id=iid,
                    status=InvocationStatus.FAILED,
                    error=str(exc),
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

            state.budget_usage.invocations += 1
            if audit_req.strategy == ExecutionStrategy.BOOST:
                state.budget_usage.boost_invocations += 1

            if model_res.status == InvocationStatus.SUCCEEDED:
                findings: list[str] = []
                if model_res.structured_data and isinstance(model_res.structured_data.get("findings"), list):
                    findings = [str(f) for f in model_res.structured_data["findings"]]
                elif model_res.response:
                    findings = []
                    for line in model_res.response.splitlines():
                        raw_line = line.strip()
                        if raw_line.startswith(("-", "*")):
                            clean = raw_line.lstrip("- *").strip()
                            if clean.lower().startswith("finding:"):
                                clean = clean[len("finding:"):].strip()
                            findings.append(clean)

                audit_res = AuditResult(
                    worker_id=audit_req.worker_id,
                    invocation_id=iid,
                    findings=findings,
                    response=model_res.response,
                    status=InvocationStatus.SUCCEEDED,
                    started_at=model_res.started_at,
                    completed_at=model_res.completed_at,
                )
                if hasattr(self.store, "record_invocation_completed"):
                    self.store.record_invocation_completed(state.run_id, iid, result=audit_res)
                else:
                    self._emit_event(
                        EventType.INVOCATION_COMPLETED,
                        state.run_id,
                        {"invocation_id": str(iid), "worker_id": str(audit_req.worker_id)},
                    )
                self.store.save_result(state.run_id, audit_res)
                return audit_res

            # Failure path
            failure_class = classify_failure(model_res)
            audit_res = AuditResult(
                worker_id=audit_req.worker_id,
                invocation_id=iid,
                findings=[],
                response=model_res.error or model_res.response,
                status=InvocationStatus.FAILED,
                failure=failure_class,
                started_at=model_res.started_at,
                completed_at=model_res.completed_at,
            )
            if hasattr(self.store, "record_invocation_failed"):
                self.store.record_invocation_failed(
                    state.run_id,
                    iid,
                    error=model_res.error or "Auditor invocation failed",
                    failure=failure_class,
                )
            else:
                self._emit_event(
                    EventType.INVOCATION_FAILED,
                    state.run_id,
                    {"invocation_id": str(iid), "worker_id": str(audit_req.worker_id)},
                )
            self.store.save_result(state.run_id, audit_res)
            return audit_res
        finally:
            self.lease_manager.release(current_lease.lease_id, run_id=state.run_id)
            if state.run_id in self._active_leases and current_lease in self._active_leases[state.run_id]:
                self._active_leases[state.run_id].remove(current_lease)
            self._emit_event(
                EventType.PROFILE_RELEASED,
                state.run_id,
                {
                    "lease_id": str(current_lease.lease_id),
                    "profile_name": current_lease.profile_name,
                    "worker_id": str(current_lease.worker_id),
                },
            )

    async def _execute_action_wave(
        self,
        action: CoordinatorAction,
        state: RunState,
        repository_scope: str = "",
    ) -> tuple[list[WorkerResult | AuditResult], list[WorkerResult | AuditResult], list[WorkerRequest | AuditRequest]]:
        """Dispatch parallel worker or auditor tasks and return results."""
        completed: list[WorkerResult | AuditResult] = []
        failed: list[WorkerResult | AuditResult] = []
        rejected: list[WorkerRequest | AuditRequest] = []

        # 1. Determine executable requests
        if action.kind in (ActionKind.RUN_WORKERS, ActionKind.RUN_SYNTHESIS, ActionKind.RUN_EXECUTOR):
            requests: Sequence[WorkerRequest | AuditRequest] = action.workers
        elif action.kind == ActionKind.RUN_AUDITORS:
            requests = action.auditors
        else:
            return completed, failed, rejected

        if not requests:
            return completed, failed, rejected

        # Executor safety check: before executor starts, ensure no other active tasks
        if action.kind == ActionKind.RUN_EXECUTOR:
            active_for_run = self._active_tasks.get(state.run_id, {})
            if active_for_run:
                raise EngineError("Executor safety violation: prior tasks still active in run")

        # 2. Transactionally allocate exclusive leases
        min_q = (
            state.budget.min_quota_remaining / 100.0
            if state.budget.min_quota_remaining > 1.0
            else state.budget.min_quota_remaining
        )
        leases = self.scheduler.allocate(
            requests,
            run_id=state.run_id,
            min_quota=min_q,
            raise_on_insufficient=True,
        )

        self._active_leases.setdefault(state.run_id, []).extend(leases)
        for lease in leases:
            self._emit_event(
                EventType.PROFILE_LEASED,
                state.run_id,
                {
                    "lease_id": str(lease.lease_id),
                    "profile_name": lease.profile_name,
                    "worker_id": str(lease.worker_id),
                    "run_id": str(state.run_id),
                },
            )

        lease_by_wid: dict[WorkerId, ProfileLease] = {lease.worker_id: lease for lease in leases}

        # 3. Create per-invocation tasks and register them
        tasks: list[asyncio.Task[Any]] = []
        self._active_tasks.setdefault(state.run_id, {})

        for req in requests:
            lease = lease_by_wid[req.worker_id]
            if isinstance(req, WorkerRequest):
                coro = self._execute_single_worker(req, lease, state, repository_scope)
            else:
                coro = self._execute_single_auditor(req, lease, state, repository_scope)

            t = asyncio.create_task(coro)
            inv_placeholder = InvocationId(f"task-{req.worker_id}")
            self._active_tasks[state.run_id][inv_placeholder] = t
            tasks.append(t)

        # 4. Gather all parallel wave tasks
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._active_tasks.pop(state.run_id, None)

        # 5. Process execution outcomes
        for idx, res in enumerate(results):
            if isinstance(res, (KeyboardInterrupt, asyncio.CancelledError)):
                raise res
            req = requests[idx]
            if isinstance(res, (WorkerResult, AuditResult)):
                if res.status == InvocationStatus.SUCCEEDED:
                    completed.append(res)
                else:
                    failed.append(res)
            elif isinstance(res, Exception):
                logger.error("Invocation task for worker %s raised unhandled exception: %s", req.worker_id, res)
                role = getattr(req, "role", WorkerRole.GENERAL)
                failed.append(
                    WorkerResult(
                        worker_id=req.worker_id,
                        role=role,
                        status=InvocationStatus.FAILED,
                        response=str(res),
                        failure=FailureClass.RETRYABLE,
                    )
                )

        return completed, failed, rejected

    # ========================================================================
    # Core Engine Loop & Public Execution API
    # ========================================================================

    async def run_async(
        self,
        task: str,
        mode: RunMode = RunMode.PLAN,
        budget: OrchestrationBudget | None = None,
        run_id: RunId | str | None = None,
        coordinator: CoordinatorClient | None = None,
        repository_scope: str = "",
        raise_on_interrupt: bool = False,
        raise_on_error: bool = False,
    ) -> RunState:
        """Execute an orchestration run asynchronously."""
        coord = coordinator or self.coordinator
        if coord is None:
            raise EngineError("No coordinator provided for run")

        rid = RunId(run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}")
        b = budget or self.default_budget or OrchestrationBudget()

        # Step 1: Create run atomically
        if hasattr(self.store, "create_run"):
            state = self.store.create_run(run_id=rid, task=task, mode=mode, budget=b)
        else:
            now = datetime.now(timezone.utc).isoformat()
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
            self.store.save_run(state)
            self._emit_event(EventType.RUN_CREATED, rid, {"task": task, "mode": mode.value})

        state.status = RunStatus.RUNNING
        self.store.save_run(state)

        known_worker_ids: set[WorkerId] = set()

        try:
            # Step 2: Query fleet capacity (do not independently query quota)
            fleet_view = self.scheduler.get_fleet_view()

            # Step 3: Start coordinator with fleet view only
            # Support both initial response tuple/InitialCoordinatorResponse and assess_task + decide_action
            action: CoordinatorAction | None = None

            if hasattr(coord, "start_run") and callable(coord.start_run):
                initial_res = coord.start_run(task, fleet_view)
            elif hasattr(coord, "assess_task") and callable(coord.assess_task):
                initial_res = coord.assess_task(task, fleet_view)
            else:
                initial_res = None

            assessment: TaskAssessment | None = None
            if isinstance(initial_res, InitialCoordinatorResponse):
                assessment = initial_res.assessment
                action = initial_res.action
            elif isinstance(initial_res, tuple) and len(initial_res) == 2:
                assessment, action = initial_res
            elif isinstance(initial_res, TaskAssessment):
                assessment = initial_res
            elif isinstance(initial_res, (str, dict)):
                try:
                    parsed_initial = parse_initial_response(initial_res, known_worker_ids=known_worker_ids)
                    assessment = parsed_initial.assessment
                    action = parsed_initial.action
                except Exception:
                    pass

            if assessment is None:
                assessment = TaskAssessment(
                    task_type=TaskType.GENERAL,
                    complexity=ComplexityLevel.MEDIUM,
                    confidence=1.0,
                    mutation_required=(mode == RunMode.IMPLEMENT),
                    repository_scope=repository_scope,
                )

            # Persist assessment
            state.assessment = assessment
            if hasattr(self.store, "save_assessment"):
                self.store.save_assessment(state.run_id, assessment)
            self._emit_event(EventType.TASK_ASSESSED, state.run_id, {"assessment": assessment.to_dict()})
            self.store.save_run(state)

            if hasattr(coord, "conversation_id") and getattr(coord, "conversation_id") is not None:
                state.coordinator_conversation_id = ConversationId(getattr(coord, "conversation_id"))

            observation: CoordinatorObservation | None = None

            # Step 11: Repeat adaptively (decision -> validate -> execute -> observation)
            while True:
                fleet_view = self.scheduler.get_fleet_view()

                if action is None:
                    self._emit_event(
                        EventType.ROUND_STARTED,
                        state.run_id,
                        {"round_number": state.round_number},
                    )
                    obs = observation or CoordinatorObservation(
                        fleet_view=fleet_view,
                        round_number=state.round_number,
                        budget_usage=state.budget_usage,
                    )
                    raw_action = coord.decide_action(obs, conversation_id=state.coordinator_conversation_id)
                    if isinstance(raw_action, CoordinatorAction):
                        action = raw_action
                    elif isinstance(raw_action, (str, dict)):
                        try:
                            action = parse_coordinator_action(raw_action, known_worker_ids=known_worker_ids)
                        except (ActionValidationError, ProtocolError, ValueError) as exc:
                            self._emit_event(
                                EventType.ACTION_REJECTED,
                                state.run_id,
                                {"action_id": "invalid_action", "reason": str(exc)},
                            )
                            fail_res = [
                                failure_to_worker_result(
                                    create_action_rejected_failure("invalid_action", str(exc))
                                )
                            ]
                            observation = self._build_observation(state, completed=[], failed=fail_res, rejected=[])
                            action = None
                            continue
                    else:
                        err_msg = f"Invalid coordinator action returned: {type(raw_action)}"
                        self._emit_event(
                            EventType.ACTION_REJECTED,
                            state.run_id,
                            {"action_id": "invalid_action", "reason": err_msg},
                        )
                        fail_res = [
                            failure_to_worker_result(
                                create_action_rejected_failure("invalid_action", err_msg)
                            )
                        ]
                        observation = self._build_observation(state, completed=[], failed=fail_res, rejected=[])
                        action = None
                        continue

                self._emit_event(
                    EventType.ACTION_REQUESTED,
                    state.run_id,
                    {"action": action.to_dict()},
                )

                # Step 12 & Step 17: Check FINALIZE
                if action.kind == ActionKind.FINALIZE:
                    self._emit_event(EventType.ACTION_ACCEPTED, state.run_id, {"action_id": str(action.action_id)})
                    state.status = RunStatus.COMPLETED
                    state.final_result = action.final_response
                    state.updated_at = datetime.now(timezone.utc).isoformat()
                    if hasattr(self.store, "finalize_run"):
                        try:
                            self.store.finalize_run(state.run_id, state.final_result or "", status=RunStatus.COMPLETED)
                        except Exception:
                            self.store.save_run(state)
                    else:
                        self.store.save_run(state)
                    self._emit_event(
                        EventType.RUN_COMPLETED,
                        state.run_id,
                        {"final_result": state.final_result},
                    )
                    return state

                # Step 4 & Step 5: Action & Budget Validation
                is_valid, reject_reason = self._validate_action(action, state, fleet_view, known_worker_ids)
                if not is_valid:
                    self._emit_event(
                        EventType.ACTION_REJECTED,
                        state.run_id,
                        {"action_id": str(action.action_id), "reason": reject_reason},
                    )
                    all_reqs = list(action.workers) + list(action.auditors)
                    fail_res = [
                        failure_to_worker_result(
                            create_action_rejected_failure(action.action_id, reject_reason or "Action rejected")
                        )
                    ]
                    observation = self._build_observation(state, completed=[], failed=fail_res, rejected=all_reqs)
                    action = None
                    continue

                # Action accepted
                self._emit_event(EventType.ACTION_ACCEPTED, state.run_id, {"action_id": str(action.action_id)})

                # Step 6 & 7: Allocate profiles and run wave
                try:
                    completed, failed, rejected = await self._execute_action_wave(action, state, repository_scope)
                except InsufficientCapacityError as exc:
                    # Scheduler could not satisfy allocation -> rejected action observation
                    self._emit_event(
                        EventType.ACTION_REJECTED,
                        state.run_id,
                        {"action_id": str(action.action_id), "reason": str(exc)},
                    )
                    all_reqs = list(action.workers) + list(action.auditors)
                    fail_res = [failure_to_worker_result(create_profile_unavailable_failure(r.worker_id)) for r in all_reqs]
                    observation = self._build_observation(state, completed=[], failed=fail_res, rejected=all_reqs)
                    action = None
                    continue

                for r in completed:
                    known_worker_ids.add(r.worker_id)

                state.round_number += 1
                state.budget_usage.rounds = state.round_number
                if state.created_at:
                    try:
                        cdt = datetime.fromisoformat(state.created_at)
                        if cdt.tzinfo is None:
                            cdt = cdt.replace(tzinfo=timezone.utc)
                        state.budget_usage.runtime_seconds = (datetime.now(timezone.utc) - cdt).total_seconds()
                    except Exception:
                        pass

                self.store.save_run(state)
                self._emit_event(
                    EventType.ROUND_COMPLETED,
                    state.run_id,
                    {"round_number": state.round_number - 1},
                )

                # Persist coordinator tracking info
                if hasattr(self.store, "save_coordinator_info"):
                    try:
                        from agym.orchestration.persistence import CoordinatorInfo
                        cinfo = CoordinatorInfo(
                            conversation_id=state.coordinator_conversation_id,
                            round_number=state.round_number,
                            last_accepted_action=action,
                            latest_observation=self._build_observation(
                                state, completed=completed, failed=failed, rejected=rejected
                            ),
                        )
                        self.store.save_coordinator_info(state.run_id, cinfo)
                    except Exception:
                        pass

                # Build observation for next decision
                observation = self._build_observation(state, completed=completed, failed=failed, rejected=rejected)
                action = None

        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            self._handle_interruption(state.run_id, state, reason=str(exc) or "Run interrupted (Ctrl+C)", coordinator=coord)
            if raise_on_interrupt:
                raise
            return state
        except Exception as exc:
            logger.exception("Run %s failed due to coordinator or engine exception: %s", state.run_id, exc)
            state.status = RunStatus.FAILED
            state.final_result = f"Failed: {exc}"
            state.updated_at = datetime.now(timezone.utc).isoformat()
            self.store.save_run(state)
            self._emit_event(EventType.RUN_FAILED, state.run_id, {"error": str(exc)})
            self._cleanup_leases(state.run_id)
            if raise_on_error:
                raise
            return state

    def run(
        self,
        task: str,
        mode: RunMode = RunMode.PLAN,
        budget: OrchestrationBudget | None = None,
        run_id: RunId | str | None = None,
        coordinator: CoordinatorClient | None = None,
        repository_scope: str = "",
        raise_on_interrupt: bool = False,
        raise_on_error: bool = False,
    ) -> RunState:
        """Synchronously execute an orchestration run."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        try:
            if loop and loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        asyncio.run,
                        self.run_async(
                            task,
                            mode=mode,
                            budget=budget,
                            run_id=run_id,
                            coordinator=coordinator,
                            repository_scope=repository_scope,
                            raise_on_interrupt=raise_on_interrupt,
                            raise_on_error=raise_on_error,
                        ),
                    )
                    return future.result()
            else:
                return asyncio.run(
                    self.run_async(
                        task,
                        mode=mode,
                        budget=budget,
                        run_id=run_id,
                        coordinator=coordinator,
                        repository_scope=repository_scope,
                        raise_on_interrupt=raise_on_interrupt,
                        raise_on_error=raise_on_error,
                    )
                )
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            state: RunState | None = None
            if run_id:
                state = self.store.get_run(RunId(run_id))
            if state is None and hasattr(self.store, "list_runs"):
                runs = self.store.list_runs(limit=1)
                if runs:
                    state = runs[0]
            if state is not None:
                self._handle_interruption(state.run_id, state, reason=str(exc) or "Run interrupted (Ctrl+C)", coordinator=coordinator)
                if raise_on_interrupt:
                    raise
                return state
            if raise_on_interrupt:
                raise
            return RunState(
                run_id=RunId(str(run_id or "interrupted")),
                task=task,
                status=RunStatus.INTERRUPTED,
            )

    # ========================================================================
    # Resume API
    # ========================================================================

    async def resume_async(
        self,
        run_id: RunId | str,
        coordinator: CoordinatorClient | None = None,
        raise_on_interrupt: bool = False,
        raise_on_error: bool = False,
    ) -> RunState:
        """Resume an interrupted or crashed run asynchronously."""
        rid = RunId(run_id)
        if hasattr(self.store, "load_run"):
            state = self.store.load_run(rid)
        else:
            state = self.store.get_run(rid)

        if state is None:
            raise EngineError(f"Cannot resume: run '{rid}' not found")

        if state.status == RunStatus.COMPLETED:
            return state

        coord = coordinator or self.coordinator
        if coord is None:
            raise EngineError(f"No coordinator available to resume run '{rid}'")

        # Step 16: Detect unfinished invocations, classify them, cleanup stale owned leases
        self._cleanup_leases(rid)

        # Inspect and classify unfinished invocations
        all_results = list(self.store.get_results(rid))
        known_worker_ids = {r.worker_id for r in all_results if r.status == InvocationStatus.SUCCEEDED}

        if hasattr(self.store, "run_dir"):
            inv_dir = self.store.run_dir(rid) / "invocations"
            if inv_dir.exists():
                for inv_file in inv_dir.glob("*.json"):
                    try:
                        with open(inv_file, "r", encoding="utf-8") as f:
                            idata = json.load(f)
                        st = idata.get("status")
                        active_at_int = idata.get("active_at_interruption", False)
                        iid = InvocationId(idata["invocation_id"])
                        wid = WorkerId(idata.get("worker_id", "unknown"))
                        role = WorkerRole(idata.get("role", WorkerRole.GENERAL))

                        if st in (InvocationStatus.RUNNING.value, InvocationStatus.PENDING.value) or active_at_int:
                            # Do not pretend an unfinished subprocess is still alive unless positively known
                            is_running = False
                            if hasattr(self.runner, "is_invocation_running"):
                                is_running = self.runner.is_invocation_running(rid, iid)

                            if not is_running:
                                failed_res = WorkerResult(
                                    worker_id=wid,
                                    role=role,
                                    status=InvocationStatus.FAILED,
                                    invocation_id=iid,
                                    response="Process terminated or interrupted before completion",
                                    failure=FailureClass.RETRYABLE,
                                )
                                if hasattr(self.store, "record_invocation_failed"):
                                    self.store.record_invocation_failed(
                                        rid,
                                        iid,
                                        error="Process terminated or interrupted before completion",
                                        failure=FailureClass.RETRYABLE,
                                    )
                                self.store.save_result(rid, failed_res)
                                all_results.append(failed_res)
                    except Exception as exc:
                        logger.warning("Error inspecting invocation %s during resume: %s", inv_file, exc)

        # Recover coordinator info
        if hasattr(self.store, "get_coordinator_info"):
            try:
                cinfo = self.store.get_coordinator_info(rid)
                if cinfo and cinfo.conversation_id:
                    state.coordinator_conversation_id = cinfo.conversation_id
            except Exception:
                pass

        # Reconstruct observation
        completed = [r for r in all_results if r.status == InvocationStatus.SUCCEEDED]
        failed = [r for r in all_results if r.status != InvocationStatus.SUCCEEDED]
        fleet_view = self.scheduler.get_fleet_view()

        state.status = RunStatus.RUNNING
        self.store.save_run(state)

        observation = self._build_observation(state, completed=completed, failed=failed, rejected=[])
        action: CoordinatorAction | None = None

        try:
            # Continue the loop from current observation
            while True:
                fleet_view = self.scheduler.get_fleet_view()

                if action is None:
                    self._emit_event(
                        EventType.ROUND_STARTED,
                        state.run_id,
                        {"round_number": state.round_number},
                    )
                    raw_action = coord.decide_action(observation, conversation_id=state.coordinator_conversation_id)
                    if isinstance(raw_action, CoordinatorAction):
                        action = raw_action
                    elif isinstance(raw_action, (str, dict)):
                        try:
                            action = parse_coordinator_action(raw_action, known_worker_ids=known_worker_ids)
                        except (ActionValidationError, ProtocolError, ValueError) as exc:
                            self._emit_event(
                                EventType.ACTION_REJECTED,
                                state.run_id,
                                {"action_id": "invalid_action", "reason": str(exc)},
                            )
                            fail_res = [
                                failure_to_worker_result(
                                    create_action_rejected_failure("invalid_action", str(exc))
                                )
                            ]
                            observation = self._build_observation(state, completed=[], failed=fail_res, rejected=[])
                            action = None
                            continue
                    else:
                        err_msg = f"Invalid coordinator action: {type(raw_action)}"
                        self._emit_event(
                            EventType.ACTION_REJECTED,
                            state.run_id,
                            {"action_id": "invalid_action", "reason": err_msg},
                        )
                        fail_res = [
                            failure_to_worker_result(
                                create_action_rejected_failure("invalid_action", err_msg)
                            )
                        ]
                        observation = self._build_observation(state, completed=[], failed=fail_res, rejected=[])
                        action = None
                        continue

                self._emit_event(
                    EventType.ACTION_REQUESTED,
                    state.run_id,
                    {"action": action.to_dict()},
                )

                if action.kind == ActionKind.FINALIZE:
                    self._emit_event(EventType.ACTION_ACCEPTED, state.run_id, {"action_id": str(action.action_id)})
                    state.status = RunStatus.COMPLETED
                    state.final_result = action.final_response
                    state.updated_at = datetime.now(timezone.utc).isoformat()
                    if hasattr(self.store, "finalize_run"):
                        try:
                            self.store.finalize_run(state.run_id, state.final_result or "", status=RunStatus.COMPLETED)
                        except Exception:
                            self.store.save_run(state)
                    else:
                        self.store.save_run(state)
                    self._emit_event(
                        EventType.RUN_COMPLETED,
                        state.run_id,
                        {"final_result": state.final_result},
                    )
                    return state

                is_valid, reject_reason = self._validate_action(action, state, fleet_view, known_worker_ids)
                if not is_valid:
                    self._emit_event(
                        EventType.ACTION_REJECTED,
                        state.run_id,
                        {"action_id": str(action.action_id), "reason": reject_reason},
                    )
                    all_reqs = list(action.workers) + list(action.auditors)
                    fail_res = [
                        failure_to_worker_result(
                            create_action_rejected_failure(action.action_id, reject_reason or "Action rejected")
                        )
                    ]
                    observation = self._build_observation(state, completed=[], failed=fail_res, rejected=all_reqs)
                    action = None
                    continue

                self._emit_event(EventType.ACTION_ACCEPTED, state.run_id, {"action_id": str(action.action_id)})

                try:
                    c_wave, f_wave, r_wave = await self._execute_action_wave(action, state)
                except InsufficientCapacityError as exc:
                    self._emit_event(
                        EventType.ACTION_REJECTED,
                        state.run_id,
                        {"action_id": str(action.action_id), "reason": str(exc)},
                    )
                    all_reqs = list(action.workers) + list(action.auditors)
                    fail_res = [failure_to_worker_result(create_profile_unavailable_failure(r.worker_id)) for r in all_reqs]
                    observation = self._build_observation(state, completed=[], failed=fail_res, rejected=all_reqs)
                    action = None
                    continue

                for r in c_wave:
                    known_worker_ids.add(r.worker_id)

                state.round_number += 1
                state.budget_usage.rounds = state.round_number
                self.store.save_run(state)
                self._emit_event(
                    EventType.ROUND_COMPLETED,
                    state.run_id,
                    {"round_number": state.round_number - 1},
                )

                observation = self._build_observation(state, completed=c_wave, failed=f_wave, rejected=r_wave)
                action = None

        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            self._handle_interruption(state.run_id, state, reason=str(exc) or "Run interrupted (Ctrl+C)", coordinator=coord)
            if raise_on_interrupt:
                raise
            return state
        except Exception as exc:
            logger.exception("Resume of run %s failed: %s", state.run_id, exc)
            state.status = RunStatus.FAILED
            state.final_result = f"Failed: {exc}"
            state.updated_at = datetime.now(timezone.utc).isoformat()
            self.store.save_run(state)
            self._emit_event(EventType.RUN_FAILED, state.run_id, {"error": str(exc)})
            self._cleanup_leases(state.run_id)
            if raise_on_error:
                raise
            return state

    def resume(
        self,
        run_id: RunId | str,
        coordinator: CoordinatorClient | None = None,
        raise_on_interrupt: bool = False,
        raise_on_error: bool = False,
    ) -> RunState:
        """Synchronously resume an interrupted or crashed run."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        try:
            if loop and loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        asyncio.run,
                        self.resume_async(
                            run_id,
                            coordinator=coordinator,
                            raise_on_interrupt=raise_on_interrupt,
                            raise_on_error=raise_on_error,
                        ),
                    )
                    return future.result()
            else:
                return asyncio.run(
                    self.resume_async(
                        run_id,
                        coordinator=coordinator,
                        raise_on_interrupt=raise_on_interrupt,
                        raise_on_error=raise_on_error,
                    )
                )
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            rid = RunId(run_id)
            state = self.store.get_run(rid)
            if state is not None:
                self._handle_interruption(rid, state, reason=str(exc) or "Run interrupted (Ctrl+C)", coordinator=coordinator)
                if raise_on_interrupt:
                    raise
                return state
            if raise_on_interrupt:
                raise
            return RunState(
                run_id=rid,
                task="",
                status=RunStatus.INTERRUPTED,
            )

    # ========================================================================
    # Dry Run API
    # ========================================================================

    async def dry_run_async(
        self,
        task: str,
        mode: RunMode = RunMode.PLAN,
        budget: OrchestrationBudget | None = None,
        coordinator: CoordinatorClient | None = None,
        repository_scope: str = "",
    ) -> DryRunPlan:
        """Inspect fleet, assess task, obtain and validate first action without worker dispatch."""
        coord = coordinator or self.coordinator
        if coord is None:
            raise EngineError("No coordinator provided for dry-run")

        rid = RunId(f"dry-run-{uuid.uuid4().hex[:8]}")
        b = budget or self.default_budget or OrchestrationBudget()
        state = RunState(
            run_id=rid,
            task=task,
            mode=mode,
            budget=b,
            budget_usage=BudgetUsage(),
        )

        # 1. Inspect fleet
        fleet_view = self.scheduler.get_fleet_view()

        # 2. Run assessment
        assessment: TaskAssessment | None = None
        action: CoordinatorAction | None = None

        if hasattr(coord, "start_run") and callable(coord.start_run):
            initial_res = coord.start_run(task, fleet_view)
        elif hasattr(coord, "assess_task") and callable(coord.assess_task):
            initial_res = coord.assess_task(task, fleet_view)
        else:
            initial_res = None

        if isinstance(initial_res, InitialCoordinatorResponse):
            assessment = initial_res.assessment
            action = initial_res.action
        elif isinstance(initial_res, tuple) and len(initial_res) == 2:
            assessment, action = initial_res
        elif isinstance(initial_res, TaskAssessment):
            assessment = initial_res
        elif isinstance(initial_res, (str, dict)):
            try:
                parsed = parse_initial_response(initial_res, known_worker_ids=set())
                assessment = parsed.assessment
                action = parsed.action
            except Exception:
                pass

        if assessment is None:
            assessment = TaskAssessment(
                task_type=TaskType.GENERAL,
                complexity=ComplexityLevel.MEDIUM,
                confidence=1.0,
                mutation_required=(mode == RunMode.IMPLEMENT),
                repository_scope=repository_scope,
            )

        state.assessment = assessment

        # 3. Obtain first action if not already returned
        if action is None:
            init_obs = CoordinatorObservation(
                fleet_view=fleet_view,
                round_number=0,
                budget_usage=state.budget_usage,
            )
            raw_action = coord.decide_action(init_obs)
            if isinstance(raw_action, CoordinatorAction):
                action = raw_action
            elif isinstance(raw_action, (str, dict)):
                action = parse_coordinator_action(raw_action, known_worker_ids=set())
            else:
                raise ActionValidationError(f"Invalid coordinator action: {type(raw_action)}")

        # 4. Validate first action
        is_valid, error = self._validate_action(action, state, fleet_view, known_worker_ids=set())

        # 5. Extract plan without invoking requested workers
        planned_workers = [w.to_dict() for w in action.workers]
        planned_auditors = [a.to_dict() for a in action.auditors]

        return DryRunPlan(
            run_id=rid,
            task=task,
            mode=mode,
            assessment=assessment,
            action=action,
            is_valid=is_valid,
            validation_error=error,
            fleet_view=fleet_view,
            budget=b,
            planned_workers=planned_workers,
            planned_auditors=planned_auditors,
        )

    def dry_run(
        self,
        task: str,
        mode: RunMode = RunMode.PLAN,
        budget: OrchestrationBudget | None = None,
        coordinator: CoordinatorClient | None = None,
        repository_scope: str = "",
    ) -> DryRunPlan:
        """Synchronously execute dry-run planning."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run,
                    self.dry_run_async(
                        task,
                        mode=mode,
                        budget=budget,
                        coordinator=coordinator,
                        repository_scope=repository_scope,
                    ),
                )
                return future.result()
        else:
            return asyncio.run(
                self.dry_run_async(
                    task,
                    mode=mode,
                    budget=budget,
                    coordinator=coordinator,
                    repository_scope=repository_scope,
                )
            )
