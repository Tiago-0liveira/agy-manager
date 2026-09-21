"""Unit, concurrency, and adversarial tests for AGYM Council SQLite persistence.

Covers:
- Database initialization and PRAGMA settings (WAL mode, foreign keys ON, 5000ms timeout)
- Account CRUD and unique profile reference constraints
- Run, stage, and worker persistence and lifecycle
- Atomic attempt claiming under high concurrency
- Physical stage release barrier (draft isolation until atomic commit)
- Monotonic event sequence logging and SSE replay (Last-Event-ID)
- Foreign key cascade deletions
- Mutation idempotency tracking
- Startup crash recovery in-flight attempt detection
- Busy timeout contention handling
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agym.council.models import (
    Account,
    AccountAuthStatus,
    ArtifactRef,
    Attempt,
    AttemptStatus,
    ProviderType,
    Run,
    RunStatus,
    StageConfig,
    StageKind,
    WorkerConfig,
    WorkflowConfig,
    WorkflowInput,
)
from agym.council.storage import (
    claim_attempt_atomic,
    complete_idempotency_record,
    create_account,
    create_attempt,
    create_run,
    delete_account,
    get_account,
    get_account_by_profile_ref,
    get_attempt,
    get_connection,
    get_idempotency_record,
    get_in_flight_attempts,
    get_run,
    get_stage,
    get_stages_for_run,
    get_workers_for_run,
    immediate_transaction,
    init_db,
    list_accounts,
    list_artifacts_for_run,
    list_events_for_run,
    list_issues_for_run,
    list_released_artifacts_for_stages,
    list_runs,
    record_event,
    record_issue,
    release_stage_artifacts_atomic,
    resolve_issue,
    start_idempotency_record,
    store_artifact_record,
    update_account,
    update_attempt,
    update_run_status,
    update_stage_status,
    update_worker_status,
)


def _make_dummy_workflow_config() -> WorkflowConfig:
    return WorkflowConfig(
        name="Test Workflow",
        goal="Test Goal",
        inputs=[WorkflowInput(id="brief", description="Test input", value="data")],
        workers=[
            WorkerConfig(
                id="w1",
                name="Worker 1",
                account_ref="prof1",
                model="model-1",
                role="analyst",
                instructions="Work",
                task="Analyze",
            ),
            WorkerConfig(
                id="w2",
                name="Worker 2",
                account_ref="prof2",
                model="model-2",
                role="reviewer",
                instructions="Review",
                task="Critique",
            ),
        ],
        stages=[
            StageConfig(
                id="stage1",
                kind=StageKind.INDEPENDENT,
                workers=["w1"],
                instruction="Do independent work",
            ),
            StageConfig(
                id="stage2",
                kind=StageKind.SYNTHESIZE,
                workers=["w2"],
                input_stages=["stage1"],
                instruction="Synthesize results",
            ),
        ],
        final_stage="stage2",
    )


class TestStorageBasics(unittest.TestCase):
    """Test standard DB initialization, pragmas, and CRUD."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_council.db"
        self.conn = init_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_init_db_creates_tables_and_pragmas(self) -> None:
        # Check pragmas
        journal = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(journal.lower(), "wal")

        fk = self.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk, 1)

        busy = self.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(busy, 5000)

        # Check tables
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "schema_migrations",
            "accounts",
            "agent_templates",
            "workflow_templates",
            "runs",
            "stages",
            "workers",
            "attempts",
            "artifacts",
            "events",
            "issues",
            "idempotency_records",
        }
        self.assertTrue(expected.issubset(tables))

    def test_account_crud(self) -> None:
        acc = Account(
            profile_ref="prof-alpha",
            label="Alpha Profile",
            provider=ProviderType.ANTIGRAVITY,
            auth_status=AccountAuthStatus.READY,
            concurrency_limit=2,
        )
        acc_id = create_account(self.conn, acc)
        self.assertEqual(acc_id, acc.id)

        # Retrieve
        row = get_account(self.conn, acc_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["profile_ref"], "prof-alpha")
        self.assertEqual(row["concurrency_limit"], 2)

        # Retrieve by profile_ref
        row_ref = get_account_by_profile_ref(self.conn, "prof-alpha")
        self.assertIsNotNone(row_ref)
        self.assertEqual(row_ref["account_id"], acc_id)

        # List
        accounts = list_accounts(self.conn)
        self.assertEqual(len(accounts), 1)

        # Update
        acc.auth_status = AccountAuthStatus.NEEDS_LOGIN
        acc.label = "Alpha Updated"
        update_account(self.conn, acc)
        updated = get_account(self.conn, acc_id)
        self.assertEqual(updated["auth_status"], "needs_login")
        self.assertEqual(updated["display_label"], "Alpha Updated")

        # Delete
        self.assertTrue(delete_account(self.conn, acc_id))
        self.assertIsNone(get_account(self.conn, acc_id))
        self.assertFalse(delete_account(self.conn, acc_id))

    def test_run_and_stages_lifecycle(self) -> None:
        cfg = _make_dummy_workflow_config()
        run = Run(config=cfg, status=RunStatus.READY)
        run_id = create_run(self.conn, run)

        # Check run
        run_row = get_run(self.conn, run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row["name"], "Test Workflow")
        self.assertEqual(run_row["status"], "READY")

        # Check stages order
        stages = get_stages_for_run(self.conn, run_id)
        self.assertEqual(len(stages), 2)
        self.assertEqual(stages[0]["stage_id"], "stage1")
        self.assertEqual(stages[1]["stage_id"], "stage2")
        self.assertEqual(stages[0]["sequence_order"], 0)
        self.assertEqual(stages[1]["sequence_order"], 1)

        # Check workers
        workers = get_workers_for_run(self.conn, run_id)
        self.assertEqual(len(workers), 2)

        # Status transitions
        update_run_status(self.conn, run_id, RunStatus.RUNNING, current_stage_id="stage1")
        self.assertEqual(get_run(self.conn, run_id)["status"], "RUNNING")
        self.assertEqual(get_run(self.conn, run_id)["current_stage_id"], "stage1")

        update_stage_status(self.conn, run_id, "stage1", "COMPLETED")
        self.assertEqual(get_stage(self.conn, run_id, "stage1")["status"], "COMPLETED")

        update_worker_status(self.conn, run_id, "w1", "SUCCEEDED", conversation_handle="conv-w1")
        w_row = [w for w in get_workers_for_run(self.conn, run_id) if w["worker_id"] == "w1"][0]
        self.assertEqual(w_row["status"], "SUCCEEDED")
        self.assertEqual(w_row["conversation_handle"], "conv-w1")

    def test_event_sequence_monotonicity(self) -> None:
        cfg = _make_dummy_workflow_config()
        run = Run(config=cfg)
        create_run(self.conn, run)

        ev1 = record_event(self.conn, run.id, "run.started", {"step": 1})
        ev2 = record_event(self.conn, run.id, "stage.started", {"step": 2})
        ev3 = record_event(self.conn, run.id, "worker.dispatched", {"step": 3})
        ev4 = record_event(self.conn, run.id, "worker.completed", {"step": 4})
        ev5 = record_event(self.conn, run.id, "run.completed", {"step": 5})

        self.assertEqual(ev1.sequence, 1)
        self.assertEqual(ev2.sequence, 2)
        self.assertEqual(ev3.sequence, 3)
        self.assertEqual(ev4.sequence, 4)
        self.assertEqual(ev5.sequence, 5)

        # Test SSE reconnect replay via Last-Event-ID (after_sequence)
        replay_events = list_events_for_run(self.conn, run.id, after_sequence=3)
        self.assertEqual(len(replay_events), 2)
        self.assertEqual(replay_events[0]["sequence"], 4)
        self.assertEqual(replay_events[1]["sequence"], 5)

    def test_idempotency_record_flow(self) -> None:
        key = "mutate-run-create-123"
        action = "create_run"
        req_hash = "sha256-payload-test"

        # First request successfully registers IN_PROGRESS
        self.assertTrue(start_idempotency_record(self.conn, key, action, req_hash))
        # Immediate replay while in-progress returns False (conflict / in-progress)
        self.assertFalse(start_idempotency_record(self.conn, key, action, req_hash))

        # Complete operation
        complete_idempotency_record(
            self.conn, key, 201, "{\"run_id\": \"r1\"}", "{\"Content-Type\": \"application/json\"}"
        )

        rec = get_idempotency_record(self.conn, key)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(rec["response_status_code"], 201)
        self.assertEqual(rec["response_body"], "{\"run_id\": \"r1\"}")


