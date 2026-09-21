"""Durable Staged Execution Engine & Release Barriers for AGYM Council.

Implements Milestone 4 Features:
- Feature 24: Ordered Stage Loop Execution (Sequential stages with concurrent worker execution)
- Feature 25: Atomic Stage Release Barrier (Transactional commit releasing artifacts (`released=1`) only when stage completes)
- Feature 26: Output Section Enforcement (Validate required JSON sections: findings, evidence, uncertainties, action)
- Feature 27: Strict Dissent Preservation (Pass critique objections to synthesizer; mandate "Do not invent consensus")
- Feature 28: Zero-Tolerance Failure Policy (Mark run NEEDS_ATTENTION on required worker failure; never force consensus with missing contributors)
- Feature 31: Run & Attempt Lifecycle State Machine (Full transitions: READY, RUNNING, PAUSING, PAUSED, NEEDS_ATTENTION, COMPLETED, CANCELLED)
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from agym.council.artifacts import (
    ArtifactStore,
    store_artifact as cas_store_artifact,
)
from agym.council.context import (
    assemble_prompt,
    parse_council_sections,
    setup_attempt_workspace,
)
from agym.council.models import (
    ArtifactRef,
    Attempt,
    AttemptStatus,
    CouncilOutputSections,
    Run,
    RunStatus,
    StageConfig,
    StageContextPolicy,
    StageKind,
    StageStatus,
    TurnAllowance,
    TurnEvent,
    TurnRequest,
    TurnResult,
    TurnStatus,
    WorkerConfig,
    WorkerStatus,
    WorkflowConfig,
)
from agym.council.providers.antigravity import get_process_start_time
from agym.council.providers.base import ProviderAdapter
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.scheduler import CapacityScheduler, claim_attempt_atomic
from agym.council.storage import (
    create_attempt,
    get_attempt,
    get_connection,
    get_run,
    get_stage,
    get_stages_for_run,
    get_workers_for_run,
    list_artifacts_for_run,
    list_released_artifacts_for_stages,
    record_event,
    record_issue,
    release_stage_artifacts_atomic,
    update_attempt,
    update_run_status,
    update_stage_status,
    update_worker_status,
)
from agym.profiles import _default_data_root


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StageResult:
    """Outcome of running a single workflow stage."""

    stage_id: str
    status: StageStatus
    worker_results: dict[str, TurnResult] = field(default_factory=dict)
    released_artifacts: list[ArtifactRef] = field(default_factory=list)
    error_message: str | None = None


@dataclass
class RunResult:
    """Outcome of executing an entire workflow run."""

    run_id: str
    status: RunStatus
    stage_results: list[StageResult] = field(default_factory=list)
    final_deliverable_artifact_id: str | None = None
    total_model_calls: int = 0
    total_wall_clock_seconds: float = 0.0
    error_message: str | None = None


class CouncilEngine:
    """Durable workflow execution engine for AGYM Council.

    Coordinates staged pipeline progression, transactional attempt claiming,
    capacity-scheduled worker execution, output section validation, atomic
    release barriers, and strict dissent preservation.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        artifact_store: ArtifactStore | None = None,
        provider: ProviderAdapter | None = None,
        scheduler: CapacityScheduler | None = None,
        data_root: Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.artifact_store = artifact_store or ArtifactStore()
        self.provider = provider or FakeProviderAdapter()
        self.scheduler = scheduler or CapacityScheduler(global_limit=4, per_account_limit=1)
        self.data_root = Path(data_root).resolve() if data_root else _default_data_root()

        # In-memory tracking for cancellation / pause signaling
        self._active_runs: dict[str, bool] = {}  # run_id -> is_running
        self._cancelled_runs: set[str] = set()
        self._paused_runs: set[str] = set()
        self._active_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._active_attempts: dict[str, set[str]] = {}

    def _get_conn(self) -> sqlite3.Connection:
        """Obtain an SQLite connection configured with Council pragmas."""
        return get_connection(self.db_path)

    def log_event(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        level: str,
        message: str,
        stage_id: str | None = None,
        worker_id: str | None = None,
        attempt_id: str | None = None,
        details: Any = None,
    ) -> None:
        """Record a durable, leveled structured log event for real-time streaming."""
        payload: dict[str, Any] = {
            "level": level.upper(),
            "message": message,
            "timestamp": _utc_now_iso(),
        }
        if details is not None:
            payload["details"] = details
        if stage_id:
            payload["stage_id"] = stage_id
        if worker_id:
            payload["worker_id"] = worker_id
        record_event(
            conn,
            run_id=run_id,
            stage_id=stage_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
            event_type="run.log",
            payload=payload,
        )

    def store_artifact(
        self,
        run_id: str,
        name: str,
        data: bytes,
        stage_id: str | None = None,
        worker_id: str | None = None,
        released: bool = False,
        attempt_id: str | None = None,
        mime_type: str = "text/plain",
    ) -> ArtifactRef:
        """Store bytes in CAS and persist the artifact record to SQLite."""
        conn = self._get_conn()
        try:
            return cas_store_artifact(
                run_id=run_id,
                name=name,
                data=data,
                stage_id=stage_id,
                worker_id=worker_id,
                released=released,
                attempt_id=attempt_id,
                artifact_store=self.artifact_store,
                conn=conn,
                mime_type=mime_type,
            )
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Run Lifecycle Management
    # -----------------------------------------------------------------------

    def pause_run(self, run_id: str) -> None:
        """Signal a running workflow to pause after in-flight turns complete."""
        self._paused_runs.add(run_id)
        conn = self._get_conn()
        try:
            active_tasks = self._active_tasks.get(run_id)
            if active_tasks and any(not t.done() for t in active_tasks):
                update_run_status(conn, run_id, status=RunStatus.PAUSING)
                record_event(conn, run_id, event_type="run.pausing", payload={"run_id": run_id})
            else:
                update_run_status(conn, run_id, status=RunStatus.PAUSED)
                record_event(conn, run_id, event_type="run.paused", payload={"run_id": run_id})
        finally:
            conn.close()

    async def resume_run(self, run_id: str) -> RunResult:
        """Resume a paused workflow run."""
        self._paused_runs.discard(run_id)
        conn = self._get_conn()
        try:
            update_run_status(conn, run_id, status=RunStatus.RUNNING)
            record_event(conn, run_id, event_type="run.resumed", payload={"run_id": run_id})
        finally:
            conn.close()
        return await self.execute_run(run_id)

    def resume_run_sync(self, run_id: str) -> RunResult:
        """Synchronous wrapper for resume_run."""
        return asyncio.run(self.resume_run(run_id))

    def cancel_run(self, run_id: str) -> None:
        """Cancel an in-flight workflow run and terminate active child processes."""
        self._cancelled_runs.add(run_id)

        # Cancel active asyncio tasks for this run
        tasks = list(self._active_tasks.get(run_id, set()))
        for t in tasks:
            if not t.done():
                t.cancel()

        # Signal cancellation to active attempts via provider
        attempt_ids = list(self._active_attempts.get(run_id, set()))
        for att_id in attempt_ids:
            if hasattr(self.provider, "cancel"):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.provider.cancel(att_id))
                except RuntimeError:
                    pass

        conn = self._get_conn()
        try:
            # Mark active attempts cancelled
            active_attempts = conn.execute(
                "SELECT attempt_id FROM attempts WHERE run_id = ? AND status IN ('CLAIMED', 'DISPATCHED', 'RUNNING')",
                (run_id,),
            ).fetchall()
            for row in active_attempts:
                att_id = row["attempt_id"]
                update_attempt(conn, att_id, status=AttemptStatus.CANCELLED.value, error_details="Run cancelled by user")

            # Reset active workers back to IDLE
            now_iso = _utc_now_iso()
            conn.execute(
                """
                UPDATE workers
                SET status = 'IDLE', updated_at = ?
                WHERE run_id = ? AND status IN ('RUNNING', 'ASSIGNED')
                """,
                (now_iso, run_id),
            )

            update_run_status(conn, run_id, status=RunStatus.CANCELLED, error_message="Run cancelled by user")
            record_event(conn, run_id, event_type="run.cancelled", payload={"run_id": run_id})
        finally:
            conn.close()

    def resolve_run(
        self,
        run_id: str,
        action: str = "retry",
        resolution_note: str | None = None,
    ) -> None:
        """Resolve a run in NEEDS_ATTENTION state back to READY for continuation."""
        conn = self._get_conn()
        try:
            run_row = get_run(conn, run_id)
            if not run_row or run_row["status"] != RunStatus.NEEDS_ATTENTION.value:
                raise ValueError(f"Run '{run_id}' is not in NEEDS_ATTENTION state")

            now_iso = _utc_now_iso()
            # Resolve any unresolved issues
            conn.execute(
                """
                UPDATE issues
                SET resolved = 1, resolution_note = ?, resolved_at = ?
                WHERE run_id = ? AND resolved = 0
                """,
                (resolution_note or f"Resolved via {action}", now_iso, run_id),
            )

            # Reset failed stages back to PENDING
            conn.execute(
                """
                UPDATE stages
                SET status = 'PENDING', error_message = NULL, updated_at = ?
                WHERE run_id = ? AND status = 'NEEDS_ATTENTION'
                """,
                (now_iso, run_id),
            )

            # Reset failed workers back to IDLE
            conn.execute(
                """
                UPDATE workers
                SET status = 'IDLE', updated_at = ?
                WHERE run_id = ? AND status IN ('FAILED', 'RUNNING')
                """,
                (now_iso, run_id),
            )

            update_run_status(conn, run_id, status=RunStatus.READY, error_message=None)
            record_event(
                conn,
                run_id,
                event_type="run.resolved",
                payload={"action": action, "note": resolution_note},
            )
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Staged Pipeline Execution
    # -----------------------------------------------------------------------

    async def execute_run(self, run_id: str) -> RunResult:
        """Execute a complete workflow run through its ordered stages.

        Enforces Invariants:
        1. Sequential stage ordering: Stage N must complete before Stage N+1 begins.
        2. Zero-tolerance failure policy: If any required worker fails, execution halts
           and the run transitions to NEEDS_ATTENTION.
        3. Budget enforcement: Model call and wall-clock time limits are monitored.
        """
        conn = self._get_conn()
        try:
            run_row = get_run(conn, run_id)
            if not run_row:
                raise ValueError(f"Run '{run_id}' not found")

            # Handle status checks
            current_status = RunStatus(run_row["status"])
            if current_status == RunStatus.COMPLETED:
                return RunResult(
                    run_id=run_id,
                    status=RunStatus.COMPLETED,
                    final_deliverable_artifact_id=run_row["final_deliverable_artifact_id"],
                    total_model_calls=run_row["total_model_calls"],
                    total_wall_clock_seconds=run_row["total_wall_clock_seconds"],
                )
            if current_status == RunStatus.CANCELLED:
                return RunResult(run_id=run_id, status=RunStatus.CANCELLED, error_message="Run is cancelled")
            if current_status == RunStatus.NEEDS_ATTENTION:
                return RunResult(
                    run_id=run_id,
                    status=RunStatus.NEEDS_ATTENTION,
                    error_message=run_row["error_message"] or "Run requires attention",
                )

            # Transition READY/DRAFT to RUNNING
            now_iso = _utc_now_iso()
            update_run_status(conn, run_id, status=RunStatus.RUNNING)
            record_event(conn, run_id, event_type="run.started", payload={"run_id": run_id})

            workflow_config = WorkflowConfig.model_validate(json.loads(run_row["config_json"]))
            stage_rows = get_stages_for_run(conn, run_id)

            self.log_event(conn, run_id, "INFO", f"Council run '{run_id}' ({workflow_config.name}) started. Goal: {workflow_config.goal}")
            self.log_event(conn, run_id, "INFO", f"Configured {len(stage_rows)} stage(s) with {len(workflow_config.workers)} worker(s). Provider: {self.provider.__class__.__name__}")

            stage_results: list[StageResult] = []
            start_wall_time = time.time()
            total_calls = run_row["total_model_calls"]

            for stage_row in stage_rows:
                stage_id = stage_row["stage_id"]
                stage_status = StageStatus(stage_row["status"])

                # If stage already completed in prior partial run, collect its result and advance
                if stage_status == StageStatus.COMPLETED:
                    rel_arts = list_artifacts_for_run(conn, run_id, stage_id=stage_id, released_only=True)
                    stage_results.append(
                        StageResult(
                            stage_id=stage_id,
                            status=StageStatus.COMPLETED,
                            released_artifacts=[ArtifactRef.model_validate(dict(r)) for r in rel_arts],
                        )
                    )
                    continue

                # Check pause / cancellation before launching stage
                if run_id in self._cancelled_runs:
                    update_run_status(conn, run_id, status=RunStatus.CANCELLED, error_message="Run cancelled")
                    return RunResult(run_id=run_id, status=RunStatus.CANCELLED, stage_results=stage_results)

                if run_id in self._paused_runs:
                    update_run_status(conn, run_id, status=RunStatus.PAUSED)
                    return RunResult(run_id=run_id, status=RunStatus.PAUSED, stage_results=stage_results)

                # Check budget limits before launching next stage (model calls and wall clock)
                elapsed_wall = time.time() - start_wall_time
                if elapsed_wall >= workflow_config.limits.max_wall_seconds:
                    msg = f"Budget exhausted: reached maximum wall clock time limit ({workflow_config.limits.max_wall_seconds}s)"
                    update_run_status(
                        conn,
                        run_id,
                        status=RunStatus.BUDGET_EXHAUSTED,
                        error_message=msg,
                        total_model_calls=total_calls,
                        total_wall_clock_seconds=elapsed_wall,
                    )
                    record_event(conn, run_id, event_type="budget.exhausted", payload={"wall_seconds": elapsed_wall})
                    record_issue(
                        conn,
                        run_id=run_id,
                        stage_id=stage_id,
                        severity="error",
                        category="budget_exhausted",
                        message=msg,
                    )
                    return RunResult(
                        run_id=run_id,
                        status=RunStatus.BUDGET_EXHAUSTED,
                        stage_results=stage_results,
                        total_model_calls=total_calls,
                        total_wall_clock_seconds=elapsed_wall,
                        error_message=msg,
                    )

                if total_calls >= workflow_config.limits.max_model_calls:
                    msg = f"Budget exhausted: reached maximum model calls limit ({workflow_config.limits.max_model_calls})"
                    update_run_status(conn, run_id, status=RunStatus.BUDGET_EXHAUSTED, error_message=msg)
                    record_event(conn, run_id, event_type="budget.exhausted", payload={"calls": total_calls})
                    record_issue(
                        conn,
                        run_id=run_id,
                        stage_id=stage_id,
                        severity="error",
                        category="budget_exhausted",
                        message=msg,
                    )
                    return RunResult(
                        run_id=run_id,
                        status=RunStatus.BUDGET_EXHAUSTED,
                        stage_results=stage_results,
                        total_model_calls=total_calls,
                        total_wall_clock_seconds=elapsed_wall,
                        error_message=msg,
                    )

                # Execute the stage (Atomic Release Barrier enforced within run_stage)
                update_run_status(conn, run_id, current_stage_id=stage_id)
                stage_res = await self.run_stage(run_id, stage_id)
                stage_results.append(stage_res)

                # Count actual model calls in DB for this run
                attempt_cnt = conn.execute(
                    "SELECT COUNT(*) FROM attempts WHERE run_id = ?", (run_id,)
                ).fetchone()
                total_calls = attempt_cnt[0] if attempt_cnt else total_calls + len(stage_res.worker_results)

                # Check stage outcome
                if stage_res.status != StageStatus.COMPLETED:
                    # Invariant 28: Zero-tolerance failure policy!
                    # Required worker failure transitions run to NEEDS_ATTENTION;
                    # downstream synthesis is NEVER forced with missing contributors.
                    err_msg = stage_res.error_message or f"Stage '{stage_id}' failed to complete required workers"
                    update_run_status(
                        conn,
                        run_id,
                        status=RunStatus.NEEDS_ATTENTION,
                        error_message=err_msg,
                        total_model_calls=total_calls,
                        total_wall_clock_seconds=time.time() - start_wall_time,
                    )
                    record_event(
                        conn,
                        run_id,
                        stage_id=stage_id,
                        event_type="run.needs_attention",
                        payload={"error": err_msg},
                    )
                    self.log_event(conn, run_id, "ERROR", f"⚠ Council run halted in stage '{stage_id}': {err_msg}", stage_id=stage_id)
                    return RunResult(
                        run_id=run_id,
                        status=RunStatus.NEEDS_ATTENTION,
                        stage_results=stage_results,
                        total_model_calls=total_calls,
                        total_wall_clock_seconds=time.time() - start_wall_time,
                        error_message=err_msg,
                    )

            # All stages completed successfully!
            elapsed_seconds = time.time() - start_wall_time
            final_stage_id = workflow_config.final_stage

            # Retrieve deliverable artifact from the final stage
            final_artifacts = list_artifacts_for_run(conn, run_id, stage_id=final_stage_id, released_only=True)
            final_artifact_id = final_artifacts[-1]["artifact_id"] if final_artifacts else None

            update_run_status(
                conn,
                run_id,
                status=RunStatus.COMPLETED,
                final_deliverable_artifact_id=final_artifact_id,
                total_model_calls=total_calls,
                total_wall_clock_seconds=elapsed_seconds,
            )
            record_event(
                conn,
                run_id,
                event_type="run.completed",
                payload={"final_stage_id": final_stage_id, "final_artifact_id": final_artifact_id},
            )
            self.log_event(conn, run_id, "SUCCESS", f"★ Council run completed successfully in {elapsed_seconds:.2f}s! Final deliverable: {final_artifact_id}")

            return RunResult(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                stage_results=stage_results,
                final_deliverable_artifact_id=final_artifact_id,
                total_model_calls=total_calls,
                total_wall_clock_seconds=elapsed_seconds,
            )
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Stage Runner & Atomic Release Barrier
    # -----------------------------------------------------------------------

    async def run_stage(self, run_id: str, stage_id: str) -> StageResult:
        """Run a single stage with concurrent workers bounded by capacity scheduler.

        Enforces Invariant 25 (Atomic Stage Release Barrier):
        - All draft outputs remain physically unreleased (`released = 0`) until all
          required workers succeed.
        - Only upon complete stage success does a single SQLite transaction commit
          `release_stage_artifacts_atomic()`, releasing artifacts (`released = 1`)
          and setting stage status to `COMPLETED`.
        """
        conn = self._get_conn()
        try:
            run_row = get_run(conn, run_id)
            if not run_row:
                raise ValueError(f"Run '{run_id}' not found")

            stage_row = get_stage(conn, run_id, stage_id)
            if not stage_row:
                raise ValueError(f"Stage '{stage_id}' not found in run '{run_id}'")

            workflow_config = WorkflowConfig.model_validate(json.loads(run_row["config_json"]))
            stage_config = next(s for s in workflow_config.stages if s.id == stage_id)
            worker_map = {w.id: w for w in workflow_config.workers}

            # Update stage status to RUNNING
            update_stage_status(conn, run_id, stage_id, status=StageStatus.RUNNING.value)
            record_event(conn, run_id, stage_id=stage_id, event_type="stage.started", payload={"stage_id": stage_id})
            self.log_event(conn, run_id, "INFO", f"▶ Starting Stage '{stage_id}' ({stage_config.kind.value.upper()}) with worker(s): {', '.join(stage_config.workers)}", stage_id=stage_id)

            # Fetch ONLY released prerequisite artifacts from earlier completed input stages
            released_rows = list_released_artifacts_for_stages(conn, run_id, stage_config.input_stages)
            released_artifacts = [ArtifactRef.model_validate(dict(r)) for r in released_rows]
            self.log_event(conn, run_id, "DEBUG", f"Stage '{stage_id}': inputs from {stage_config.input_stages or 'initial'}. Loaded {len(released_artifacts)} released prerequisite artifact(s).", stage_id=stage_id)

            # Collect prior stage outputs for prompt context
            prior_outputs = self._collect_prior_stage_outputs(conn, run_id, stage_config.input_stages)

            # Check for dissent preservation
            # If any prior stage recorded dissent, or if current stage kind is SYNTHESIZE
            has_dissent = bool(run_row["dissent_recorded"]) or (stage_config.kind == StageKind.SYNTHESIZE)

            # Prepare attempt objects for each worker in this stage
            worker_tasks: list[asyncio.Task[tuple[str, TurnResult]]] = []
            for worker_id in stage_config.workers:
                worker = worker_map[worker_id]
                worker_tasks.append(
                    asyncio.create_task(
                        self._execute_worker_turn(
                            run_id=run_id,
                            stage_config=stage_config,
                            worker=worker,
                            workflow_config=workflow_config,
                            released_artifacts=released_artifacts,
                            prior_outputs=prior_outputs,
                            has_dissent=has_dissent,
                        )
                    )
                )

            # Await concurrent worker turns (concurrency is bounded by CapacityScheduler)
            worker_results_list = await asyncio.gather(*worker_tasks, return_exceptions=False)
            worker_results: dict[str, TurnResult] = {wid: res for wid, res in worker_results_list}

            # Evaluate success across all required workers in this stage
            all_succeeded = all(res.status == TurnStatus.SUCCESS for res in worker_results.values())

            if all_succeeded:
                # -------------------------------------------------------------------
                # ATOMIC STAGE RELEASE BARRIER
                # Commits stage completion and sets released=1 in one SQLite transaction
                # -------------------------------------------------------------------
                release_stage_artifacts_atomic(conn, run_id, stage_id)

                newly_released_rows = list_artifacts_for_run(conn, run_id, stage_id=stage_id, released_only=True)
                newly_released = [ArtifactRef.model_validate(dict(r)) for r in newly_released_rows]
                self.log_event(conn, run_id, "SUCCESS", f"✔ Stage '{stage_id}' completed. Atomic release barrier published {len(newly_released)} artifact(s) downstream.", stage_id=stage_id)

                return StageResult(
                    stage_id=stage_id,
                    status=StageStatus.COMPLETED,
                    worker_results=worker_results,
                    released_artifacts=newly_released,
                )
            else:
                # -------------------------------------------------------------------
                # ZERO-TOLERANCE FAILURE POLICY
                # At least one required worker failed: do NOT release artifacts!
                # -------------------------------------------------------------------
                failed_workers = [wid for wid, res in worker_results.items() if res.status != TurnStatus.SUCCESS]
                first_failed_res = worker_results[failed_workers[0]]
                err_msg = (
                    f"Required worker(s) {failed_workers} failed in stage '{stage_id}': "
                    f"{first_failed_res.error_message or first_failed_res.status.value}"
                )

                update_stage_status(
                    conn,
                    run_id,
                    stage_id,
                    status=StageStatus.NEEDS_ATTENTION.value,
                    error_message=err_msg,
                )
                record_event(
                    conn,
                    run_id,
                    stage_id=stage_id,
                    event_type="stage.failed",
                    payload={"failed_workers": failed_workers, "error": err_msg},
                )
                record_issue(
                    conn,
                    run_id=run_id,
                    stage_id=stage_id,
                    severity="error",
                    category="stage_failure",
                    message=err_msg,
                    details={"failed_workers": failed_workers},
                )

                return StageResult(
                    stage_id=stage_id,
                    status=StageStatus.NEEDS_ATTENTION,
                    worker_results=worker_results,
                    error_message=err_msg,
                )
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Worker Turn Execution & Attempt State Machine
    # -----------------------------------------------------------------------

    async def _execute_worker_turn(
        self,
        run_id: str,
        stage_config: StageConfig,
        worker: WorkerConfig,
        workflow_config: WorkflowConfig,
        released_artifacts: Sequence[ArtifactRef],
        prior_outputs: Sequence[dict[str, Any]],
        has_dissent: bool,
    ) -> tuple[str, TurnResult]:
        """Execute a worker turn with retry capability, scratch staging, and section checks."""
        conn = self._get_conn()
        curr_task = asyncio.current_task()
        if curr_task:
            self._active_tasks.setdefault(run_id, set()).add(curr_task)
        try:
            max_retries = workflow_config.limits.max_retries_per_task
            existing_cnt = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id = ? AND stage_id = ? AND worker_id = ?",
                (run_id, stage_config.id, worker.id),
            ).fetchone()[0]
            attempt_number = existing_cnt + 1
            last_result: TurnResult | None = None

            while attempt_number <= existing_cnt + max_retries + 1:
                # Create QUEUED attempt record
                attempt_id = str(uuid.uuid4())
                attempt = Attempt(
                    id=attempt_id,
                    run_id=run_id,
                    stage_id=stage_config.id,
                    worker_id=worker.id,
                    attempt_number=attempt_number,
                    account_ref=worker.account_ref,
                    model=worker.model,
                    status=AttemptStatus.QUEUED,
                )
                create_attempt(conn, attempt)
                self._active_attempts.setdefault(run_id, set()).add(attempt_id)

                try:
                    # Acquire dual lease (account serialization + global concurrency)
                    async with self.scheduler.acquire_lease(worker.account_ref):
                        # Atomically claim the attempt via SQLite BEGIN IMMEDIATE
                        claimed = claim_attempt_atomic(conn, attempt_id)
                        if not claimed:
                            # Attempt was already claimed or state modified
                            row = get_attempt(conn, attempt_id)
                            last_result = TurnResult(
                                attempt_id=attempt_id,
                                status=TurnStatus.CANCELLED,
                                error_message="Attempt claim failed",
                            )
                            return worker.id, last_result

                        # Setup isolated scratch workspace with ONLY released inputs
                        workspace_dir = setup_attempt_workspace(
                            run_id=run_id,
                            stage_id=stage_config.id,
                            worker_id=worker.id,
                            attempt_id=attempt_id,
                            released_artifacts=released_artifacts,
                            artifact_store=self.artifact_store,
                            base_dir=self.data_root,
                            allowed_input_stages=stage_config.input_stages,
                        )

                        # Assemble strictly hierarchical prompt
                        prompt_text = assemble_prompt(
                            goal=workflow_config.goal,
                            stage=stage_config,
                            worker=worker,
                            workflow=workflow_config,
                            inputs=[inp.model_dump() for inp in workflow_config.inputs],
                            execution_mode=workflow_config.execution_mode,
                            released_artifacts=released_artifacts,
                            prior_stage_outputs=prior_outputs,
                            has_dissent=has_dissent,
                        )

                        # Store prompt artifact in CAS (unreleased draft)
                        prompt_art = self.store_artifact(
                            run_id=run_id,
                            name=f"prompt_{stage_config.id}_{worker.id}.txt",
                            data=prompt_text.encode("utf-8"),
                            stage_id=stage_config.id,
                            worker_id=worker.id,
                            attempt_id=attempt_id,
                            released=False,
                        )

                        # Transition attempt to RUNNING and record OS process identity
                        now_iso = _utc_now_iso()
                        start_epoch = get_process_start_time(os.getpid()) or time.time()
                        update_attempt(
                            conn,
                            attempt_id=attempt_id,
                            status=AttemptStatus.RUNNING.value,
                            workspace_dir=str(workspace_dir),
                            rendered_prompt=prompt_text,
                            prompt_artifact_id=prompt_art.id,
                            started_at=now_iso,
                            pid=os.getpid(),
                            process_start_time=start_epoch,
                        )
                        update_worker_status(conn, run_id, worker.id, status=WorkerStatus.RUNNING.value)
                        record_event(
                            conn,
                            run_id=run_id,
                            stage_id=stage_config.id,
                            worker_id=worker.id,
                            attempt_id=attempt_id,
                            event_type="worker.started",
                            payload={"worker_id": worker.id, "model": worker.model, "attempt": attempt_number},
                        )

                        # Determine conversation handle (CONTINUE vs FRESH)
                        conv_handle: str | None = None
                        if stage_config.context == StageContextPolicy.CONTINUE:
                            workers = get_workers_for_run(conn, run_id)
                            curr_w = next((w for w in workers if w["worker_id"] == worker.id), None)
                            if curr_w and curr_w["conversation_handle"]:
                                conv_handle = curr_w["conversation_handle"]

                        # Construct TurnRequest contract
                        turn_req = TurnRequest(
                            attempt_id=attempt_id,
                            run_id=run_id,
                            stage_id=stage_config.id,
                            worker_id=worker.id,
                            account_ref=worker.account_ref,
                            model=worker.model,
                            prompt=prompt_text,
                            role=worker.role,
                            conversation_handle=conv_handle,
                            working_directory=str(workspace_dir),
                            required_sections=stage_config.required_sections,
                            allowance=TurnAllowance(
                                timeout_seconds=workflow_config.limits.max_wall_seconds,
                            ),
                        )

                        # Invoke provider adapter and stream progressive events
                        turn_result: TurnResult | None = None
                        try:
                            async for event_or_result in self.provider.run_turn(turn_req):
                                if isinstance(event_or_result, TurnEvent):
                                    if event_or_result.event_type == "started":
                                        child_pid = event_or_result.payload.get("pid")
                                        child_start = event_or_result.payload.get("process_start_time")
                                        if child_pid:
                                            update_attempt(
                                                conn,
                                                attempt_id=attempt_id,
                                                status=AttemptStatus.RUNNING.value,
                                                pid=child_pid,
                                                process_start_time=child_start,
                                            )
                                        self.log_event(
                                            conn,
                                            run_id,
                                            level="INFO",
                                            message=f"[{worker.id}] Subprocess active (PID {child_pid or os.getpid()}) with model '{worker.model}'",
                                            stage_id=stage_config.id,
                                            worker_id=worker.id,
                                            attempt_id=attempt_id,
                                        )
                                    elif event_or_result.event_type == "thought":
                                        thought_text = event_or_result.delta or str(event_or_result.payload.get("thought", ""))
                                        self.log_event(
                                            conn,
                                            run_id,
                                            level="REASONING",
                                            message=f"[{worker.id}] Model reasoning: {thought_text}",
                                            stage_id=stage_config.id,
                                            worker_id=worker.id,
                                            attempt_id=attempt_id,
                                            details={"thought": thought_text},
                                        )
                                    record_event(
                                        conn,
                                        run_id=run_id,
                                        stage_id=stage_config.id,
                                        worker_id=worker.id,
                                        attempt_id=attempt_id,
                                        event_type=f"worker.{event_or_result.event_type}",
                                        payload={"delta": event_or_result.delta, **event_or_result.payload},
                                    )
                                elif isinstance(event_or_result, TurnResult):
                                    turn_result = event_or_result
                                    self.log_event(
                                        conn,
                                        run_id,
                                        level="INFO" if turn_result.status == TurnStatus.SUCCESS else "ERROR",
                                        message=f"[{worker.id}] Turn concluded with status: {turn_result.status.value} (Usage: {turn_result.usage.get('total_tokens', 0)} tokens)",
                                        stage_id=stage_config.id,
                                        worker_id=worker.id,
                                        attempt_id=attempt_id,
                                    )
                        except asyncio.CancelledError:
                            update_attempt(
                                conn,
                                attempt_id=attempt_id,
                                status=AttemptStatus.CANCELLED.value,
                                finished_at=_utc_now_iso(),
                                error_details="Turn cancelled",
                            )
                            raise
                        except Exception as exc:
                            turn_result = TurnResult(
                                attempt_id=attempt_id,
                                status=TurnStatus.UNKNOWN_COMPLETION,
                                error_message=str(exc),
                            )

                        if turn_result is None:
                            turn_result = TurnResult(
                                attempt_id=attempt_id,
                                status=TurnStatus.UNKNOWN_COMPLETION,
                                error_message="Provider produced no TurnResult",
                            )

                        # Check run cancellation before processing outcome
                        if run_id in self._cancelled_runs:
                            update_attempt(
                                conn,
                                attempt_id=attempt_id,
                                status=AttemptStatus.CANCELLED.value,
                                finished_at=_utc_now_iso(),
                                error_details="Run cancelled by user",
                            )
                            return worker.id, TurnResult(
                                attempt_id=attempt_id,
                                status=TurnStatus.CANCELLED,
                                error_message="Run cancelled by user",
                            )

                        # ---------------------------------------------------------------
                        # Feature 26: Output Section Enforcement
                        # ---------------------------------------------------------------
                        if turn_result.status == TurnStatus.SUCCESS:
                            if not turn_result.parsed_sections and turn_result.structured_output:
                                try:
                                    turn_result.parsed_sections = CouncilOutputSections.model_validate(
                                        turn_result.structured_output
                                    )
                                    if not turn_result.output_text:
                                        turn_result.output_text = json.dumps(turn_result.structured_output, indent=2)
                                except Exception:
                                    pass

                            if not turn_result.parsed_sections and turn_result.output_text:
                                parsed, _ = parse_council_sections(turn_result.output_text)
                                turn_result.parsed_sections = parsed

                            if turn_result.parsed_sections is not None:
                                missing = turn_result.parsed_sections.validate_required(
                                    stage_config.required_sections
                                )
                                if missing:
                                    turn_result.status = TurnStatus.MALFORMED_OUTPUT
                                    turn_result.error_message = (
                                        f"Missing required output section(s): {', '.join(missing)}"
                                    )
                            else:
                                turn_result.status = TurnStatus.MALFORMED_OUTPUT
                                turn_result.error_message = "Output contains no valid structured sections"

                        last_result = turn_result

                        # If turn succeeded, store draft output artifact and commit attempt success
                        if turn_result.status == TurnStatus.SUCCESS:
                            out_text = turn_result.output_text or ""
                            if turn_result.parsed_sections:
                                out_text = json.dumps(
                                    turn_result.parsed_sections.model_dump(mode="json"), indent=2
                                )

                            # Output artifact is stored with released=False (sealed until stage barrier)
                            out_art = self.store_artifact(
                                run_id=run_id,
                                name=f"output_{stage_config.id}_{worker.id}.json",
                                data=out_text.encode("utf-8"),
                                stage_id=stage_config.id,
                                worker_id=worker.id,
                                attempt_id=attempt_id,
                                released=False,
                                mime_type="application/json",
                            )

                            # Update attempt to SUCCEEDED
                            update_attempt(
                                conn,
                                attempt_id=attempt_id,
                                status=AttemptStatus.SUCCEEDED.value,
                                finished_at=_utc_now_iso(),
                                result_artifact_id=out_art.id,
                                model_used=turn_result.observed_model or worker.model,
                                usage_json=json.dumps(turn_result.usage) if turn_result.usage else None,
                            )

                            # Update worker conversation handle
                            update_worker_status(
                                conn,
                                run_id=run_id,
                                worker_id=worker.id,
                                status=WorkerStatus.COMPLETED.value,
                                conversation_handle=turn_result.conversation_handle,
                            )

                            # Check dissent flag
                            # Invariant 4: Preserve dissent if critic objected
                            is_critic = "critic" in worker.role.lower() or stage_config.kind == StageKind.CRITIQUE
                            dissent_in_uncertainties = (
                                turn_result.parsed_sections is not None
                                and any(
                                    word in turn_result.parsed_sections.uncertainties.lower()
                                    for word in ("dissent", "object", "disagree", "contradict", "flaw")
                                )
                            )
                            if is_critic or dissent_in_uncertainties:
                                update_run_status(conn, run_id, status=RunStatus.RUNNING, dissent_recorded=True)

                            record_event(
                                conn,
                                run_id=run_id,
                                stage_id=stage_config.id,
                                worker_id=worker.id,
                                attempt_id=attempt_id,
                                event_type="worker.completed",
                                payload={"worker_id": worker.id, "artifact_id": out_art.id},
                            )

                            return worker.id, turn_result

                        else:
                            # Attempt failed
                            finish_iso = _utc_now_iso()
                            update_attempt(
                                conn,
                                attempt_id=attempt_id,
                                status=AttemptStatus.FAILED.value,
                                finished_at=finish_iso,
                                error_details=turn_result.error_message or turn_result.status.value,
                            )
                            record_event(
                                conn,
                                run_id=run_id,
                                stage_id=stage_config.id,
                                worker_id=worker.id,
                                attempt_id=attempt_id,
                                event_type="worker.failed",
                                payload={"worker_id": worker.id, "error": turn_result.error_message},
                            )

                            # Check if another retry attempt is permitted
                            if attempt_number <= existing_cnt + max_retries:
                                record_issue(
                                    conn,
                                    run_id=run_id,
                                    stage_id=stage_config.id,
                                    worker_id=worker.id,
                                    attempt_id=attempt_id,
                                    severity="warning",
                                    category="worker_retry",
                                    message=(
                                        f"Worker '{worker.id}' attempt {attempt_number} failed: "
                                        f"{turn_result.error_message}. Retrying (attempt {attempt_number + 1})..."
                                    ),
                                )
                                attempt_number += 1
                                continue
                            else:
                                # Retries exhausted
                                update_worker_status(conn, run_id, worker.id, status=WorkerStatus.FAILED.value)
                                record_issue(
                                    conn,
                                    run_id=run_id,
                                    stage_id=stage_config.id,
                                    worker_id=worker.id,
                                    attempt_id=attempt_id,
                                    severity="error",
                                    category="worker_failure",
                                    message=(
                                        f"Worker '{worker.id}' failed after {attempt_number} attempt(s): "
                                        f"{turn_result.error_message}"
                                    ),
                                )
                                return worker.id, turn_result
                finally:
                    self._active_attempts.get(run_id, set()).discard(attempt_id)

            return worker.id, (last_result or TurnResult(attempt_id="", status=TurnStatus.UNKNOWN_COMPLETION))
        finally:
            if curr_task:
                self._active_tasks.get(run_id, set()).discard(curr_task)
            conn.close()

    def _collect_prior_stage_outputs(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        input_stages: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Collect successful output text from prerequisite stages for prompt context."""
        outputs: list[dict[str, Any]] = []
        if not input_stages:
            return outputs

        placeholders = ",".join("?" for _ in input_stages)
        sql = f"""
            SELECT a.stage_id, a.worker_id, a.result_artifact_id, art.content_hash
            FROM attempts a
            LEFT JOIN artifacts art ON a.result_artifact_id = art.artifact_id
            WHERE a.run_id = ? AND a.stage_id IN ({placeholders}) AND a.status = 'SUCCEEDED' AND art.released = 1
            ORDER BY a.finished_at ASC
        """
        rows = conn.execute(sql, [run_id, *input_stages]).fetchall()
        for row in rows:
            content_hash = row["content_hash"]
            if content_hash and self.artifact_store.exists(content_hash):
                raw_bytes = self.artifact_store.get(content_hash)
                text = raw_bytes.decode("utf-8", errors="replace")
                parsed_sec = None
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        parsed_sec = CouncilOutputSections.model_validate(data)
                except Exception:
                    pass
                outputs.append(
                    {
                        "stage_id": row["stage_id"],
                        "worker_id": row["worker_id"],
                        "content": text,
                        "parsed_sections": parsed_sec,
                    }
                )
        return outputs
