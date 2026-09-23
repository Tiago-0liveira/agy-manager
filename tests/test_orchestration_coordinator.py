"""Tests for AGYM orchestration coordinator runtime.

Covers:
- Initial assessment (TaskAssessment extraction and validation)
- First action (CoordinatorAction extraction and validation)
- Second adaptive action (CoordinatorObservation feedback -> adaptive decision)
- Three rounds in a single persistent session (no per-round process respawning)
- Finalize action (clean termination with final_response)
- Malformed response then corrected response (bounded correction recovery)
- Malformed response repeatedly (bounded retry halting, no infinite loop)
- Coordinator process crash (typed failure returned, RunStore status untouched by coordinator)
- Conversation ID recorded (captured and persisted as soon as known)
- Resume conversation (reconnects with conversation ID and AGYM-authoritative RunState)
- Close (deterministic resource release, closed client protection, context manager)
- Independence & Safety (zero real quota, zero council dependencies)
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    AuditRequest,
    AuditResult,
    BudgetUsage,
    ComplexityLevel,
    ConversationId,
    CoordinatorAction,
    CoordinatorClient as CoordinatorClientProtocol,
    CoordinatorObservation,
    ExecutionStrategy,
    FailureClass,
    FleetView,
    InvocationId,
    InvocationStatus,
    ModelInvocation,
    ModelResult,
    OrchestrationBudget,
    RunId,
    RunMode,
    RunState,
    RunStatus,
    TaskAssessment,
    TaskType,
    WorkerId,
    WorkerRequest,
    WorkerResult,
    WorkerRole,
    WorkspaceMode,
)
from agym.orchestration.coordinator import (
    CoordinatorClient,
    CoordinatorClosedError,
    CoordinatorCrashError,
    CoordinatorError,
    CoordinatorRuntime,
    CoordinatorStartResult,
    CoordinatorTimeoutError,
)
from agym.orchestration.persistence import FileRunStore
from agym.orchestration.protocol import (
    ActionValidationError,
    ForbiddenFieldError,
    MalformedResponseError,
    ProtocolSchemaError,
)
from agym.orchestration.runner import FakeModelRunner, FakeModelSession

# ============================================================================
# Test Fixtures & Scripted Responses
# ============================================================================

INITIAL_RESPONSE_DATA: dict = {
    "assessment": {
        "task_type": "GENERAL",
        "complexity": "SMALL",
        "confidence": 0.9,
        "mutation_required": False,
        "repository_scope": "agym/orchestration",
        "value_of_parallel_reasoning": 0.6,
        "value_of_auditing": 0.4,
        "summary": "Initial architecture assessment of orchestration subsystem",
        "proposed_initial_work": ["Analyze code", "Synthesize findings"],
    },
    "action": {
        "action_id": "act-round-0",
        "kind": "RUN_WORKERS",
        "workers": [
            {
                "worker_id": "w-arch",
                "role": "GENERAL",
                "strategy": "STANDARD",
                "workspace_mode": "READ_ONLY",
                "objective": "Analyze current subsystem architecture",
            }
        ],
        "reason_summary": "Need initial baseline inspection",
    },
}

ROUND_1_ACTION_DATA: dict = {
    "action_id": "act-round-1",
    "kind": "RUN_AUDITORS",
    "auditors": [
        {
            "worker_id": "audit-arch",
            "target_worker_ids": ["w-arch"],
            "focus": "Review architectural invariants and edge cases",
            "strategy": "STANDARD",
        }
    ],
    "reason_summary": "Verify findings before synthesis",
}

ROUND_2_ACTION_DATA: dict = {
    "action_id": "act-round-2",
    "kind": "RUN_SYNTHESIS",
    "workers": [
        {
            "worker_id": "synth-plan",
            "role": "SYNTHESIZER",
            "strategy": "STANDARD",
            "workspace_mode": "READ_ONLY",
            "objective": "Synthesize architecture analysis and audit findings into plan",
        }
    ],
    "reason_summary": "Consolidate results",
}

FINALIZE_ACTION_DATA: dict = {
    "action_id": "act-finalize",
    "kind": "FINALIZE",
    "final_response": "The architecture has been verified and the implementation plan is ready.",
    "reason_summary": "All investigation rounds complete and synthesized",
}


# ============================================================================
# Test Suites
# ============================================================================


class TestCoordinatorLifecycle(unittest.TestCase):
    """Tests for normal coordinator start, decisions, multi-round session, and finalize."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.run_store = FileRunStore(self.tmp_dir)
        self.run_id = RunId("run-coord-test")
        self.run_store.create_run(self.run_id, "Test coordinator run", mode=RunMode.PLAN)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_protocol_conformance(self) -> None:
        """Verify CoordinatorClient satisfies CoordinatorClientProtocol."""
        runner = FakeModelRunner()
        coord = CoordinatorClient(runner=runner)
        self.assertIsInstance(coord, CoordinatorClientProtocol)
        self.assertEqual(CoordinatorRuntime, CoordinatorClient)

    def test_initial_assessment(self) -> None:
        """Test initial task assessment parsing and fields."""
        runner = FakeModelRunner(responses=[INITIAL_RESPONSE_DATA])
        coord = CoordinatorClient(
            runner=runner,
            run_store=self.run_store,
            run_id=self.run_id,
        )

        fleet = FleetView(available_profiles=3, max_parallel=3, standard_capacity=3)
        result = coord.start(
            task="Inspect orchestration architecture",
            fleet_view=fleet,
            run_mode=RunMode.PLAN,
            budget=OrchestrationBudget(max_rounds=5, max_invocations=10),
            repository_scope="agym/orchestration",
        )

        self.assertIsInstance(result, CoordinatorStartResult)
        assessment = result.assessment
        self.assertEqual(assessment.task_type, TaskType.GENERAL)
        self.assertEqual(assessment.complexity, ComplexityLevel.SMALL)
        self.assertAlmostEqual(assessment.confidence, 0.9)
        self.assertFalse(assessment.mutation_required)
        self.assertEqual(assessment.repository_scope, "agym/orchestration")
        self.assertIn("Initial architecture assessment", assessment.summary)

        # assess_task method also returns assessment
        assessed = coord.assess_task("Inspect orchestration architecture", fleet)
        self.assertEqual(assessed.summary, assessment.summary)

    def test_first_action(self) -> None:
        """Test first action produced during coordinator start."""
        runner = FakeModelRunner(responses=[INITIAL_RESPONSE_DATA])
        coord = CoordinatorClient(
            runner=runner,
            run_store=self.run_store,
            run_id=self.run_id,
        )

        fleet = FleetView(available_profiles=2, max_parallel=2)
        assessment, action, conv_id = coord.start(
            task="Inspect orchestration architecture",
            fleet_view=fleet,
        )

        self.assertEqual(action.action_id, ActionId("act-round-0"))
        self.assertEqual(action.kind, ActionKind.RUN_WORKERS)
        self.assertEqual(len(action.workers), 1)
        self.assertEqual(action.workers[0].worker_id, WorkerId("w-arch"))
        self.assertEqual(action.workers[0].role, WorkerRole.GENERAL)
        self.assertEqual(action.workers[0].workspace_mode, WorkspaceMode.READ_ONLY)
        self.assertEqual(action.reason_summary, "Need initial baseline inspection")
        self.assertIsNotNone(conv_id)

    def test_second_adaptive_action(self) -> None:
        """Test coordinator receiving observation and producing second adaptive action."""
        runner = FakeModelRunner(responses=[INITIAL_RESPONSE_DATA, ROUND_1_ACTION_DATA])
        coord = CoordinatorClient(
            runner=runner,
            run_store=self.run_store,
            run_id=self.run_id,
        )

        coord.start(task="Analyze architecture", fleet_view=FleetView())

        # Simulate Round 1 observation
        obs = CoordinatorObservation(
            round_number=1,
            completed_results=[
                WorkerResult(
                    worker_id=WorkerId("w-arch"),
                    role=WorkerRole.GENERAL,
                    response="Architecture is clean and modular.",
                )
            ],
            budget_usage=BudgetUsage(invocations=1, rounds=1),
            fleet_view=FleetView(available_profiles=2),
        )

        action_1 = coord.decide_action(obs)
        self.assertEqual(action_1.action_id, ActionId("act-round-1"))
        self.assertEqual(action_1.kind, ActionKind.RUN_AUDITORS)
        self.assertEqual(len(action_1.auditors), 1)
        self.assertEqual(action_1.auditors[0].worker_id, WorkerId("audit-arch"))
        self.assertEqual(action_1.auditors[0].target_worker_ids, [WorkerId("w-arch")])

    def test_three_rounds_in_single_persistent_session(self) -> None:
        """Verify that multiple rounds execute within a single persistent session.

        Does not spawn a new coordinator model for every round under normal conditions.
        """
        runner = FakeModelRunner(
            responses=[
                INITIAL_RESPONSE_DATA,  # Round 0
                ROUND_1_ACTION_DATA,    # Round 1
                ROUND_2_ACTION_DATA,    # Round 2
                FINALIZE_ACTION_DATA,   # Round 3
            ]
        )
        coord = CoordinatorClient(
            runner=runner,
            run_store=self.run_store,
            run_id=self.run_id,
        )

        # Round 0: start
        assessment, action_0, conv_id = coord.start("Multi-round task")
        self.assertEqual(action_0.action_id, ActionId("act-round-0"))
        initial_session = coord._session
        self.assertIsNotNone(initial_session)

        # Round 1: observation 1 -> action 1
        obs_1 = CoordinatorObservation(
            round_number=1,
            completed_results=[
                WorkerResult(worker_id=WorkerId("w-arch"), role=WorkerRole.GENERAL, response="Arch verified")
            ],
        )
        action_1 = coord.decide_action(obs_1)
        self.assertEqual(action_1.action_id, ActionId("act-round-1"))
        # Verify SAME session instance was reused
        self.assertIs(coord._session, initial_session)

        # Round 2: observation 2 -> action 2
        obs_2 = CoordinatorObservation(
            round_number=2,
            completed_results=[
                AuditResult(worker_id=WorkerId("audit-arch"), findings=["Finding A", "Finding B"])
            ],
        )
        action_2 = coord.decide_action(obs_2)
        self.assertEqual(action_2.action_id, ActionId("act-round-2"))
        self.assertIs(coord._session, initial_session)

        # Round 3: observation 3 -> action 3 (finalize)
        obs_3 = CoordinatorObservation(
            round_number=3,
            completed_results=[
                WorkerResult(worker_id=WorkerId("synth-plan"), role=WorkerRole.SYNTHESIZER, response="Plan synthesized")
            ],
        )
        action_3 = coord.decide_action(obs_3)
        self.assertEqual(action_3.kind, ActionKind.FINALIZE)
        self.assertIs(coord._session, initial_session)

        # Verify turn count on the persistent session
        if isinstance(coord._session, FakeModelSession):
            self.assertEqual(coord._session._turn_count, 4)
            self.assertEqual(len(coord._session.sent_prompts), 4)

    def test_finalize_action(self) -> None:
        """Test coordinator finalizing the orchestration run."""
        runner = FakeModelRunner(responses=[INITIAL_RESPONSE_DATA, FINALIZE_ACTION_DATA])
        coord = CoordinatorClient(runner=runner, run_store=self.run_store, run_id=self.run_id)

        coord.start("Task to finalize")
        obs = CoordinatorObservation(
            round_number=1,
            completed_results=[
                WorkerResult(worker_id=WorkerId("w-arch"), role=WorkerRole.GENERAL, response="Complete")
            ],
        )
        action = coord.decide_action(obs)
        self.assertEqual(action.kind, ActionKind.FINALIZE)
        self.assertEqual(action.final_response, "The architecture has been verified and the implementation plan is ready.")
        self.assertEqual(len(action.workers), 0)
        self.assertEqual(len(action.auditors), 0)


