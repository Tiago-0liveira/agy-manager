"""Tests for Startup Crash Reconciliation (Feature 32)."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agym.council.models import (
    Attempt,
    AttemptReconciliation,
    AttemptStatus,
    Run,
    RunStatus,
    StageStatus,
)
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.recovery import (
    reconcile_snapshot_os,
    reconcile_startup_crashes,
    reconcile_startup_crashes_sync,
)
from agym.council.storage import (
    create_attempt,
    create_run,
    get_attempt,
    get_run,
    get_stage,
    get_workers_for_run,
    init_db,
    list_events_for_run,
    list_issues_for_run,
    update_attempt,
    update_run_status,
)


class TestStartupCrashRecovery(unittest.IsolatedAsyncioTestCase):
    """Test startup crash reconciliation against dead processes, PID reuse, and provider sync."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "recovery_test.db"
        self.conn = init_db(self.db_path)

        # Setup baseline run
        run_data = {
            "name": "Crash Recovery Run",
            "goal": "Verify crash recovery",
            "inputs": [{"id": "i1", "description": "desc", "required": True, "value": "val"}],
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
                    "id": "stage-crash",
                    "kind": "independent",
                    "workers": ["w1"],
                    "instruction": "inst",
                }
            ],
            "final_stage": "stage-crash",
        }
        self.run_id = create_run(self.conn, run_data)
        update_run_status(self.conn, self.run_id, status=RunStatus.RUNNING)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_reconcile_dead_pid_transitions_attempt_to_unknown(self) -> None:
        """In-flight attempt with dead PID is marked UNKNOWN and run enters NEEDS_ATTENTION."""
        # Find a definitely non-existent PID
        fake_pid = 99999999
        attempt = Attempt(
            id="att-dead-pid",
            run_id=self.run_id,
            stage_id="stage-crash",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.RUNNING,
            pid=fake_pid,
            process_start_time=time.time() - 100.0,
        )
        create_attempt(self.conn, attempt)

        reconciled = await reconcile_startup_crashes(self.conn)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].attempt_id, "att-dead-pid")
        self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.UNKNOWN)

        # Verify DB attempt status updated
        att_row = get_attempt(self.conn, "att-dead-pid")
        self.assertIsNotNone(att_row)
        self.assertEqual(att_row["status"], "UNKNOWN")

        # Verify run status transitioned to NEEDS_ATTENTION
        run_row = get_run(self.conn, self.run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row["status"], "NEEDS_ATTENTION")

        # Verify stage status transitioned to NEEDS_ATTENTION
        stage_row = get_stage(self.conn, self.run_id, "stage-crash")
        self.assertIsNotNone(stage_row)
        self.assertEqual(stage_row["status"], "NEEDS_ATTENTION")

        # Verify worker status transitioned to FAILED
        workers = get_workers_for_run(self.conn, self.run_id)
        w1_row = next(w for w in workers if w["worker_id"] == "w1")
        self.assertEqual(w1_row["status"], "FAILED")

        # Verify audit issue logged
        issues = list_issues_for_run(self.conn, self.run_id)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "crash_recovery")
        self.assertEqual(issues[0]["severity"], "critical")

        # Verify audit event logged
        events = list_events_for_run(self.conn, self.run_id)
        reconcile_events = [e for e in events if e["event_type"] == "attempt.reconciled"]
        self.assertEqual(len(reconcile_events), 1)

    async def test_reconcile_pid_reuse_safeguard(self) -> None:
        """In-flight attempt with reused PID (start time mismatch) is detected and marked UNKNOWN."""
        current_pid = os.getpid()
        recorded_stale_time = time.time() - 50000.0  # Way in the past

        attempt = Attempt(
            id="att-pid-reuse",
            run_id=self.run_id,
            stage_id="stage-crash",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.RUNNING,
            pid=current_pid,
            process_start_time=recorded_stale_time,
        )
        create_attempt(self.conn, attempt)

        # Mock current process start time as current time
        with patch("agym.council.recovery.get_process_start_time", return_value=time.time()):
            reconciled = await reconcile_startup_crashes(self.conn)

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.UNKNOWN)
        self.assertIn("PID reuse detected", reconciled[0].reason)

        att_row = get_attempt(self.conn, "att-pid-reuse")
        self.assertIsNotNone(att_row)
        self.assertEqual(att_row["status"], "UNKNOWN")

    async def test_reconcile_claimed_without_pid(self) -> None:
        """Attempt claimed but crashed before spawning an OS PID is reconciled to UNKNOWN."""
        attempt = Attempt(
            id="att-claimed-no-pid",
            run_id=self.run_id,
            stage_id="stage-crash",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.CLAIMED,
            pid=None,
        )
        create_attempt(self.conn, attempt)

        reconciled = await reconcile_startup_crashes(self.conn)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.UNKNOWN)

    async def test_reconcile_with_fake_provider(self) -> None:
        """Provider-specific reconciliation is respected when adapter is provided."""
        fake_adapter = FakeProviderAdapter()
        attempt = Attempt(
            id="att-prov-1",
            run_id=self.run_id,
            stage_id="stage-crash",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.RUNNING,
            pid=99999999,
        )
        create_attempt(self.conn, attempt)

        reconciled = await reconcile_startup_crashes(self.conn, provider=fake_adapter)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].attempt_id, "att-prov-1")
        self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.UNKNOWN)

    def test_synchronous_reconcile_entrypoint(self) -> None:
        """reconcile_startup_crashes_sync correctly recovers in-flight attempts synchronously."""
        attempt = Attempt(
            id="att-sync-1",
            run_id=self.run_id,
            stage_id="stage-crash",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.DISPATCHED,
            pid=99999999,
        )
        create_attempt(self.conn, attempt)

        reconciled = reconcile_startup_crashes_sync(self.conn)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.UNKNOWN)

        att_row = get_attempt(self.conn, "att-sync-1")
        self.assertIsNotNone(att_row)
        self.assertEqual(att_row["status"], "UNKNOWN")

    async def test_no_in_flight_attempts_returns_empty(self) -> None:
        """Clean database with no in-flight work returns empty list without side effects."""
        reconciled = await reconcile_startup_crashes(self.conn)
        self.assertEqual(reconciled, [])
        run_row = get_run(self.conn, self.run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row["status"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