class TestConcurrencyAndBarriers(unittest.TestCase):
    """Test physical release barriers, atomic attempt claiming, and multi-threaded contention."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_concurrency.db"
        self.conn = init_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_atomic_stage_release_barrier(self) -> None:
        cfg = _make_dummy_workflow_config()
        run = Run(config=cfg)
        create_run(self.conn, run)

        # Store two draft artifacts for stage1 (released = 0)
        art1 = ArtifactRef(
            id="art-draft-1",
            run_id=run.id,
            stage_id="stage1",
            worker_id="w1",
            name="draft1.json",
            path="cas/path1",
            size_bytes=50,
            sha256="1" * 64,
            released=False,
        )
        art2 = ArtifactRef(
            id="art-draft-2",
            run_id=run.id,
            stage_id="stage1",
            worker_id="w1",
            name="draft2.json",
            path="cas/path2",
            size_bytes=60,
            sha256="2" * 64,
            released=False,
        )
        store_artifact_record(self.conn, art1)
        store_artifact_record(self.conn, art2)

        # Verify draft isolation: peer query for released artifacts must return EMPTY
        released_before = list_released_artifacts_for_stages(self.conn, run.id, ["stage1"])
        self.assertEqual(len(released_before), 0)

        # Execute atomic barrier release
        release_stage_artifacts_atomic(self.conn, run.id, "stage1")

        # Verify stage is COMPLETED and released_at is populated
        stage_row = get_stage(self.conn, run.id, "stage1")
        self.assertEqual(stage_row["status"], "COMPLETED")
        self.assertIsNotNone(stage_row["released_at"])

        # Verify artifacts are now released (released = 1)
        released_after = list_released_artifacts_for_stages(self.conn, run.id, ["stage1"])
        self.assertEqual(len(released_after), 2)
        self.assertEqual({r["artifact_id"] for r in released_after}, {"art-draft-1", "art-draft-2"})

    def test_concurrent_attempt_claiming(self) -> None:
        cfg = _make_dummy_workflow_config()
        run = Run(config=cfg)
        create_run(self.conn, run)

        attempt = Attempt(
            id="attempt-target-1",
            run_id=run.id,
            stage_id="stage1",
            worker_id="w1",
            account_ref="prof1",
            model="model-1",
            status=AttemptStatus.QUEUED,
        )
        create_attempt(self.conn, attempt)

        def worker_claim_attempt() -> bool:
            # Open dedicated connection per thread to simulate independent workers
            c = get_connection(self.db_path)
            try:
                return claim_attempt_atomic(c, attempt.id)
            finally:
                c.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker_claim_attempt) for _ in range(10)]
            results = [f.result() for f in futures]

        # Exactly 1 worker must succeed; 9 must fail
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 9)

        # Final state in DB must be CLAIMED
        att_row = get_attempt(self.conn, attempt.id)
        self.assertEqual(att_row["status"], "CLAIMED")

    def test_foreign_key_cascade_deletion(self) -> None:
        cfg = _make_dummy_workflow_config()
        run = Run(config=cfg)
        create_run(self.conn, run)

        # Insert child records
        att = Attempt(
            run_id=run.id, stage_id="stage1", worker_id="w1", account_ref="prof1", model="m1"
        )
        create_attempt(self.conn, att)

        art = ArtifactRef(
            id="art-child",
            run_id=run.id,
            stage_id="stage1",
            worker_id="w1",
            name="out.txt",
            path="cas/p",
            size_bytes=10,
            sha256="9" * 64,
        )
        store_artifact_record(self.conn, art)

        record_event(self.conn, run.id, "run.ping", {})
        record_issue(self.conn, run.id, "ERR", "Sample issue")

        # Delete parent run
        with immediate_transaction(self.conn):
            self.conn.execute("DELETE FROM runs WHERE run_id = ?", (run.id,))

        # Verify cascades to child tables
        self.assertEqual(len(get_stages_for_run(self.conn, run.id)), 0)
        self.assertEqual(len(get_workers_for_run(self.conn, run.id)), 0)
        self.assertIsNone(get_attempt(self.conn, att.id))
        self.assertEqual(len(list_artifacts_for_run(self.conn, run.id)), 0)
        self.assertEqual(len(list_events_for_run(self.conn, run.id)), 0)
        self.assertEqual(len(list_issues_for_run(self.conn, run.id)), 0)

    def test_busy_timeout_contention(self) -> None:
        # Thread 1 holds RESERVED lock for 100ms
        # Thread 2 should wait up to 5000ms and succeed without SQLITE_BUSY
        def slow_writer() -> None:
            c = get_connection(self.db_path)
            try:
                with immediate_transaction(c):
                    c.execute(
                        "INSERT INTO schema_migrations VALUES (99, 'now', 'slow test')"
                    )
                    time.sleep(0.1)
            finally:
                c.close()

        def fast_writer() -> bool:
            time.sleep(0.02)  # ensure slow_writer acquired first
            c = get_connection(self.db_path)
            try:
                with immediate_transaction(c):
                    c.execute(
                        "INSERT INTO schema_migrations VALUES (100, 'now', 'fast test')"
                    )
                return True
            except sqlite3.OperationalError:
                return False
            finally:
                c.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(slow_writer)
            f2 = executor.submit(fast_writer)
            f1.result()
            self.assertTrue(f2.result())


class TestCrashRecoveryQueries(unittest.TestCase):
    """Test in-flight attempt queries used during startup crash reconciliation."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_recovery.db"
        self.conn = init_db(self.db_path)

        self.cfg = _make_dummy_workflow_config()
        self.run = Run(config=self.cfg)
        create_run(self.conn, self.run)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_in_flight_detection(self) -> None:
        # Insert 4 attempts in various states
        att_queued = Attempt(
            run_id=self.run.id, stage_id="stage1", worker_id="w1", account_ref="p1", model="m",
            status=AttemptStatus.QUEUED
        )
        att_running = Attempt(
            run_id=self.run.id, stage_id="stage1", worker_id="w1", account_ref="p1", model="m",
            status=AttemptStatus.RUNNING, pid=12345, process_start_time=1000.0
        )
        att_dispatched = Attempt(
            run_id=self.run.id, stage_id="stage1", worker_id="w2", account_ref="p2", model="m",
            status=AttemptStatus.DISPATCHED, pid=12346, process_start_time=1001.0
        )
        att_succeeded = Attempt(
            run_id=self.run.id, stage_id="stage1", worker_id="w1", account_ref="p1", model="m",
            status=AttemptStatus.SUCCEEDED, exit_code=0
        )

        for a in [att_queued, att_running, att_dispatched, att_succeeded]:
            create_attempt(self.conn, a)

        in_flight = get_in_flight_attempts(self.conn)
        in_flight_ids = {row["attempt_id"] for row in in_flight}

        # Must include only RUNNING and DISPATCHED
        self.assertEqual(in_flight_ids, {att_running.id, att_dispatched.id})

    def test_schema_migration_idempotence(self) -> None:
        # Calling init_db() a second time on the same database should not fail
        conn2 = init_db(self.db_path)
        row = conn2.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        self.assertEqual(row, 1)
        conn2.close()

    def test_issue_recording_and_resolution(self) -> None:
        issue_id = record_issue(
            self.conn,
            self.run.id,
            code="WORKER_TIMEOUT",
            message="Worker timed out after 600s",
            severity="error",
        )
        unresolved = list_issues_for_run(self.conn, self.run.id, unresolved_only=True)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["issue_id"], issue_id)

        resolve_issue(self.conn, issue_id, resolution_note="Retried successfully")
        self.assertEqual(len(list_issues_for_run(self.conn, self.run.id, unresolved_only=True)), 0)
        self.assertEqual(len(list_issues_for_run(self.conn, self.run.id, unresolved_only=False)), 1)


if __name__ == "__main__":
    unittest.main()