# ============================================================================
# Recovery & Crash Tests
# ============================================================================


class TestCoordinatorRecoveryAndErrors(unittest.TestCase):
    """Tests for protocol recovery, repeated malformed responses, and crashes."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.run_store = FileRunStore(self.tmp_dir)
        self.run_id = RunId("run-recovery-test")
        self.run_store.create_run(self.run_id, "Test recovery run", mode=RunMode.PLAN)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_malformed_response_then_corrected_response(self) -> None:
        """Verify bounded recovery when model returns malformed JSON then valid JSON."""
        runner = FakeModelRunner(
            responses=[
                INITIAL_RESPONSE_DATA,
                # Turn 1: malformed response
                "Here is my decision: I think we should run workers but without JSON.",
                # Turn 2: corrected response after correction prompt
                ROUND_1_ACTION_DATA,
            ]
        )
        coord = CoordinatorClient(
            runner=runner,
            run_store=self.run_store,
            run_id=self.run_id,
            max_correction_attempts=2,
        )

        coord.start("Task with recovery")

        obs = CoordinatorObservation(
            round_number=1,
            completed_results=[
                WorkerResult(worker_id=WorkerId("w-arch"), role=WorkerRole.GENERAL, response="OK")
            ],
        )

        action = coord.decide_action(obs)
        self.assertEqual(action.action_id, ActionId("act-round-1"))
        self.assertEqual(action.kind, ActionKind.RUN_AUDITORS)

        # Check that correction prompt was sent to the session
        session = coord._session
        self.assertIsInstance(session, FakeModelSession)
        self.assertEqual(len(session.sent_prompts), 3)  # initial, observation, correction
        correction_sent = session.sent_prompts[2]
        self.assertIn("Your previous response violated the CoordinatorAction schema", correction_sent)

    def test_malformed_response_repeatedly_halts_without_infinite_loop(self) -> None:
        """Verify that repeated malformed output raises typed error and halts."""
        runner = FakeModelRunner(
            responses=[
                INITIAL_RESPONSE_DATA,
                # Turn 1: malformed
                "Malformed text 1",
                # Turn 2: malformed again
                "Malformed text 2",
                # Turn 3: malformed again
                "Malformed text 3",
            ]
        )
        coord = CoordinatorClient(
            runner=runner,
            run_store=self.run_store,
            run_id=self.run_id,
            max_correction_attempts=2,
        )

        coord.start("Task with repeated malformed output")

        obs = CoordinatorObservation(
            round_number=1,
            completed_results=[
                WorkerResult(worker_id=WorkerId("w-arch"), role=WorkerRole.GENERAL, response="OK")
            ],
        )

        with self.assertRaises(MalformedResponseError):
            coord.decide_action(obs)

        # Verify bounded attempts: exactly 1 normal attempt + 2 correction attempts = 3 total turns
        session = coord._session
        self.assertIsInstance(session, FakeModelSession)
        self.assertEqual(len(session.sent_prompts), 4)  # 1 initial + 3 in decide_action

    def test_coordinator_process_crash_returns_typed_failure_without_marking_run(self) -> None:
        """Verify coordinator crash returns typed failure and does NOT mark run failed.

        Engine owns run status. Coordinator runtime never marks run status itself.
        """
        runner = FakeModelRunner(
            responses=[
                INITIAL_RESPONSE_DATA,
                # Turn 1: process crash (nonzero exit code)
                ModelResult(
                    invocation_id=InvocationId("inv-crash"),
                    status=InvocationStatus.FAILED,
                    exit_code=1,
                    error="Coordinator process terminated with exit code 1",
                ),
            ]
        )
        coord = CoordinatorClient(
            runner=runner,
            run_store=self.run_store,
            run_id=self.run_id,
        )

        coord.start("Crash task")

        obs = CoordinatorObservation(round_number=1)

        with self.assertRaises(CoordinatorCrashError) as cm:
            coord.decide_action(obs)

        err = cm.exception
        self.assertIsInstance(err, CoordinatorCrashError)
        self.assertIn("crashed", err.message.lower())
        self.assertEqual(err.exit_code, 1)
        self.assertIsNotNone(err.failure)
        self.assertEqual(err.failure.error_type, "CoordinatorCrash")

        # Crucial invariant: The coordinator did NOT mark the run as FAILED in RunStore
        run_state = self.run_store.load_run(self.run_id)
        self.assertEqual(run_state.status, RunStatus.CREATED)
        self.assertNotEqual(run_state.status, RunStatus.FAILED)

    def test_coordinator_process_exception_raises_crash_error(self) -> None:
        """Verify unexpected exception during session communication raises CoordinatorCrashError."""
        from unittest.mock import MagicMock

        runner = FakeModelRunner(responses=[INITIAL_RESPONSE_DATA])
        coord = CoordinatorClient(runner=runner, run_store=self.run_store, run_id=self.run_id)
        coord.start("Exception crash task")

        # Simulate broken subprocess communication on the active session
        assert coord._session is not None
        coord._session.send = MagicMock(side_effect=RuntimeError("Broken pipe to coordinator process"))

        obs = CoordinatorObservation(round_number=1)
        with self.assertRaises(CoordinatorCrashError) as cm:
            coord.decide_action(obs)

        self.assertIsInstance(cm.exception.cause, RuntimeError)


# ============================================================================
# Persistence & Resume Tests
# ============================================================================


class TestCoordinatorPersistenceAndResume(unittest.TestCase):
    """Tests for conversation ID recording, resume support, and AGYM authoritative state."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.run_store = FileRunStore(self.tmp_dir)
        self.run_id = RunId("run-persist-test")
        self.run_store.create_run(self.run_id, "Persistence task", mode=RunMode.PLAN)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_conversation_id_recorded_immediately(self) -> None:
        """Verify conversation ID is captured and persisted as soon as known."""
        runner = FakeModelRunner(
            responses=[INITIAL_RESPONSE_DATA],
        )
        coord = CoordinatorClient(
            runner=runner,
            run_store=self.run_store,
            run_id=self.run_id,
        )

        self.assertIsNone(coord._conversation_id)
        res = coord.start("Record CID task")

        # CID must be known on coordinator instance
        self.assertIsNotNone(coord.conversation_id)
        self.assertEqual(coord.conversation_id, res.conversation_id)

        # CID must be persisted in RunStore
        info = self.run_store.get_coordinator_info(self.run_id)
        self.assertIsNotNone(info)
        self.assertEqual(info.conversation_id, coord.conversation_id)
        self.assertEqual(info.round_number, 0)
        self.assertIsNotNone(info.last_accepted_action)

    def test_resume_conversation_with_authoritative_state(self) -> None:
        """Verify resuming an existing conversation uses AGYM-authoritative RunState."""
        # 1. First coordinator session starts and records state
        runner_1 = FakeModelRunner(
            responses=[INITIAL_RESPONSE_DATA],
        )
        coord_1 = CoordinatorClient(
            runner=runner_1,
            run_store=self.run_store,
            run_id=self.run_id,
        )
        _, _, saved_cid = coord_1.start("Resume task")
        coord_1.close()

        # 2. Replacement coordinator resumes using known conversation ID
        runner_2 = FakeModelRunner(
            responses=[ROUND_1_ACTION_DATA],
        )
        coord_2 = CoordinatorClient(
            runner=runner_2,
            run_store=self.run_store,
            run_id=self.run_id,
        )

        coord_2.resume(run_id=self.run_id)
        self.assertEqual(coord_2.conversation_id, saved_cid)

        # Provide authoritative Round 1 observation
        obs = CoordinatorObservation(
            round_number=1,
            completed_results=[
                WorkerResult(worker_id=WorkerId("w-arch"), role=WorkerRole.GENERAL, response="Analysis complete")
            ],
            budget_usage=BudgetUsage(invocations=1, rounds=1),
        )

        action = coord_2.decide_action(obs)
        self.assertEqual(action.action_id, ActionId("act-round-1"))

        # Verify that prompt sent to resumed session included AGYM-authoritative header
        session_2 = coord_2._session
        self.assertIsInstance(session_2, FakeModelSession)
        sent_prompt = session_2.sent_prompts[0]
        self.assertIn("RESUMED COORDINATOR SESSION - AGYM AUTHORITATIVE STATE", sent_prompt)
        self.assertIn("Never assume model conversation memory is more authoritative than RunState", sent_prompt)
        self.assertIn(str(self.run_id), sent_prompt)

    def test_deterministic_close_and_context_manager(self) -> None:
        """Verify deterministic shutdown, closed errors, and context manager support."""
        runner = FakeModelRunner(responses=[INITIAL_RESPONSE_DATA])
        with CoordinatorClient(runner=runner, run_store=self.run_store, run_id=self.run_id) as coord:
            coord.start("Close test task")
            self.assertFalse(coord.is_closed)
            self.assertIsNotNone(coord._session)

        # After exiting context manager, client is closed
        self.assertTrue(coord.is_closed)
        self.assertIsNone(coord._session)

        # Calling decide_action or start on closed coordinator raises CoordinatorClosedError
        obs = CoordinatorObservation(round_number=1)
        with self.assertRaises(CoordinatorClosedError):
            coord.decide_action(obs)

        with self.assertRaises(CoordinatorClosedError):
            coord.start("Another task")

        with self.assertRaises(CoordinatorClosedError):
            coord.resume()


# ============================================================================
# Independence and Safety Tests
# ============================================================================


class TestCoordinatorIndependenceAndSafety(unittest.TestCase):
    """Verify zero quota usage and no dependencies on agym.council."""

    def test_no_dependencies_on_agym_council(self) -> None:
        import inspect
        import agym.orchestration.coordinator as coord_mod

        src = inspect.getsource(coord_mod)
        self.assertNotIn("agym.council", src)
        self.assertNotIn("agym/council", src)

    def test_does_not_execute_worker_actions(self) -> None:
        """Verify coordinator runtime acts only as: state/context -> model -> validated decision."""
        runner = FakeModelRunner(responses=[INITIAL_RESPONSE_DATA])
        coord = CoordinatorClient(runner=runner)
        _, action, _ = coord.start("Non-executing task")

        # The coordinator produces an action description, never executes it
        self.assertIsInstance(action, CoordinatorAction)
        self.assertEqual(len(action.workers), 1)
        # Does not have worker execution methods or launcher calls
        self.assertFalse(hasattr(coord, "execute_worker"))
        self.assertFalse(hasattr(coord, "launch_worker"))


if __name__ == "__main__":
    unittest.main()
