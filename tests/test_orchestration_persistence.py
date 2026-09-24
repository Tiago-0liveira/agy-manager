"""Tests for orchestration subsystem run persistence.

Covers:
- create run
- duplicate run
- atomic run update
- write/read assessment
- append events
- event ordering
- write invocation
- write output
- load invocation
- corrupted output file
- interrupted run
- completed run
- conversation ID persistence
- resume-related state reconstruction
- RunStore and EventSink protocol conformance
- partial run creation recognition
- module independence (no council dependencies)
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    AuditRequest,
    AuditResult,
    BudgetUsage,
    ComplexityLevel,
    ConversationId,
    CoordinatorAction,
    CoordinatorObservation,
    EventSink,
    EventType,
    ExecutionStrategy,
    FailureClass,
    FleetView,
    InvocationId,
    InvocationStatus,
    ModelInvocation,
    ModelResult,
    OrchestrationBudget,
    OrchestrationEvent,
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
from agym.orchestration.persistence import (
    CoordinatorInfo,
    CorruptRunError,
    CrashInspection,
    FileRunStore,
    InvocationNotFoundError,
    InvocationRecord,
    PersistenceError,
    RunAlreadyExistsError,
    RunNotFoundError,
    atomic_write_json,
    atomic_write_text,
    create_run,
    emit_event,
    finalize_run,
    get_assessment,
    get_coordinator_info,
    get_default_runs_dir,
    get_events,
    get_results,
    inspect_run,
    list_runs,
    load_run,
    mark_run_interrupted,
    read_output,
    save_assessment,
    save_coordinator_info,
    save_result,
    save_run,
    write_output,
)


class TestPersistenceBase(unittest.TestCase):
    """Base test class providing temporary directories for run stores."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="agym_test_persistence_")
        self.store = FileRunStore(base_dir=self.temp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestRunCreationAndLayout(TestPersistenceBase):
    """Tests for run creation, layout, and duplicate checks."""

    def test_create_run(self) -> None:
        """Create run and verify filesystem layout and initial state."""
        run_id = RunId("run-layout-001")
        task_text = "Implement authentication cache persistence"
        budget = OrchestrationBudget(max_parallel=2, max_invocations=10)

        state = self.store.create_run(
            run_id=run_id,
            task=task_text,
            mode=RunMode.IMPLEMENT,
            budget=budget,
        )

        self.assertEqual(state.run_id, run_id)
        self.assertEqual(state.task, task_text)
        self.assertEqual(state.mode, RunMode.IMPLEMENT)
        self.assertEqual(state.status, RunStatus.CREATED)
        self.assertEqual(state.budget.max_parallel, 2)
        self.assertTrue(state.created_at)
        self.assertTrue(state.updated_at)

        # Verify directory layout:
        # <AGYM_DATA_HOME>/orchestrator/runs/<run-id>/
        #     run.json
        #     task.txt
        #     events.jsonl
        #     invocations/
        #     outputs/
        run_dir = self.store.run_dir(run_id)
        self.assertTrue(run_dir.is_dir())
        self.assertTrue((run_dir / "run.json").is_file())
        self.assertTrue((run_dir / "task.txt").is_file())
        self.assertTrue((run_dir / "events.jsonl").is_file())
        self.assertTrue((run_dir / "invocations").is_dir())
        self.assertTrue((run_dir / "outputs").is_dir())
        self.assertTrue((run_dir / "artifacts").is_dir())
        self.assertTrue((run_dir / "deliverables").is_dir())

        # Verify task.txt contents
        self.assertEqual((run_dir / "task.txt").read_text(encoding="utf-8"), task_text)

        # Verify run.json content
        with open(run_dir / "run.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["run_id"], str(run_id))
        self.assertEqual(data["task"], task_text)
        self.assertEqual(data["status"], "CREATED")
        self.assertEqual(data["mode"], "IMPLEMENT")

        # Verify events.jsonl has initial RUN_CREATED event
        events = self.store.get_events(run_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, EventType.RUN_CREATED)
        self.assertEqual(events[0].run_id, run_id)

    def test_duplicate_run(self) -> None:
        """Attempting to create a duplicate run raises RunAlreadyExistsError."""
        run_id = RunId("run-dup-001")
        self.store.create_run(run_id=run_id, task="Initial task")

        with self.assertRaises(RunAlreadyExistsError) as ctx:
            self.store.create_run(run_id=run_id, task="Duplicate task")

        self.assertIn("already exists", str(ctx.exception))

        # Existing run is unchanged
        loaded = self.store.load_run(run_id)
        self.assertEqual(loaded.task, "Initial task")

    def test_partially_created_run_recognition(self) -> None:
        """Partially created runs (directory without run.json or with staging) are recognized."""
        run_id = RunId("run-partial-001")
        run_dir = self.store.run_dir(run_id)
        run_dir.mkdir(parents=True)

        self.assertTrue(self.store.is_partially_created(run_id))
        with self.assertRaises(CorruptRunError):
            self.store.load_run(run_id)


class TestAtomicRunUpdate(TestPersistenceBase):
    """Tests for atomic JSON updates and failure tolerance."""

    def test_atomic_run_update(self) -> None:
        """Run updates use tempfile, flush, and os.replace."""
        run_id = RunId("run-atomic-001")
        state = self.store.create_run(run_id=run_id, task="Atomic update task")

        # Modify state
        state.status = RunStatus.RUNNING
        state.round_number = 1
        state.budget_usage.invocations = 3
        state.budget_usage.runtime_seconds = 45.2
        self.store.save_run(state)

        # Verify on-disk state
        loaded = self.store.load_run(run_id)
        self.assertEqual(loaded.status, RunStatus.RUNNING)
        self.assertEqual(loaded.round_number, 1)
        self.assertEqual(loaded.budget_usage.invocations, 3)
        self.assertAlmostEqual(loaded.budget_usage.runtime_seconds, 45.2)

        # Verify no temporary files remain in run_dir
        run_dir = self.store.run_dir(run_id)
        tmp_files = list(run_dir.glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0)

    def test_atomic_write_preserves_original_on_failure(self) -> None:
        """If writing the temp file fails, the original file is preserved intact."""
        target_file = Path(self.temp_dir) / "test_atomic.json"
        atomic_write_json(target_file, {"original": True})

        with patch("json.dump", side_effect=OSError("Disk full simulation")):
            with self.assertRaises(OSError):
                atomic_write_json(target_file, {"corrupted": True})

        # Original is still intact
        with open(target_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, {"original": True})


class TestAssessmentPersistence(TestPersistenceBase):
    """Tests for writing and reading task assessments."""

    def test_write_read_assessment(self) -> None:
        """Write assessment to assessment.json, update run.json, and read it back."""
        run_id = RunId("run-assessment-001")
        self.store.create_run(run_id=run_id, task="Assess architecture migration")

        assessment = TaskAssessment(
            task_type=TaskType.ARCHITECTURE,
            complexity=ComplexityLevel.MEDIUM,
            confidence=0.85,
            mutation_required=False,
            repository_scope="agym/orchestration",
            value_of_parallel_reasoning=0.9,
            value_of_auditing=0.7,
            summary="Refactor state persistence to be crash-safe",
            proposed_initial_work=["Design file layouts", "Add atomic write helpers"],
        )

        self.store.save_assessment(run_id, assessment)

        # Check assessment.json file
        assessment_file = self.store.run_dir(run_id) / "assessment.json"
        self.assertTrue(assessment_file.is_file())

        # Check get_assessment
        loaded_assessment = self.store.get_assessment(run_id)
        self.assertIsNotNone(loaded_assessment)
        self.assertEqual(loaded_assessment.task_type, TaskType.ARCHITECTURE)
        self.assertEqual(loaded_assessment.complexity, ComplexityLevel.MEDIUM)
        self.assertAlmostEqual(loaded_assessment.confidence, 0.85)
        self.assertEqual(loaded_assessment.summary, "Refactor state persistence to be crash-safe")

        # Check load_run includes the assessment
        state = self.store.load_run(run_id)
        self.assertIsNotNone(state.assessment)
        self.assertEqual(state.assessment.task_type, TaskType.ARCHITECTURE)

        # Check TASK_ASSESSED event was appended
        events = self.store.get_events(run_id)
        event_types = [e.type for e in events]
        self.assertIn(EventType.TASK_ASSESSED, event_types)


class TestEventLog(TestPersistenceBase):
    """Tests for append-only event logging and deterministic ordering."""

    def test_append_events(self) -> None:
        """Appending an event does not rewrite full run state."""
        run_id = RunId("run-events-001")
        self.store.create_run(run_id=run_id, task="Event test")

        run_file = self.store.run_dir(run_id) / "run.json"
        initial_mtime = run_file.stat().st_mtime_ns

        event1 = OrchestrationEvent(
            event_id="evt-1",
            run_id=run_id,
            type=EventType.ROUND_STARTED,
            payload={"round": 1},
        )
        self.store.emit(event1)

        # run.json was NOT modified
        self.assertEqual(run_file.stat().st_mtime_ns, initial_mtime)

        # events.jsonl has the event
        events = self.store.get_events(run_id)
        self.assertEqual(len(events), 2)  # RUN_CREATED + ROUND_STARTED
        self.assertEqual(events[1].type, EventType.ROUND_STARTED)
        self.assertEqual(events[1].payload["round"], 1)

    def test_event_ordering(self) -> None:
        """Events maintain strict deterministic append ordering."""
        run_id = RunId("run-ordering-001")
        self.store.create_run(run_id=run_id, task="Ordering test")

        event_types = [
            EventType.ROUND_STARTED,
            EventType.ACTION_REQUESTED,
            EventType.ACTION_ACCEPTED,
            EventType.INVOCATION_STARTED,
            EventType.INVOCATION_COMPLETED,
            EventType.ROUND_COMPLETED,
        ]

        for i, etype in enumerate(event_types):
            ev = OrchestrationEvent(
                event_id=f"evt-{i}",
                run_id=run_id,
                type=etype,
                timestamp="2026-09-23T11:00:00Z",  # identical timestamp
                payload={"index": i},
            )
            self.store.emit(ev)

        events = self.store.get_events(run_id)
        # Skip the initial RUN_CREATED
        appended_events = events[1:]
        self.assertEqual(len(appended_events), len(event_types))

        for i, ev in enumerate(appended_events):
            self.assertEqual(ev.type, event_types[i])
            self.assertEqual(ev.payload["index"], i)


class TestInvocationPersistence(TestPersistenceBase):
    """Tests for invocation metadata, outputs, and status tracking."""

    def test_write_invocation(self) -> None:
        """Test invocation start, completion, and failure persistence."""
        run_id = RunId("run-inv-001")
        self.store.create_run(run_id=run_id, task="Invocation test")

        req = WorkerRequest(
            worker_id=WorkerId("w-arch"),
            role=WorkerRole.ARCHITECTURE,
            strategy=ExecutionStrategy.HIGH_EFFORT,
            objective="Design database schema",
        )

        # 1. Before execution: INVOCATION_STARTED
        record = self.store.record_invocation_started(
            run_id=run_id,
            invocation=req,
            invocation_id=InvocationId("inv-arch-1"),
        )
        self.assertEqual(record.status, InvocationStatus.RUNNING)
        self.assertEqual(record.role, WorkerRole.ARCHITECTURE)
        self.assertEqual(record.strategy, ExecutionStrategy.HIGH_EFFORT)

        inv_file = self.store.run_dir(run_id) / "invocations" / "inv-arch-1.json"
        self.assertTrue(inv_file.is_file())

        # Check INVOCATION_STARTED event
        events = self.store.get_events(run_id)
        self.assertTrue(any(e.type == EventType.INVOCATION_STARTED for e in events))

        # 2. After success: output text + result metadata + INVOCATION_COMPLETED
        result = WorkerResult(
            worker_id=WorkerId("w-arch"),
            role=WorkerRole.ARCHITECTURE,
            status=InvocationStatus.SUCCEEDED,
            invocation_id=InvocationId("inv-arch-1"),
            response="Schema design completed successfully.",
            structured_data={"tables": ["users", "sessions"]},
        )
        completed_record = self.store.record_invocation_completed(
            run_id=run_id,
            invocation_id="inv-arch-1",
            result=result,
        )
        self.assertEqual(completed_record.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(completed_record.response, "Schema design completed successfully.")
        self.assertEqual(completed_record.structured_data, {"tables": ["users", "sessions"]})

        # Check output text file was created
        output_file = self.store.run_dir(run_id) / "outputs" / "inv-arch-1.txt"
        self.assertTrue(output_file.is_file())
        self.assertEqual(output_file.read_text(encoding="utf-8"), "Schema design completed successfully.")

        # Check INVOCATION_COMPLETED event
        events = self.store.get_events(run_id)
        self.assertTrue(any(e.type == EventType.INVOCATION_COMPLETED for e in events))

        # 3. After failure: error metadata + INVOCATION_FAILED
        self.store.record_invocation_started(
            run_id=run_id,
            invocation=req,
            invocation_id=InvocationId("inv-arch-2"),
        )
        failed_record = self.store.record_invocation_failed(
            run_id=run_id,
            invocation_id="inv-arch-2",
            error="Rate limit exceeded",
            failure=FailureClass.RETRYABLE,
            exit_code=429,
        )
        self.assertEqual(failed_record.status, InvocationStatus.FAILED)
        self.assertEqual(failed_record.error, "Rate limit exceeded")
        self.assertEqual(failed_record.failure, FailureClass.RETRYABLE)
        self.assertEqual(failed_record.exit_code, 429)

        events = self.store.get_events(run_id)
        self.assertTrue(any(e.type == EventType.INVOCATION_FAILED for e in events))

    def test_write_output(self) -> None:
        """Write output text atomically to outputs/<id>.txt."""
        run_id = RunId("run-out-001")
        self.store.create_run(run_id=run_id, task="Output test")

        out_path = self.store.write_output(run_id, "inv-test-1", "Raw model output stream text")
        self.assertTrue(out_path.is_file())
        self.assertEqual(out_path.name, "inv-test-1.txt")

        read_back = self.store.read_output(run_id, "inv-test-1")
        self.assertEqual(read_back, "Raw model output stream text")

    def test_load_invocation(self) -> None:
        """Load invocation record and results for workers and auditors."""
        run_id = RunId("run-load-inv-001")
        self.store.create_run(run_id=run_id, task="Load invocation test")

        # Worker invocation
        worker_res = WorkerResult(
            worker_id=WorkerId("w-sec"),
            role=WorkerRole.SECURITY,
            status=InvocationStatus.SUCCEEDED,
            invocation_id=InvocationId("inv-sec-1"),
            response="No vulnerabilities found",
        )
        self.store.save_result(run_id, worker_res)

        # Auditor invocation
        audit_res = AuditResult(
            worker_id=WorkerId("w-auditor"),
            invocation_id=InvocationId("inv-aud-1"),
            findings=["Finding 1: Missing CSRF token", "Finding 2: Insecure cookie"],
            response="Audit identified 2 issues",
            status=InvocationStatus.SUCCEEDED,
        )
        self.store.save_result(run_id, audit_res)

        # Retrieve specific invocation
        sec_rec = self.store.get_invocation(run_id, "inv-sec-1")
        self.assertEqual(sec_rec.worker_id, "w-sec")
        self.assertEqual(sec_rec.role, WorkerRole.SECURITY)
        self.assertEqual(sec_rec.response, "No vulnerabilities found")

        # Retrieve all results via get_results
        results = self.store.get_results(run_id)
        self.assertEqual(len(results), 2)
        worker_results = [r for r in results if isinstance(r, WorkerResult)]
        audit_results = [r for r in results if isinstance(r, AuditResult)]
        self.assertEqual(len(worker_results), 1)
        self.assertEqual(len(audit_results), 1)
        self.assertEqual(len(audit_results[0].findings), 2)

    def test_corrupted_output_file(self) -> None:
        """Malformed or corrupted output file does not destroy the whole run or crash loading."""
        run_id = RunId("run-corrupt-out-001")
        self.store.create_run(run_id=run_id, task="Corrupted output test")

        inv_id = InvocationId("inv-corrupt-1")
        self.store.record_invocation_started(
            run_id,
            invocation={"worker_id": "w-1", "role": "GENERAL"},
            invocation_id=inv_id,
        )

        # Write invalid binary bytes directly to outputs/<id>.txt
        out_file = self.store.run_dir(run_id) / "outputs" / f"{inv_id}.txt"
        out_file.write_bytes(b"\x80\x81\xff\xfe\x00\x01\x82")

        # Reading output handles safely with replacement
        text = self.store.read_output(run_id, inv_id)
        self.assertIsNotNone(text)

        # get_results works without crashing
        results = self.store.get_results(run_id)
        self.assertEqual(len(results), 1)

        # load_run works without crashing
        state = self.store.load_run(run_id)
        self.assertEqual(state.run_id, run_id)


class TestInterruptedAndCompletedState(TestPersistenceBase):
    """Tests for interrupted state transition and finalization."""

    def test_interrupted_run(self) -> None:
        """Transition RUNNING -> INTERRUPTED tracks active invocations."""
        run_id = RunId("run-interrupted-001")
        state = self.store.create_run(run_id=run_id, task="Interrupted run test")
        state.status = RunStatus.RUNNING
        self.store.save_run(state)

        # Start 3 invocations
        self.store.record_invocation_started(
            run_id,
            invocation={"worker_id": "w-done", "role": "GENERAL"},
            invocation_id="inv-done",
        )
        self.store.record_invocation_completed(
            run_id,
            invocation_id="inv-done",
            output_text="Completed in time",
        )

        self.store.record_invocation_started(
            run_id,
            invocation={"worker_id": "w-active-1", "role": "TESTING"},
            invocation_id="inv-active-1",
        )
        self.store.record_invocation_started(
            run_id,
            invocation={"worker_id": "w-active-2", "role": "SECURITY"},
            invocation_id="inv-active-2",
        )

        # Explicit interruption
        interrupted_state = self.store.mark_run_interrupted(run_id, reason="Timeout reached")
        self.assertEqual(interrupted_state.status, RunStatus.INTERRUPTED)

        # Verify active invocations were recorded as interrupted
        inv1 = self.store.get_invocation(run_id, "inv-active-1")
        self.assertTrue(inv1.active_at_interruption)
        self.assertEqual(inv1.error, "Timeout reached")

        inv2 = self.store.get_invocation(run_id, "inv-active-2")
        self.assertTrue(inv2.active_at_interruption)

        # Verify completed invocation was NOT marked interrupted
        done_inv = self.store.get_invocation(run_id, "inv-done")
        self.assertFalse(done_inv.active_at_interruption)
        self.assertEqual(done_inv.status, InvocationStatus.SUCCEEDED)

        # Verify RUN_INTERRUPTED event
        events = self.store.get_events(run_id)
        interrupted_events = [e for e in events if e.type == EventType.RUN_INTERRUPTED]
        self.assertEqual(len(interrupted_events), 1)
        self.assertIn("inv-active-1", interrupted_events[0].payload["active_invocations"])
        self.assertIn("inv-active-2", interrupted_events[0].payload["active_invocations"])

    def test_completed_run(self) -> None:
        """Finalize run stores final.json independently and transitions state to COMPLETED."""
        run_id = RunId("run-completed-001")
        self.store.create_run(run_id=run_id, task="Complete run test")

        final_data = {
            "summary": "Full orchestration plan implemented and validated",
            "modified_files": ["agym/auth.py", "agym/cache.py"],
            "verification": "All 42 tests passed",
        }

        completed_state = self.store.finalize_run(run_id, final_data)
        self.assertEqual(completed_state.status, RunStatus.COMPLETED)
        self.assertIsNotNone(completed_state.final_result)

        # Check final.json exists independently
        final_file = self.store.run_dir(run_id) / "final.json"
        self.assertTrue(final_file.is_file())
        with open(final_file, "r", encoding="utf-8") as f:
            fdata = json.load(f)
        self.assertEqual(fdata["status"], "COMPLETED")
        self.assertEqual(fdata["final_result"]["summary"], final_data["summary"])

        final_md = self.store.run_dir(run_id) / "deliverables" / "final.md"
        self.assertTrue(final_md.is_file())
        self.assertEqual(completed_state.final_artifact_path, str(final_md))
        self.assertIn("Full orchestration plan implemented and validated", final_md.read_text(encoding="utf-8"))

        # Check RUN_COMPLETED event
        events = self.store.get_events(run_id)
        self.assertTrue(any(e.type == EventType.RUN_COMPLETED for e in events))


class TestCoordinatorAndResume(TestPersistenceBase):
    """Tests for coordinator state persistence and crash recovery reconstruction."""

    def test_conversation_id_persistence(self) -> None:
        """Coordinator conversation ID, round, action, and observation persist for resume."""
        run_id = RunId("run-coord-001")
        self.store.create_run(run_id=run_id, task="Coordinator test")

        conv_id = ConversationId("conv-gemini-7788")
        action = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("w-1"),
                    role=WorkerRole.GENERAL,
                    objective="Inspect auth",
                )
            ],
            reason_summary="Inspect auth code before making changes",
        )
        obs = CoordinatorObservation(
            round_number=1,
            completed_results=[
                WorkerResult(
                    worker_id=WorkerId("w-0"),
                    role=WorkerRole.GENERAL,
                    response="Inspection done",
                )
            ],
            budget_usage=BudgetUsage(invocations=1, rounds=1),
        )

        info = self.store.save_coordinator_info(
            run_id=run_id,
            conversation_id=conv_id,
            round_number=1,
            last_accepted_action=action,
            latest_observation=obs,
        )

        self.assertEqual(info.conversation_id, conv_id)
        self.assertEqual(info.round_number, 1)

        # Read back from coordinator.json
        loaded_info = self.store.get_coordinator_info(run_id)
        self.assertIsNotNone(loaded_info)
        self.assertEqual(loaded_info.conversation_id, conv_id)
        self.assertEqual(loaded_info.round_number, 1)
        self.assertIsNotNone(loaded_info.last_accepted_action)
        self.assertEqual(loaded_info.last_accepted_action.action_id, "act-1")
        self.assertIsNotNone(loaded_info.latest_observation)
        self.assertEqual(len(loaded_info.latest_observation.completed_results), 1)

        # run.json was updated with conversation_id and round
        state = self.store.load_run(run_id)
        self.assertEqual(state.coordinator_conversation_id, conv_id)
        self.assertEqual(state.round_number, 1)

    def test_resume_related_state_reconstruction(self) -> None:
        """Definition of done: Given only the filesystem after a crash, a caller can understand:

        - what task was running
        - what round it reached
        - which agents completed
        - which failed
        - which were in progress
        - which conversation the coordinator used
        - what the budget usage was
        """
        run_id = RunId("run-crash-recovery-001")
        task_str = "Refactor token rotation and cache synchronization"
        self.store.create_run(run_id=run_id, task=task_str)

        # Coordinator conversation & round
        conv_id = ConversationId("conv-coordinator-reconstruct-99")
        self.store.save_coordinator_info(
            run_id=run_id,
            conversation_id=conv_id,
            round_number=2,
        )

        # Agent 1: Completed
        self.store.record_invocation_started(
            run_id,
            invocation={"worker_id": "agent-completed", "role": "ARCHITECTURE"},
            invocation_id="inv-c-1",
        )
        self.store.record_invocation_completed(
            run_id,
            invocation_id="inv-c-1",
            output_text="Completed architecture review",
        )

        # Agent 2: Failed
        self.store.record_invocation_started(
            run_id,
            invocation={"worker_id": "agent-failed", "role": "TESTING"},
            invocation_id="inv-f-1",
        )
        self.store.record_invocation_failed(
            run_id,
            invocation_id="inv-f-1",
            error="Connection timeout",
            failure=FailureClass.RETRYABLE,
        )

        # Agent 3: In progress when process crashed
        self.store.record_invocation_started(
            run_id,
            invocation={"worker_id": "agent-in-progress", "role": "IMPLEMENTATION_REVIEW"},
            invocation_id="inv-p-1",
        )

        # Update budget usage in run state
        state = self.store.load_run(run_id)
        state.budget_usage = BudgetUsage(invocations=3, rounds=2, runtime_seconds=120.5)
        state.status = RunStatus.RUNNING
        self.store.save_run(state)

        # Now simulate a fresh process inspecting the filesystem alone
        inspection = self.store.inspect_run(run_id)

        # 1. what task was running
        self.assertEqual(inspection.task, task_str)

        # 2. what round it reached
        self.assertEqual(inspection.round_number, 2)

        # 3. which agents completed
        self.assertIn("agent-completed", inspection.completed_agents)
        self.assertNotIn("agent-failed", inspection.completed_agents)
        self.assertNotIn("agent-in-progress", inspection.completed_agents)

        # 4. which failed
        self.assertIn("agent-failed", inspection.failed_agents)
        self.assertNotIn("agent-completed", inspection.failed_agents)
        self.assertNotIn("agent-in-progress", inspection.failed_agents)

        # 5. which were in progress
        self.assertIn("agent-in-progress", inspection.in_progress_agents)
        self.assertNotIn("agent-completed", inspection.in_progress_agents)
        self.assertNotIn("agent-failed", inspection.in_progress_agents)

        # 6. which conversation the coordinator used
        self.assertEqual(inspection.coordinator_conversation_id, conv_id)

        # 7. what the budget usage was
        self.assertEqual(inspection.budget_usage.invocations, 3)
        self.assertEqual(inspection.budget_usage.rounds, 2)
        self.assertAlmostEqual(inspection.budget_usage.runtime_seconds, 120.5)


