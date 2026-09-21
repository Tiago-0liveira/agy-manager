"""Tests for Dual-Lease Capacity Scheduler & Transactional Attempt Claiming (Features 29 & 30)."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from agym.council.models import Attempt, AttemptStatus
from agym.council.scheduler import (
    CapacityScheduler,
    claim_attempt,
    claim_attempt_atomic,
    claim_attempt_by_id,
)
from agym.council.storage import (
    create_attempt,
    create_run,
    get_attempt,
    init_db,
)


class TestCapacityScheduler(unittest.IsolatedAsyncioTestCase):
    """Test dual-lease capacity scheduling, per-account serialization, and pause/cancel lifecycles."""

    async def test_per_account_serialization(self) -> None:
        """Two workers assigned to the same account must run strictly sequentially."""
        scheduler = CapacityScheduler(global_limit=4, per_account_limit=1)
        execution_order: list[str] = []
        active_counts: list[int] = []

        async def worker_task(name: str, account: str) -> None:
            async with scheduler.acquire_lease(account):
                active_counts.append(scheduler.account_active_count(account))
                execution_order.append(f"{name}_start")
                await asyncio.sleep(0.05)
                execution_order.append(f"{name}_end")

        # Launch two workers on the same account concurrently
        await asyncio.gather(
            worker_task("worker_1", "acc_alpha"),
            worker_task("worker_2", "acc_alpha"),
        )

        # Verify max concurrent on acc_alpha was strictly 1
        self.assertTrue(all(c == 1 for c in active_counts))
        # Verify worker 1 ended before worker 2 started (or vice versa)
        self.assertEqual(len(execution_order), 4)
        if execution_order[0] == "worker_1_start":
            self.assertEqual(execution_order[1], "worker_1_end")
            self.assertEqual(execution_order[2], "worker_2_start")
            self.assertEqual(execution_order[3], "worker_2_end")
        else:
            self.assertEqual(execution_order[1], "worker_2_end")
            self.assertEqual(execution_order[2], "worker_1_start")
            self.assertEqual(execution_order[3], "worker_1_end")

    async def test_different_accounts_run_concurrently(self) -> None:
        """Workers on different accounts run concurrently up to global_concurrency."""
        scheduler = CapacityScheduler(global_limit=3, per_account_limit=1)
        max_concurrent_global = 0
        lock = asyncio.Lock()
        running_now = 0

        async def worker_task(account: str) -> None:
            nonlocal max_concurrent_global, running_now
            async with scheduler.acquire_lease(account):
                async with lock:
                    running_now += 1
                    if running_now > max_concurrent_global:
                        max_concurrent_global = running_now
                await asyncio.sleep(0.05)
                async with lock:
                    running_now -= 1

        # Launch 3 workers on 3 distinct accounts
        await asyncio.gather(
            worker_task("acc_1"),
            worker_task("acc_2"),
            worker_task("acc_3"),
        )

        # Concurrency across distinct accounts should have reached 3
        self.assertEqual(max_concurrent_global, 3)

    async def test_global_concurrency_bounding(self) -> None:
        """Global concurrency is bounded by global_limit even across distinct accounts."""
        scheduler = CapacityScheduler(global_limit=2, per_account_limit=1)
        max_concurrent = 0
        lock = asyncio.Lock()
        running = 0

        async def worker_task(account: str) -> None:
            nonlocal max_concurrent, running
            async with scheduler.acquire_lease(account):
                async with lock:
                    running += 1
                    if running > max_concurrent:
                        max_concurrent = running
                await asyncio.sleep(0.04)
                async with lock:
                    running -= 1

        # 4 distinct accounts launched simultaneously
        await asyncio.gather(*(worker_task(f"acc_{i}") for i in range(4)))

        # Global limit was 2, so max concurrent must never exceed 2
        self.assertEqual(max_concurrent, 2)

    async def test_pause_and_resume_lifecycle(self) -> None:
        """Scheduler pause holds new dispatch until resumed."""
        scheduler = CapacityScheduler(global_limit=2, per_account_limit=1)
        started_events: list[str] = []

        async def task(name: str) -> None:
            async with scheduler.acquire_lease(f"acc_{name}"):
                started_events.append(name)

        # Pause scheduler before launching task_paused
        scheduler.pause()
        self.assertTrue(scheduler.is_paused)

        # Launch task while paused
        fut = asyncio.create_task(task("paused_task"))
        await asyncio.sleep(0.05)

        # Nothing should have started
        self.assertEqual(len(started_events), 0)

        # Resume scheduler
        scheduler.resume()
        self.assertFalse(scheduler.is_paused)

        await fut
        self.assertEqual(started_events, ["paused_task"])

    async def test_cancel_lifecycle(self) -> None:
        """Scheduler cancel unblocks waiting tasks with CancelledError."""
        scheduler = CapacityScheduler(global_limit=1, per_account_limit=1)

        # Acquire the sole permit
        acquired_first = asyncio.Event()

        async def holder() -> None:
            async with scheduler.acquire_lease("acc_1"):
                acquired_first.set()
                await asyncio.sleep(0.2)

        async def waiting() -> None:
            await acquired_first.wait()
            async with scheduler.acquire_lease("acc_1"):
                pass

        h_task = asyncio.create_task(holder())
        w_task = asyncio.create_task(waiting())

        await acquired_first.wait()
        # Cancel scheduler while waiting is queued
        scheduler.cancel()
        self.assertTrue(scheduler.is_cancelled)

        with self.assertRaises(asyncio.CancelledError):
            await w_task

        h_task.cancel()
        try:
            await h_task
        except asyncio.CancelledError:
            pass

    async def test_scheduler_reset_restores_dispatch(self) -> None:
        """Resetting a cancelled scheduler allows subsequent lease acquisitions."""
        scheduler = CapacityScheduler(global_limit=2, per_account_limit=1)
        scheduler.cancel()
        self.assertTrue(scheduler.is_cancelled)

        with self.assertRaises(asyncio.CancelledError):
            async with scheduler.acquire_lease("acc_1"):
                pass

        scheduler.reset()
        self.assertFalse(scheduler.is_cancelled)
        self.assertFalse(scheduler.is_paused)

        acquired = False
        async with scheduler.acquire_lease("acc_1"):
            acquired = True
        self.assertTrue(acquired)

    async def test_per_account_concurrency_override(self) -> None:
        """Custom concurrency limit per account allows multiple concurrent workers when configured."""
        scheduler = CapacityScheduler(global_limit=4, per_account_limit=1)
        max_seen = 0
        lock = asyncio.Lock()
        active = 0

        async def worker() -> None:
            nonlocal max_seen, active
            async with scheduler.acquire_lease("acc_multi", concurrency_limit=2):
                async with lock:
                    active += 1
                    if active > max_seen:
                        max_seen = active
                await asyncio.sleep(0.05)
                async with lock:
                    active -= 1

        await asyncio.gather(worker(), worker())
        self.assertEqual(max_seen, 2)


class TestTransactionalAttemptClaiming(unittest.TestCase):
    """Test transactional attempt claiming via SQLite BEGIN IMMEDIATE (Feature 30)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.conn = init_db(self.db_path)

        # Setup sample run and attempt
        run_data = {
            "name": "Test Run",
            "goal": "Test Goal",
            "inputs": [{"id": "inp1", "description": "desc", "required": True, "value": "val"}],
            "workers": [
                {
                    "id": "w1",
                    "name": "Worker 1",
                    "account_ref": "acc1",
                    "model": "model1",
                    "role": "Analyst",
                    "instructions": "inst",
                    "task": "task",
                }
            ],
            "stages": [
                {
                    "id": "s1",
                    "kind": "independent",
                    "workers": ["w1"],
                    "instruction": "inst",
                }
            ],
            "final_stage": "s1",
        }
        self.run_id = create_run(self.conn, run_data)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_claim_attempt_by_id_transitions_queued_to_claimed(self) -> None:
        """claim_attempt_by_id transitions a QUEUED attempt to CLAIMED."""
        attempt = Attempt(
            id="att-100",
            run_id=self.run_id,
            stage_id="s1",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.QUEUED,
        )
        create_attempt(self.conn, attempt)

        claimed = claim_attempt_by_id(self.conn, "att-100")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, AttemptStatus.CLAIMED)

        # Subsequent claim on already-claimed attempt must fail
        claimed_again = claim_attempt_by_id(self.conn, "att-100")
        self.assertIsNone(claimed_again)

    def test_claim_attempt_by_worker_and_run(self) -> None:
        """claim_attempt finds the queued attempt for worker and claims it."""
        attempt = Attempt(
            id="att-200",
            run_id=self.run_id,
            stage_id="s1",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.QUEUED,
        )
        create_attempt(self.conn, attempt)

        claimed = claim_attempt(self.conn, run_id=self.run_id, worker_id="w1", stage_id="s1")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "att-200")
        self.assertEqual(claimed.status, AttemptStatus.CLAIMED)

        # Verify DB state
        row = get_attempt(self.conn, "att-200")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "CLAIMED")

    def test_competing_claim_atomic_winner(self) -> None:
        """Under concurrent claim calls, exactly one caller succeeds."""
        attempt = Attempt(
            id="att-compete",
            run_id=self.run_id,
            stage_id="s1",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.QUEUED,
        )
        create_attempt(self.conn, attempt)

        res1 = claim_attempt_atomic(self.conn, "att-compete")
        res2 = claim_attempt_atomic(self.conn, "att-compete")

        self.assertTrue(res1)
        self.assertFalse(res2)


if __name__ == "__main__":
    unittest.main()
