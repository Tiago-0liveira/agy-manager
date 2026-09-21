"""Startup Crash Reconciliation for AGYM Council.

Implements Feature 32 (Startup Crash Reconciliation):
- Inspects SQLite database on startup for in-flight attempts (CLAIMED, DISPATCHED, RUNNING).
- Inspects OS process liveliness via PID and process start time to avoid PID recycling traps.
- Marks crashed or orphaned attempts as UNKNOWN.
- Transitions affected runs and stages to NEEDS_ATTENTION with structured issues and events logged.
- Coordinates with ProviderAdapter.reconcile() if an adapter is available.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from agym.council.models import (
    AttemptReconciliation,
    AttemptSnapshot,
    AttemptStatus,
    RunStatus,
    StageStatus,
    WorkerStatus,
)
from agym.council.providers.antigravity import get_process_start_time, is_pid_alive
from agym.council.providers.base import ProviderAdapter
from agym.council.storage import (
    get_in_flight_attempts,
    record_event,
    record_issue,
    update_attempt,
    update_run_status,
    update_stage_status,
    update_worker_status,
)


def build_snapshots_from_rows(rows: list[sqlite3.Row]) -> list[AttemptSnapshot]:
    """Convert raw SQLite rows from the attempts table into AttemptSnapshot models."""
    snapshots: list[AttemptSnapshot] = []
    for row in rows:
        snapshots.append(
            AttemptSnapshot(
                attempt_id=row["attempt_id"],
                run_id=row["run_id"],
                stage_id=row["stage_id"],
                worker_id=row["worker_id"],
                account_ref=row["account_ref"],
                pid=row["pid"],
                process_start_time=row["process_start_time"],
                status=row["status"],
                started_at=row["started_at"],
            )
        )
    return snapshots


def reconcile_snapshot_os(snapshot: AttemptSnapshot) -> AttemptReconciliation:
    """Inspect OS process state for a single AttemptSnapshot.

    Checks:
    1. PID presence and validity.
    2. OS process liveliness via os.kill(pid, 0).
    3. Process creation start time comparison to guard against OS PID reuse.
    """
    pid = snapshot.pid
    recorded_start = snapshot.process_start_time

    if pid is None or pid <= 0:
        return AttemptReconciliation(
            attempt_id=snapshot.attempt_id,
            reconciled_status=AttemptStatus.UNKNOWN,
            reason="No OS PID recorded for in-flight attempt; crashed during dispatch",
            details="No OS PID recorded for in-flight attempt; crashed during dispatch",
        )

    if not is_pid_alive(pid):
        return AttemptReconciliation(
            attempt_id=snapshot.attempt_id,
            reconciled_status=AttemptStatus.UNKNOWN,
            reason=f"OS process {pid} terminated unexpectedly while attempt was in-flight",
            details=f"OS process {pid} terminated unexpectedly while attempt was in-flight",
        )

    # Process exists; verify start time to prevent PID recycling false positives
    current_start = get_process_start_time(pid)
    if recorded_start is not None and current_start is not None:
        if abs(current_start - recorded_start) > 2.0:
            return AttemptReconciliation(
                attempt_id=snapshot.attempt_id,
                reconciled_status=AttemptStatus.UNKNOWN,
                reason=f"PID reuse detected: OS process {pid} start time mismatch (recorded: {recorded_start}, current: {current_start})",
                details=f"PID reuse detected: OS process {pid} start time mismatch (recorded: {recorded_start}, current: {current_start})",
            )

    # Process is still running from previous session, but orchestrator session died
    return AttemptReconciliation(
        attempt_id=snapshot.attempt_id,
        reconciled_status=AttemptStatus.UNKNOWN,
        reason=f"Orphaned in-flight process {pid} detected on startup reconciliation",
        details=f"Orphaned in-flight process {pid} detected on startup reconciliation",
    )


def apply_reconciliations_to_db(
    conn: sqlite3.Connection,
    snapshots: list[AttemptSnapshot],
    reconciliations: list[AttemptReconciliation],
) -> None:
    """Commit reconciled statuses, audit issues, and lifecycle updates to SQLite."""
    snap_map = {s.attempt_id: s for s in snapshots}

    for recon in reconciliations:
        snap = snap_map.get(recon.attempt_id)
        if not snap:
            continue

        raw_status = recon.reconciled_status
        status_str = raw_status.value if isinstance(raw_status, AttemptStatus) else str(raw_status)
        reason = recon.reason or recon.details

        # 1. Update attempt table
        update_attempt(
            conn,
            attempt_id=recon.attempt_id,
            status=status_str,
            error_details=reason,
        )

        # 2. Record audit issue
        severity = "critical" if status_str == "UNKNOWN" else "warning"
        record_issue(
            conn,
            run_id=snap.run_id,
            stage_id=snap.stage_id,
            worker_id=snap.worker_id,
            attempt_id=snap.attempt_id,
            severity=severity,
            category="crash_recovery",
            message=f"Attempt {snap.attempt_id} reconciled to {status_str}: {reason}",
            details={"pid": snap.pid, "recorded_start_time": snap.process_start_time},
        )

        # 3. Record audit event
        record_event(
            conn,
            run_id=snap.run_id,
            stage_id=snap.stage_id,
            worker_id=snap.worker_id,
            attempt_id=snap.attempt_id,
            event_type="attempt.reconciled",
            payload={"reconciled_status": status_str, "reason": reason},
        )

        # 4. If attempt failed or ended in unknown state, transition worker, stage and run to NEEDS_ATTENTION
        if status_str in ("UNKNOWN", "FAILED"):
            update_worker_status(
                conn,
                run_id=snap.run_id,
                worker_id=snap.worker_id,
                status=WorkerStatus.FAILED,
            )
            update_stage_status(
                conn,
                run_id=snap.run_id,
                stage_id=snap.stage_id,
                status=StageStatus.NEEDS_ATTENTION.value,
                error_message=f"Attempt {snap.attempt_id} interrupted or crashed: {reason}",
            )
            update_run_status(
                conn,
                run_id=snap.run_id,
                status=RunStatus.NEEDS_ATTENTION.value,
                error_message=f"In-flight attempt {snap.attempt_id} terminated unexpectedly: {reason}",
            )


async def reconcile_startup_crashes(
    conn: sqlite3.Connection,
    provider: ProviderAdapter | None = None,
) -> list[AttemptReconciliation]:
    """Scan database on startup and reconcile all in-flight attempts.

    Args:
        conn: SQLite persistence connection.
        provider: Optional ProviderAdapter for provider-specific reconciliation.

    Returns:
        List of AttemptReconciliation objects describing the resolved states.
    """
    rows = get_in_flight_attempts(conn)
    if not rows:
        return []

    snapshots = build_snapshots_from_rows(rows)

    provider_map: dict[str, AttemptReconciliation] = {}
    if provider is not None:
        try:
            provider_recons = await provider.reconcile(snapshots)
            provider_map = {r.attempt_id: r for r in provider_recons}
        except Exception:
            pass

    reconciliations: list[AttemptReconciliation] = []
    for snap in snapshots:
        if snap.attempt_id in provider_map:
            reconciliations.append(provider_map[snap.attempt_id])
        else:
            reconciliations.append(reconcile_snapshot_os(snap))

    apply_reconciliations_to_db(conn, snapshots, reconciliations)
    return reconciliations


def reconcile_startup_crashes_sync(
    conn: sqlite3.Connection,
    provider: ProviderAdapter | None = None,
) -> list[AttemptReconciliation]:
    """Synchronous entry point for startup crash reconciliation."""
    if provider is not None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # If already in event loop, schedule task or fall back to OS check
            snapshots = build_snapshots_from_rows(get_in_flight_attempts(conn))
            recons = [reconcile_snapshot_os(s) for s in snapshots]
            apply_reconciliations_to_db(conn, snapshots, recons)
            return recons
        else:
            return asyncio.run(reconcile_startup_crashes(conn, provider))

    # Fast synchronous OS PID reconciliation without provider
    rows = get_in_flight_attempts(conn)
    if not rows:
        return []

    snapshots = build_snapshots_from_rows(rows)
    reconciliations = [reconcile_snapshot_os(s) for s in snapshots]
    apply_reconciliations_to_db(conn, snapshots, reconciliations)
    return reconciliations