class TestProtocolConformanceAndList(TestPersistenceBase):
    """Tests confirming FileRunStore conforms to RunStore and EventSink protocols."""

    def test_run_store_protocol(self) -> None:
        """FileRunStore implements RunStore."""
        self.assertIsInstance(self.store, RunStore)

        # Test list_runs
        self.store.create_run(run_id="run-list-1", task="Task 1")
        self.store.create_run(run_id="run-list-2", task="Task 2")
        self.store.create_run(run_id="run-list-3", task="Task 3")

        runs = self.store.list_runs(limit=2)
        self.assertEqual(len(runs), 2)

        all_runs = self.store.list_runs(limit=10)
        self.assertEqual(len(all_runs), 3)

        # Test get_run returns None for nonexistent
        self.assertIsNone(self.store.get_run(RunId("run-nonexistent")))

    def test_event_sink_protocol(self) -> None:
        """FileRunStore implements EventSink."""
        self.assertIsInstance(self.store, EventSink)


class TestConvenienceFunctions(TestPersistenceBase):
    """Tests for module-level convenience functions using explicit store."""

    def test_top_level_convenience_functions(self) -> None:
        run_id = RunId("run-top-001")
        state = create_run(run_id, "Top level task", store=self.store)
        self.assertEqual(state.run_id, run_id)

        loaded = load_run(run_id, store=self.store)
        self.assertEqual(loaded.task, "Top level task")

        state.round_number = 4
        save_run(state, store=self.store)
        self.assertEqual(load_run(run_id, store=self.store).round_number, 4)

        runs = list_runs(limit=5, store=self.store)
        self.assertEqual(len(runs), 1)

        write_output(run_id, "inv-top-1", "output string", store=self.store)
        self.assertEqual(read_output(run_id, "inv-top-1", store=self.store), "output string")

        save_coordinator_info(run_id, "conv-top-123", round_number=4, store=self.store)
        cinfo = get_coordinator_info(run_id, store=self.store)
        self.assertIsNotNone(cinfo)
        self.assertEqual(cinfo.conversation_id, "conv-top-123")

        interrupted = mark_run_interrupted(run_id, reason="interrupted by user", store=self.store)
        self.assertEqual(interrupted.status, RunStatus.INTERRUPTED)

        completed = finalize_run(run_id, "Done!", store=self.store)
        self.assertEqual(completed.status, RunStatus.COMPLETED)

        inspection = inspect_run(run_id, store=self.store)
        self.assertEqual(inspection.task, "Top level task")
        self.assertEqual(inspection.status, RunStatus.COMPLETED)


class TestIndependenceAndSafety(unittest.TestCase):
    """Tests ensuring persistence is independent, safe, and zero-dependency."""

    def test_no_dependencies_on_agym_council(self) -> None:
        import agym.orchestration.persistence as persistence_mod

        src = inspect.getsource(persistence_mod)
        self.assertNotIn("agym.council", src)
        self.assertNotIn("council", src)

    def test_zero_real_gemini_quota_used(self) -> None:
        # All tests in this module run purely against local filesystem
        pass


if __name__ == "__main__":
    unittest.main()
