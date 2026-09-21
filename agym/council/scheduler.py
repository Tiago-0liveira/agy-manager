"""Dual-Lease Capacity Scheduler & Transactional Attempt Claiming for AGYM Council.

Implements Feature 29 (Dual-Lease Capacity Scheduler) and Feature 30 (Transactional Attempt Claiming):
- Enforces global worker concurrency semaphore (default: LimitsConfig.global_concurrency).
- Enforces per-account serialization semaphore (default: LimitsConfig.per_account_concurrency = 1).
- Strict account serialization: workers assigned to the same account run strictly sequentially.
- Workers assigned to different accounts run concurrently up to the global limit.
- Starvation-free: per-account lease is acquired prior to the global lease.
- Pause lifecycle: pauses new lease dispatch while allowing active turns to drain.
- Cancel lifecycle: cancels waiting leases and unblocks callers with CancelledError.
- Transactional attempt claiming via SQLite BEGIN IMMEDIATE.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from agym.council.models import Attempt, AttemptStatus
from agym.council.storage import (
    claim_attempt_atomic,
    get_attempt,
    immediate_transaction,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CapacityScheduler:
    """Dual-lease asynchronous capacity scheduler.

    Manages execution permits ensuring:
    1. Total concurrent workers across the whole system <= global_limit
    2. Concurrent workers for any single account_ref <= per_account_limit (default 1)
    """

    def __init__(self, global_limit: int = 4, per_account_limit: int = 1) -> None:
        if global_limit <= 0:
            raise ValueError(f"global_limit must be > 0, got {global_limit}")
        if per_account_limit <= 0:
            raise ValueError(f"per_account_limit must be > 0, got {per_account_limit}")

        self.global_limit = global_limit
        self.per_account_limit = per_account_limit

        self._global_semaphore = asyncio.Semaphore(global_limit)
        self._account_semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

        # Metrics & active tracking
        self._global_active = 0
        self._account_active: dict[str, int] = {}

        # Pause and cancellation state
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Set means running (not paused)
        self._cancelled = False

    @property
    def is_paused(self) -> bool:
        """Return True if new dispatch is currently paused."""
        return not self._pause_event.is_set()

    @property
    def is_cancelled(self) -> bool:
        """Return True if the scheduler has been cancelled."""
        return self._cancelled

    @property
    def global_active_count(self) -> int:
        """Return number of currently active global leases."""
        return self._global_active

    def account_active_count(self, account_ref: str) -> int:
        """Return number of currently active leases for a given account."""
        return self._account_active.get(account_ref, 0)

    async def _get_or_create_account_semaphore(
        self, account_ref: str, concurrency_limit: int | None = None
    ) -> asyncio.Semaphore:
        """Get or lazily create a per-account serialization semaphore."""
        limit = concurrency_limit if (concurrency_limit is not None and concurrency_limit > 0) else self.per_account_limit
        async with self._lock:
            if account_ref not in self._account_semaphores:
                self._account_semaphores[account_ref] = asyncio.Semaphore(limit)
            return self._account_semaphores[account_ref]

    def pause(self) -> None:
        """Pause dispatching of new worker turns.

        In-flight worker turns continue to run and release their leases when done.
        Any worker waiting or calling acquire_lease() will block until resume().
        """
        self._pause_event.clear()

    def resume(self) -> None:
        """Resume dispatching of paused worker turns."""
        self._pause_event.set()

    def cancel(self) -> None:
        """Cancel the scheduler.

        Unblocks all waiting lease acquisitions with asyncio.CancelledError.
        """
        self._cancelled = True
        self._pause_event.set()

    def reset(self) -> None:
        """Reset cancellation and pause state for scheduler reuse."""
        self._cancelled = False
        self._pause_event.set()

    @asynccontextmanager
    async def acquire_lease(
        self, account_ref: str, concurrency_limit: int | None = None
    ) -> AsyncIterator[None]:
        """Acquire both per-account serialization and global worker capacity leases.

        Starvation-Free Acquisition Order:
        1. Acquire per-account semaphore first.
        2. Acquire global semaphore second.
        This prevents an account with many queued workers from hoarding global slots
        while waiting for its own serialization.
        Guarantees that no semaphores are held if execution is paused while waiting.

        Args:
            account_ref: The account or profile reference identifier.
            concurrency_limit: Optional custom concurrency limit for this account.

        Yields:
            None once both leases are successfully acquired.

        Raises:
            asyncio.CancelledError: If the scheduler is cancelled.
        """
        if self._cancelled:
            raise asyncio.CancelledError("Scheduler has been cancelled")

        account_sem = await self._get_or_create_account_semaphore(account_ref, concurrency_limit)

        while True:
            await self._pause_event.wait()
            if self._cancelled:
                raise asyncio.CancelledError("Scheduler has been cancelled")

            # 1. Acquire per-account lease
            await account_sem.acquire()
            try:
                if self._cancelled:
                    raise asyncio.CancelledError("Scheduler has been cancelled")
                if not self._pause_event.is_set():
                    # Paused while waiting for account semaphore: release and loop to wait
                    continue

                # 2. Acquire global capacity lease
                await self._global_semaphore.acquire()
                try:
                    if self._cancelled:
                        raise asyncio.CancelledError("Scheduler has been cancelled")
                    if not self._pause_event.is_set():
                        # Paused while waiting for global semaphore: release both and loop to wait
                        continue

                    # Track active lease counts
                    async with self._lock:
                        self._account_active[account_ref] = self._account_active.get(account_ref, 0) + 1
                        self._global_active += 1

                    try:
                        yield
                    finally:
                        async with self._lock:
                            self._account_active[account_ref] = max(
                                0, self._account_active.get(account_ref, 1) - 1
                            )
                            self._global_active = max(0, self._global_active - 1)
                    return
                finally:
                    self._global_semaphore.release()
            finally:
                account_sem.release()


# ---------------------------------------------------------------------------
# Transactional Attempt Claiming
# ---------------------------------------------------------------------------


def _row_to_attempt(row: sqlite3.Row) -> Attempt:
    d = dict(row)
    return Attempt(
        id=d["attempt_id"],
        run_id=d["run_id"],
        stage_id=d["stage_id"],
        worker_id=d["worker_id"],
        attempt_number=d.get("attempt_number", 1),
        account_ref=d["account_ref"],
        model=d.get("model_used") or "unknown",
        status=AttemptStatus(d["status"]),
        pid=d.get("pid"),
        process_start_time=d.get("process_start_time"),
        working_directory=d.get("workspace_dir"),
        prompt_artifact_id=d.get("prompt_artifact_id"),
        result_artifact_id=d.get("result_artifact_id"),
        stdout_artifact_id=d.get("stdout_artifact_id"),
        stderr_artifact_id=d.get("stderr_artifact_id"),
        error_message=d.get("error_details"),
        exit_code=d.get("exit_code"),
        started_at=d.get("started_at"),
        finished_at=d.get("finished_at"),
    )


def claim_attempt(
    conn: sqlite3.Connection,
    run_id: str,
    worker_id: str,
    stage_id: str | None = None,
) -> Attempt | None:
    """Atomically find and claim the next QUEUED attempt for a worker.

    Enforces SQLite BEGIN IMMEDIATE concurrency control.

    Args:
        conn: SQLite connection.
        run_id: Unique run ID.
        worker_id: Worker ID within the run.
        stage_id: Optional stage ID to restrict claim.

    Returns:
        The claimed Attempt domain model, or None if no queued attempt was found.
    """
    with immediate_transaction(conn):
        sql = "SELECT * FROM attempts WHERE run_id = ? AND worker_id = ? AND status = 'QUEUED'"
        params: list[Any] = [run_id, worker_id]
        if stage_id:
            sql += " AND stage_id = ?"
            params.append(stage_id)
        sql += " ORDER BY attempt_number ASC LIMIT 1"

        row = conn.execute(sql, params).fetchone()
        if not row:
            return None

        attempt_id = row["attempt_id"]
        now_iso = _utc_now_iso()
        conn.execute(
            "UPDATE attempts SET status = 'CLAIMED', updated_at = ? WHERE attempt_id = ?",
            (now_iso, attempt_id),
        )

        claimed_row = conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if not claimed_row:
            return None

        return _row_to_attempt(claimed_row)


def claim_attempt_by_id(conn: sqlite3.Connection, attempt_id: str) -> Attempt | None:
    """Atomically transition a specific attempt from QUEUED to CLAIMED by ID.

    Returns:
        The claimed Attempt model, or None if attempt was not in QUEUED state.
    """
    with immediate_transaction(conn):
        row = conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if not row or row["status"] != "QUEUED":
            return None

        now_iso = _utc_now_iso()
        conn.execute(
            "UPDATE attempts SET status = 'CLAIMED', updated_at = ? WHERE attempt_id = ?",
            (now_iso, attempt_id),
        )

        claimed_row = conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if not claimed_row:
            return None

        return _row_to_attempt(claimed_row)

