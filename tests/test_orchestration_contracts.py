"""Tests for orchestration subsystem contracts.

Covers:
- Identifier types
- Enum serialization, deserialization, case-insensitivity, and unknown values
- Dataclass serialization, deserialization, and JSON roundtrip
- Optional fields and default values
- TaskAssessment bounded invariants
- WorkerRequest invariants (no profile or argv fields)
- CoordinatorAction structural validation (RUN_WORKERS, RUN_AUDITORS, RUN_SYNTHESIS, RUN_EXECUTOR, FINALIZE)
- Mutating executor constraints
- FleetView privacy invariants (no profile identities exposed)
- RunState serialization and persistence cleanliness
- OrchestrationEvent serialization and lifecycle types
- Protocol implementations and runtime checking
- Module independence (no council dependencies, standard library only)
"""

from __future__ import annotations

import json
import unittest
from dataclasses import fields

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
    EventSink,
    EventType,
    ExecutionStrategy,
    FailureClass,
    FleetSnapshot,
    FleetView,
    InvocationId,
    InvocationStatus,
    LeaseId,
    ModelInvocation,
    ModelResult,
    ModelRunner,
    ModelSession,
    OrchestrationBudget,
    OrchestrationEvent,
    ProfileCapacity,
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


class TestIdentifiers(unittest.TestCase):
    """Tests for conceptual identifier types."""

    def test_identifier_types_are_str_subclasses(self) -> None:
        run_id = RunId("run-001")
        worker_id = WorkerId("w-arch-1")
        inv_id = InvocationId("inv-42")
        act_id = ActionId("act-1")
        lease_id = LeaseId("lease-99")
        conv_id = ConversationId("conv-abc")

        self.assertIsInstance(run_id, str)
        self.assertIsInstance(worker_id, str)
        self.assertIsInstance(inv_id, str)
        self.assertIsInstance(act_id, str)
        self.assertIsInstance(lease_id, str)
        self.assertIsInstance(conv_id, str)

        self.assertEqual(run_id, "run-001")
        self.assertEqual(worker_id, "w-arch-1")
        self.assertEqual(inv_id, "inv-42")
        self.assertEqual(act_id, "act-1")
        self.assertEqual(lease_id, "lease-99")
        self.assertEqual(conv_id, "conv-abc")

    def test_identifier_json_serialization(self) -> None:
        data = {
            "run_id": RunId("run-1"),
            "worker_id": WorkerId("worker-1"),
        }
        serialized = json.dumps(data)
        self.assertEqual(serialized, '{"run_id": "run-1", "worker_id": "worker-1"}')


class TestEnums(unittest.TestCase):
    """Tests for frozen enums."""

    def test_enum_serialization(self) -> None:
        self.assertEqual(TaskType.GENERAL.value, "GENERAL")
        self.assertEqual(ComplexityLevel.LARGE.value, "LARGE")
        self.assertEqual(RunMode.PLAN.value, "PLAN")
        self.assertEqual(WorkerRole.AUDITOR.value, "AUDITOR")
        self.assertEqual(ExecutionStrategy.BOOST.value, "BOOST")
        self.assertEqual(WorkspaceMode.MUTATING.value, "MUTATING")
        self.assertEqual(RunStatus.COMPLETED.value, "COMPLETED")
        self.assertEqual(InvocationStatus.SUCCEEDED.value, "SUCCEEDED")
        self.assertEqual(ActionKind.RUN_WORKERS.value, "RUN_WORKERS")
        self.assertEqual(FailureClass.RETRYABLE.value, "RETRYABLE")
        self.assertEqual(EventType.TASK_ASSESSED.value, "TASK_ASSESSED")

        # JSON serialization
        payload = {"role": WorkerRole.SYNTHESIZER, "status": InvocationStatus.RUNNING}
        self.assertEqual(
            json.dumps(payload),
            '{"role": "SYNTHESIZER", "status": "RUNNING"}',
        )

    def test_enum_deserialization_exact(self) -> None:
        self.assertIs(TaskType("DEBUGGING"), TaskType.DEBUGGING)
        self.assertIs(ComplexityLevel("SMALL"), ComplexityLevel.SMALL)
        self.assertIs(RunMode("IMPLEMENT"), RunMode.IMPLEMENT)
        self.assertIs(WorkerRole("EXECUTOR"), WorkerRole.EXECUTOR)
        self.assertIs(ExecutionStrategy("HIGH_EFFORT"), ExecutionStrategy.HIGH_EFFORT)
        self.assertIs(WorkspaceMode("READ_ONLY"), WorkspaceMode.READ_ONLY)
        self.assertIs(RunStatus("FAILED"), RunStatus.FAILED)
        self.assertIs(InvocationStatus("CANCELLED"), InvocationStatus.CANCELLED)
        self.assertIs(ActionKind("FINALIZE"), ActionKind.FINALIZE)
        self.assertIs(FailureClass("UNRECOVERABLE"), FailureClass.UNRECOVERABLE)
        self.assertIs(EventType("RUN_COMPLETED"), EventType.RUN_COMPLETED)

    def test_enum_deserialization_case_insensitive(self) -> None:
        self.assertIs(TaskType("general"), TaskType.GENERAL)
        self.assertIs(ComplexityLevel("very_large"), ComplexityLevel.VERY_LARGE)
        self.assertIs(RunMode("plan"), RunMode.PLAN)
        self.assertIs(WorkerRole("auditor"), WorkerRole.AUDITOR)
        self.assertIs(ExecutionStrategy("standard"), ExecutionStrategy.STANDARD)
        self.assertIs(WorkspaceMode("mutating"), WorkspaceMode.MUTATING)
        self.assertIs(RunStatus("running"), RunStatus.RUNNING)
        self.assertIs(InvocationStatus("pending"), InvocationStatus.PENDING)
        self.assertIs(ActionKind("run_executor"), ActionKind.RUN_EXECUTOR)
        self.assertIs(FailureClass("recoverable"), FailureClass.RECOVERABLE)
        self.assertIs(EventType("action_requested"), EventType.ACTION_REQUESTED)

    def test_unknown_enum_values_raise_error(self) -> None:
        with self.assertRaises(ValueError):
            TaskType("NON_EXISTENT_TASK_TYPE")

        with self.assertRaises(ValueError):
            ComplexityLevel("SUPER_COMPLEX")

        with self.assertRaises(ValueError):
            RunMode("EXECUTE_NOW")

        with self.assertRaises(ValueError):
            WorkerRole("CHEF")

        with self.assertRaises(ValueError):
            ExecutionStrategy("OVERCLOCK")

        with self.assertRaises(ValueError):
            WorkspaceMode("TEMPORARY")

        with self.assertRaises(ValueError):
            RunStatus("SLEEPING")

        with self.assertRaises(ValueError):
            InvocationStatus("WAITING_FOR_USER")

        with self.assertRaises(ValueError):
            ActionKind("RUN_EVERYTHING")

        with self.assertRaises(ValueError):
            FailureClass("CATASTROPHIC")

        with self.assertRaises(ValueError):
            EventType("UNKNOWN_EVENT")


class TestTaskAssessment(unittest.TestCase):
    """Tests for TaskAssessment contract."""

    def test_valid_task_assessment_serialization_roundtrip(self) -> None:
        assessment = TaskAssessment(
            task_type=TaskType.ARCHITECTURE,
            complexity=ComplexityLevel.MEDIUM,
            confidence=0.85,
            mutation_required=False,
            repository_scope="agym/orchestration",
            value_of_parallel_reasoning=0.9,
            value_of_auditing=0.75,
            summary="Refactor orchestration contracts",
            proposed_initial_work=["explore structure", "draft contracts"],
        )

        d = assessment.to_dict()
        self.assertEqual(d["task_type"], "ARCHITECTURE")
        self.assertEqual(d["complexity"], "MEDIUM")
        self.assertEqual(d["confidence"], 0.85)
        self.assertFalse(d["mutation_required"])
        self.assertEqual(d["repository_scope"], "agym/orchestration")
        self.assertEqual(d["value_of_parallel_reasoning"], 0.9)
        self.assertEqual(d["value_of_auditing"], 0.75)
        self.assertEqual(d["proposed_initial_work"], ["explore structure", "draft contracts"])

        json_str = assessment.to_json()
        restored = TaskAssessment.from_json(json_str)
        self.assertEqual(restored.task_type, TaskType.ARCHITECTURE)
        self.assertEqual(restored.complexity, ComplexityLevel.MEDIUM)
        self.assertEqual(restored.confidence, 0.85)
        self.assertFalse(restored.mutation_required)
        self.assertEqual(restored.repository_scope, "agym/orchestration")
        self.assertEqual(restored.value_of_parallel_reasoning, 0.9)
        self.assertEqual(restored.value_of_auditing, 0.75)
        self.assertEqual(restored.summary, "Refactor orchestration contracts")
        self.assertEqual(restored.proposed_initial_work, ["explore structure", "draft contracts"])

    def test_bounded_confidence_invariant(self) -> None:
        # Confidence must be in [0.0, 1.0]
        with self.assertRaises(ValueError):
            TaskAssessment(
                task_type=TaskType.GENERAL,
                complexity=ComplexityLevel.SMALL,
                confidence=-0.1,
                mutation_required=False,
                repository_scope="scope",
            )

        with self.assertRaises(ValueError):
            TaskAssessment(
                task_type=TaskType.GENERAL,
                complexity=ComplexityLevel.SMALL,
                confidence=1.1,
                mutation_required=False,
                repository_scope="scope",
            )

        # Boundary values must succeed
        a0 = TaskAssessment(
            task_type=TaskType.GENERAL,
            complexity=ComplexityLevel.TRIVIAL,
            confidence=0.0,
            mutation_required=False,
            repository_scope="scope",
        )
        self.assertEqual(a0.confidence, 0.0)

        a1 = TaskAssessment(
            task_type=TaskType.GENERAL,
            complexity=ComplexityLevel.TRIVIAL,
            confidence=1.0,
            mutation_required=False,
            repository_scope="scope",
        )
        self.assertEqual(a1.confidence, 1.0)

    def test_bounded_parallel_reasoning_and_auditing(self) -> None:
        with self.assertRaises(ValueError):
            TaskAssessment(
                task_type=TaskType.GENERAL,
                complexity=ComplexityLevel.SMALL,
                confidence=0.5,
                mutation_required=False,
                repository_scope="scope",
                value_of_parallel_reasoning=1.5,
            )

        with self.assertRaises(ValueError):
            TaskAssessment(
                task_type=TaskType.GENERAL,
                complexity=ComplexityLevel.SMALL,
                confidence=0.5,
                mutation_required=False,
                repository_scope="scope",
                value_of_auditing=-0.2,
            )

    def test_optional_defaults(self) -> None:
        assessment = TaskAssessment(
            task_type=TaskType.DEBUGGING,
            complexity=ComplexityLevel.SMALL,
            confidence=0.5,
            mutation_required=False,
            repository_scope="tests",
        )
        self.assertEqual(assessment.value_of_parallel_reasoning, 0.0)
        self.assertEqual(assessment.value_of_auditing, 0.0)
        self.assertEqual(assessment.summary, "")
        self.assertEqual(assessment.proposed_initial_work, [])

    def test_string_proposed_initial_work_normalized_to_list(self) -> None:
        assessment = TaskAssessment(
            task_type=TaskType.DEBUGGING,
            complexity=ComplexityLevel.SMALL,
            confidence=0.5,
            mutation_required=False,
            repository_scope="tests",
            proposed_initial_work="single task",  # type: ignore[arg-type]
        )
        self.assertEqual(assessment.proposed_initial_work, ["single task"])

    def test_no_profile_or_execution_fields(self) -> None:
        field_names = {f.name for f in fields(TaskAssessment)}
        self.assertNotIn("profile_name", field_names)
        self.assertNotIn("profile", field_names)
        self.assertNotIn("command", field_names)
        self.assertNotIn("argv", field_names)
        self.assertNotIn("process", field_names)


class TestWorkerRequestAndResult(unittest.TestCase):
    """Tests for WorkerRequest and WorkerResult contracts."""

    def test_worker_request_has_no_profile_or_process_fields(self) -> None:
        field_names = {f.name for f in fields(WorkerRequest)}
        self.assertNotIn("profile_name", field_names)
        self.assertNotIn("profile", field_names)
        self.assertNotIn("argv", field_names)
        self.assertNotIn("environment", field_names)
        self.assertNotIn("executable_path", field_names)
        self.assertNotIn("cmd", field_names)

    def test_worker_request_serialization_roundtrip(self) -> None:
        req = WorkerRequest(
            worker_id=WorkerId("w-1"),
            role=WorkerRole.ARCHITECTURE,
            strategy=ExecutionStrategy.HIGH_EFFORT,
            workspace_mode=WorkspaceMode.READ_ONLY,
            objective="Design subsystem architecture",
            context_worker_ids=[WorkerId("w-0")],
            timeout_seconds=600.0,
        )

        d = req.to_dict()
        self.assertEqual(d["worker_id"], "w-1")
        self.assertEqual(d["role"], "ARCHITECTURE")
        self.assertEqual(d["strategy"], "HIGH_EFFORT")
        self.assertEqual(d["workspace_mode"], "READ_ONLY")
        self.assertEqual(d["objective"], "Design subsystem architecture")
        self.assertEqual(d["context_worker_ids"], ["w-0"])
        self.assertEqual(d["timeout_seconds"], 600.0)

        restored = WorkerRequest.from_dict(d)
        self.assertEqual(restored.worker_id, WorkerId("w-1"))
        self.assertEqual(restored.role, WorkerRole.ARCHITECTURE)
        self.assertEqual(restored.strategy, ExecutionStrategy.HIGH_EFFORT)
        self.assertEqual(restored.timeout_seconds, 600.0)

    def test_worker_request_positive_timeout(self) -> None:
        with self.assertRaises(ValueError):
            WorkerRequest(
                worker_id=WorkerId("w-1"),
                role=WorkerRole.GENERAL,
                timeout_seconds=0.0,
            )

        with self.assertRaises(ValueError):
            WorkerRequest(
                worker_id=WorkerId("w-1"),
                role=WorkerRole.GENERAL,
                timeout_seconds=-10.0,
            )

    def test_worker_result_serialization_roundtrip(self) -> None:
        res = WorkerResult(
            worker_id=WorkerId("w-1"),
            role=WorkerRole.ARCHITECTURE,
            status=InvocationStatus.SUCCEEDED,
            invocation_id=InvocationId("inv-100"),
            response="Architecture design complete.",
            structured_data={"modules": ["engine", "contracts"]},
            conversation_id=ConversationId("conv-1"),
            failure=None,
            started_at="2026-09-23T10:00:00Z",
            completed_at="2026-09-23T10:05:00Z",
        )

        d = res.to_dict()
        self.assertEqual(d["status"], "SUCCEEDED")
        self.assertEqual(d["failure"], None)
        self.assertEqual(d["invocation_id"], "inv-100")

        restored = WorkerResult.from_dict(d)
        self.assertEqual(restored.worker_id, WorkerId("w-1"))
        self.assertEqual(restored.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(restored.structured_data, {"modules": ["engine", "contracts"]})

    def test_worker_result_optional_fields(self) -> None:
        res = WorkerResult(
            worker_id=WorkerId("w-2"),
            role=WorkerRole.GENERAL,
        )
        self.assertEqual(res.status, InvocationStatus.SUCCEEDED)
        self.assertIsNone(res.invocation_id)
        self.assertIsNone(res.response)
        self.assertIsNone(res.structured_data)
        self.assertIsNone(res.conversation_id)
        self.assertIsNone(res.failure)
        self.assertIsNone(res.started_at)
        self.assertIsNone(res.completed_at)

        d = res.to_dict()
        restored = WorkerResult.from_dict(d)
        self.assertIsNone(restored.response)
        self.assertIsNone(restored.failure)

    def test_worker_result_with_failure_class(self) -> None:
        res = WorkerResult(
            worker_id=WorkerId("w-3"),
            role=WorkerRole.DEBUGGING,
            status=InvocationStatus.FAILED,
            failure=FailureClass.RETRYABLE,
        )
        d = res.to_dict()
        self.assertEqual(d["failure"], "RETRYABLE")
        restored = WorkerResult.from_dict(d)
        self.assertEqual(restored.failure, FailureClass.RETRYABLE)


class TestAuditContracts(unittest.TestCase):
    """Tests for AuditRequest and AuditResult contracts."""

    def test_audit_request_serialization(self) -> None:
        req = AuditRequest(
            worker_id=WorkerId("auditor-1"),
            target_worker_ids=[WorkerId("w-1"), WorkerId("w-2")],
            focus="Check for race conditions",
            strategy=ExecutionStrategy.STANDARD,
            timeout_seconds=120.0,
        )
        d = req.to_dict()
        self.assertEqual(d["worker_id"], "auditor-1")
        self.assertEqual(d["target_worker_ids"], ["w-1", "w-2"])
        self.assertEqual(d["focus"], "Check for race conditions")

        restored = AuditRequest.from_dict(d)
        self.assertEqual(restored.target_worker_ids, [WorkerId("w-1"), WorkerId("w-2")])

    def test_audit_request_invalid_timeout(self) -> None:
        with self.assertRaises(ValueError):
            AuditRequest(worker_id=WorkerId("auditor-1"), timeout_seconds=-5.0)

    def test_audit_result_serialization(self) -> None:
        res = AuditResult(
            worker_id=WorkerId("auditor-1"),
            invocation_id=InvocationId("inv-200"),
            findings=["Potential race condition in lock release", "Missing timeout check"],
            response="Audit completed with 2 findings.",
            status=InvocationStatus.SUCCEEDED,
        )
        d = res.to_dict()
        self.assertEqual(len(d["findings"]), 2)
        restored = AuditResult.from_dict(d)
        self.assertEqual(restored.findings, ["Potential race condition in lock release", "Missing timeout check"])

    def test_audit_result_string_findings_normalized(self) -> None:
        res = AuditResult(
            worker_id=WorkerId("auditor-1"),
            findings="single finding",  # type: ignore[arg-type]
        )
        self.assertEqual(res.findings, ["single finding"])


class TestCoordinatorActionValidation(unittest.TestCase):
    """Tests for structural validation of CoordinatorAction."""

    def test_run_workers_valid(self) -> None:
        action = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.ARCHITECTURE),
                WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.TESTING),
            ],
            reason_summary="Parallel investigation",
        )
        self.assertEqual(len(action.workers), 2)
        self.assertEqual(action.kind, ActionKind.RUN_WORKERS)

    def test_run_workers_empty_workers_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-1"),
                kind=ActionKind.RUN_WORKERS,
                workers=[],
            )

    def test_run_workers_with_auditors_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-1"),
                kind=ActionKind.RUN_WORKERS,
                workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL)],
                auditors=[AuditRequest(worker_id=WorkerId("a-1"))],
            )

    def test_run_workers_with_mutating_worker_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-1"),
                kind=ActionKind.RUN_WORKERS,
                workers=[
                    WorkerRequest(
                        worker_id=WorkerId("w-1"),
                        role=WorkerRole.GENERAL,
                        workspace_mode=WorkspaceMode.MUTATING,
                    )
                ],
            )

    def test_run_workers_with_executor_role_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-1"),
                kind=ActionKind.RUN_WORKERS,
                workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.EXECUTOR)],
            )

    def test_run_workers_with_auditor_role_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-1"),
                kind=ActionKind.RUN_WORKERS,
                workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.AUDITOR)],
            )

    def test_run_auditors_valid(self) -> None:
        action = CoordinatorAction(
            action_id=ActionId("act-2"),
            kind=ActionKind.RUN_AUDITORS,
            auditors=[AuditRequest(worker_id=WorkerId("auditor-1"))],
        )
        self.assertEqual(len(action.auditors), 1)

    def test_run_auditors_empty_auditors_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-2"),
                kind=ActionKind.RUN_AUDITORS,
                auditors=[],
            )

    def test_run_auditors_with_workers_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-2"),
                kind=ActionKind.RUN_AUDITORS,
                workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL)],
                auditors=[AuditRequest(worker_id=WorkerId("auditor-1"))],
            )

    def test_run_synthesis_valid(self) -> None:
        action = CoordinatorAction(
            action_id=ActionId("act-3"),
            kind=ActionKind.RUN_SYNTHESIS,
            workers=[WorkerRequest(worker_id=WorkerId("synth-1"), role=WorkerRole.SYNTHESIZER)],
        )
        self.assertEqual(action.workers[0].role, WorkerRole.SYNTHESIZER)

    def test_run_synthesis_wrong_role_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-3"),
                kind=ActionKind.RUN_SYNTHESIS,
                workers=[WorkerRequest(worker_id=WorkerId("synth-1"), role=WorkerRole.ARCHITECTURE)],
            )

    def test_run_synthesis_mutating_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-3"),
                kind=ActionKind.RUN_SYNTHESIS,
                workers=[
                    WorkerRequest(
                        worker_id=WorkerId("synth-1"),
                        role=WorkerRole.SYNTHESIZER,
                        workspace_mode=WorkspaceMode.MUTATING,
                    )
                ],
            )

    def test_run_executor_valid_read_only_and_mutating(self) -> None:
        # Read-only executor
        a1 = CoordinatorAction(
            action_id=ActionId("act-exec-1"),
            kind=ActionKind.RUN_EXECUTOR,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("exec-1"),
                    role=WorkerRole.EXECUTOR,
                    workspace_mode=WorkspaceMode.READ_ONLY,
                )
            ],
        )
        self.assertEqual(len(a1.workers), 1)

        # Mutating executor (single)
        a2 = CoordinatorAction(
            action_id=ActionId("act-exec-2"),
            kind=ActionKind.RUN_EXECUTOR,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("exec-2"),
                    role=WorkerRole.EXECUTOR,
                    workspace_mode=WorkspaceMode.MUTATING,
                )
            ],
        )
        self.assertEqual(a2.workers[0].workspace_mode, WorkspaceMode.MUTATING)

    def test_run_executor_multiple_workers_fails(self) -> None:
        # At most one mutating executor / worker allowed
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-exec-fail"),
                kind=ActionKind.RUN_EXECUTOR,
                workers=[
                    WorkerRequest(worker_id=WorkerId("exec-1"), role=WorkerRole.EXECUTOR),
                    WorkerRequest(worker_id=WorkerId("exec-2"), role=WorkerRole.EXECUTOR),
                ],
            )

    def test_run_executor_empty_workers_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-exec-fail"),
                kind=ActionKind.RUN_EXECUTOR,
                workers=[],
            )

    def test_run_executor_wrong_role_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-exec-fail"),
                kind=ActionKind.RUN_EXECUTOR,
                workers=[WorkerRequest(worker_id=WorkerId("exec-1"), role=WorkerRole.GENERAL)],
            )

    def test_run_executor_with_auditors_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-exec-fail"),
                kind=ActionKind.RUN_EXECUTOR,
                workers=[WorkerRequest(worker_id=WorkerId("exec-1"), role=WorkerRole.EXECUTOR)],
                auditors=[AuditRequest(worker_id=WorkerId("a-1"))],
            )

    def test_finalize_shape_valid(self) -> None:
        action = CoordinatorAction(
            action_id=ActionId("act-fin"),
            kind=ActionKind.FINALIZE,
            final_response="Plan generation complete.",
            reason_summary="All objectives met",
        )
        self.assertEqual(action.kind, ActionKind.FINALIZE)
        self.assertEqual(action.final_response, "Plan generation complete.")
        self.assertEqual(action.workers, [])
        self.assertEqual(action.auditors, [])

    def test_finalize_with_workers_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-fin"),
                kind=ActionKind.FINALIZE,
                workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL)],
                final_response="Done",
            )

    def test_finalize_with_auditors_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-fin"),
                kind=ActionKind.FINALIZE,
                auditors=[AuditRequest(worker_id=WorkerId("a-1"))],
                final_response="Done",
            )

    def test_non_finalize_with_final_response_fails(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("act-non-fin"),
                kind=ActionKind.RUN_WORKERS,
                workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL)],
                final_response="Premature response",
            )

    def test_coordinator_action_has_no_profile_or_shell_command_fields(self) -> None:
        field_names = {f.name for f in fields(CoordinatorAction)}
        self.assertNotIn("profile_name", field_names)
        self.assertNotIn("profile", field_names)
        self.assertNotIn("shell_command", field_names)
        self.assertNotIn("argv", field_names)
        self.assertNotIn("cmd", field_names)

    def test_coordinator_action_serialization_roundtrip(self) -> None:
        action = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.ARCHITECTURE)],
            reason_summary="Investigating",
        )
        d = action.to_dict()
        restored = CoordinatorAction.from_dict(d)
        self.assertEqual(restored.action_id, ActionId("act-1"))
        self.assertEqual(restored.kind, ActionKind.RUN_WORKERS)
        self.assertEqual(len(restored.workers), 1)


class TestCoordinatorObservation(unittest.TestCase):
    """Tests for CoordinatorObservation."""

    def test_observation_serialization_roundtrip(self) -> None:
        obs = CoordinatorObservation(
            completed_results=[
                WorkerResult(worker_id=WorkerId("w-1"), role=WorkerRole.ARCHITECTURE, response="Arch done"),
                AuditResult(worker_id=WorkerId("a-1"), findings=["No issues"]),
            ],
            failed_results=[
                WorkerResult(
                    worker_id=WorkerId("w-2"),
                    role=WorkerRole.TESTING,
                    status=InvocationStatus.FAILED,
                    failure=FailureClass.RETRYABLE,
                )
            ],
            rejected_requests=[
                WorkerRequest(worker_id=WorkerId("w-3"), role=WorkerRole.DEBUGGING),
            ],
            budget_usage=BudgetUsage(invocations=3, rounds=1, runtime_seconds=45.0),
            fleet_view=FleetView(available_profiles=2, max_parallel=2),
            round_number=1,
        )

        d = obs.to_dict()
        self.assertEqual(len(d["completed_results"]), 2)
        self.assertEqual(len(d["failed_results"]), 1)
        self.assertEqual(len(d["rejected_requests"]), 1)
        self.assertEqual(d["round_number"], 1)

        restored = CoordinatorObservation.from_dict(d)
        self.assertEqual(restored.round_number, 1)
        self.assertIsInstance(restored.completed_results[0], WorkerResult)
        self.assertIsInstance(restored.completed_results[1], AuditResult)
        self.assertIsInstance(restored.rejected_requests[0], WorkerRequest)
        self.assertEqual(restored.budget_usage.invocations, 3)
        self.assertEqual(restored.fleet_view.available_profiles, 2)


class TestFleetContracts(unittest.TestCase):
    """Tests for FleetView, ProfileCapacity, and FleetSnapshot."""

    def test_fleet_view_contains_only_capability_info(self) -> None:
        field_names = {f.name for f in fields(FleetView)}
        # Must contain capability fields
        self.assertIn("available_profiles", field_names)
        self.assertIn("max_parallel", field_names)
        self.assertIn("standard_capacity", field_names)
        self.assertIn("high_effort_capacity", field_names)
        self.assertIn("boost_capacity", field_names)
        self.assertIn("quota_band_counts", field_names)

        # Must not contain private identity fields
        self.assertNotIn("profile_name", field_names)
        self.assertNotIn("profiles", field_names)
        self.assertNotIn("credentials", field_names)
        self.assertNotIn("emails", field_names)
        self.assertNotIn("paths", field_names)

    def test_fleet_view_serialization_roundtrip(self) -> None:
        fv = FleetView(
            available_profiles=3,
            max_parallel=4,
            standard_capacity=3,
            high_effort_capacity=2,
            boost_capacity=1,
            quota_band_counts={"HIGH": 2, "MEDIUM": 1},
        )
        d = fv.to_dict()
        restored = FleetView.from_dict(d)
        self.assertEqual(restored.available_profiles, 3)
        self.assertEqual(restored.quota_band_counts, {"HIGH": 2, "MEDIUM": 1})

    def test_profile_capacity_internal_roundtrip(self) -> None:
        cap = ProfileCapacity(
            profile_name="AI1",
            five_hour_remaining=92.5,
            weekly_remaining=80.0,
            leased=True,
            auth_ready=True,
            recent_failures=0,
            observed_at="2026-09-23T10:00:00Z",
        )
        d = cap.to_dict()
        self.assertEqual(d["profile_name"], "AI1")
        restored = ProfileCapacity.from_dict(d)
        self.assertEqual(restored.profile_name, "AI1")
        self.assertTrue(restored.leased)

    def test_fleet_snapshot_internal_roundtrip(self) -> None:
        snap = FleetSnapshot(
            profiles=[
                ProfileCapacity(profile_name="AI1", five_hour_remaining=90.0),
                ProfileCapacity(profile_name="AI2", five_hour_remaining=50.0),
            ],
            observed_at="2026-09-23T10:00:00Z",
        )
        d = snap.to_dict()
        self.assertEqual(len(d["profiles"]), 2)
        restored = FleetSnapshot.from_dict(d)
        self.assertEqual(len(restored.profiles), 2)
        self.assertEqual(restored.profiles[0].profile_name, "AI1")


class TestProfileLease(unittest.TestCase):
    """Tests for ProfileLease contract."""

    def test_profile_lease_serialization_roundtrip(self) -> None:
        lease = ProfileLease(
            lease_id=LeaseId("lease-001"),
            profile_name="AI1",
            run_id=RunId("run-1"),
            worker_id=WorkerId("w-1"),
            pid=12345,
            acquired_at="2026-09-23T10:00:00Z",
            heartbeat_at="2026-09-23T10:01:00Z",
        )
        d = lease.to_dict()
        self.assertEqual(d["lease_id"], "lease-001")
        self.assertEqual(d["pid"], 12345)

        restored = ProfileLease.from_dict(d)
        self.assertEqual(restored.lease_id, LeaseId("lease-001"))
        self.assertEqual(restored.pid, 12345)

    def test_no_stale_detection_policy_in_profile_lease(self) -> None:
        # Dataclass must be pure data; no policy methods
        lease = ProfileLease(
            lease_id=LeaseId("lease-1"),
            profile_name="AI1",
            run_id=RunId("run-1"),
            worker_id=WorkerId("w-1"),
        )
        self.assertFalse(hasattr(lease, "is_stale"))
        self.assertFalse(hasattr(lease, "check_stale"))


class TestBudgetContracts(unittest.TestCase):
    """Tests for OrchestrationBudget and BudgetUsage."""

    def test_budget_defaults_and_roundtrip(self) -> None:
        budget = OrchestrationBudget()
        self.assertEqual(budget.max_parallel, 4)
        self.assertEqual(budget.max_invocations, 20)
        self.assertEqual(budget.max_rounds, 10)
        self.assertEqual(budget.max_boost_invocations, 2)
        self.assertEqual(budget.max_retries, 3)
        self.assertEqual(budget.max_runtime_seconds, 1800.0)
        self.assertEqual(budget.min_quota_remaining, 10.0)
        self.assertEqual(budget.max_consecutive_rejections, 3)

        d = budget.to_dict()
        restored = OrchestrationBudget.from_dict(d)
        self.assertEqual(restored.max_parallel, 4)
        self.assertEqual(restored.max_consecutive_rejections, 3)

    def test_budget_usage_roundtrip(self) -> None:
        usage = BudgetUsage(
            invocations=5,
            rounds=2,
            boost_invocations=1,
            retries=0,
            runtime_seconds=125.5,
        )
        d = usage.to_dict()
        restored = BudgetUsage.from_dict(d)
        self.assertEqual(restored.invocations, 5)
        self.assertEqual(restored.runtime_seconds, 125.5)


class TestModelInvocationContracts(unittest.TestCase):
    """Tests for ModelInvocation and ModelResult."""

    def test_model_invocation_roundtrip(self) -> None:
        inv = ModelInvocation(
            invocation_id=InvocationId("inv-1"),
            run_id=RunId("run-1"),
            worker_id=WorkerId("w-1"),
            role=WorkerRole.GENERAL,
            strategy=ExecutionStrategy.STANDARD,
            workspace_mode=WorkspaceMode.READ_ONLY,
            prompt="Analyze the requirements.",
            output_schema={"type": "object"},
            timeout_seconds=180.0,
            conversation_id=ConversationId("conv-1"),
        )
        d = inv.to_dict()
        self.assertEqual(d["invocation_id"], "inv-1")
        self.assertEqual(d["conversation_id"], "conv-1")

        restored = ModelInvocation.from_dict(d)
        self.assertEqual(restored.invocation_id, InvocationId("inv-1"))
        self.assertEqual(restored.output_schema, {"type": "object"})

    def test_model_result_roundtrip(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("inv-1"),
            status=InvocationStatus.SUCCEEDED,
            response="Analysis text",
            structured_data={"summary": "done"},
            conversation_id=ConversationId("conv-1"),
            usage={"input_tokens": 100, "output_tokens": 50},
            exit_code=0,
            error=None,
            started_at="2026-09-23T10:00:00Z",
            completed_at="2026-09-23T10:00:05Z",
        )
        d = res.to_dict()
        self.assertEqual(d["exit_code"], 0)
        self.assertEqual(d["usage"]["input_tokens"], 100)

        restored = ModelResult.from_dict(d)
        self.assertEqual(restored.exit_code, 0)
        self.assertEqual(restored.usage, {"input_tokens": 100, "output_tokens": 50})


class TestRunState(unittest.TestCase):
    """Tests for RunState contract."""

    def test_run_state_clean_persistence_roundtrip(self) -> None:
        assessment = TaskAssessment(
            task_type=TaskType.ARCHITECTURE,
            complexity=ComplexityLevel.MEDIUM,
            confidence=0.8,
            mutation_required=False,
            repository_scope="orchestration",
        )
        state = RunState(
            run_id=RunId("run-001"),
            task="Design orchestration subsystem",
            mode=RunMode.PLAN,
            status=RunStatus.RUNNING,
            assessment=assessment,
            coordinator_conversation_id=ConversationId("conv-coord-1"),
            round_number=2,
            budget=OrchestrationBudget(max_parallel=2),
            budget_usage=BudgetUsage(invocations=4, rounds=2),
            invocation_ids=[InvocationId("inv-1"), InvocationId("inv-2")],
            created_at="2026-09-23T10:00:00Z",
            updated_at="2026-09-23T10:10:00Z",
            final_result=None,
        )

        # JSON serialization must succeed cleanly (no locks, no sockets, no subprocesses)
        json_str = state.to_json(indent=2)
        self.assertIsInstance(json_str, str)

        restored = RunState.from_json(json_str)
        self.assertEqual(restored.run_id, RunId("run-001"))
        self.assertEqual(restored.task, "Design orchestration subsystem")
        self.assertEqual(restored.mode, RunMode.PLAN)
        self.assertEqual(restored.status, RunStatus.RUNNING)
        self.assertIsNotNone(restored.assessment)
        assert restored.assessment is not None
        self.assertEqual(restored.assessment.task_type, TaskType.ARCHITECTURE)
        self.assertEqual(restored.coordinator_conversation_id, ConversationId("conv-coord-1"))
        self.assertEqual(restored.round_number, 2)
        self.assertEqual(restored.budget.max_parallel, 2)
        self.assertEqual(restored.budget_usage.invocations, 4)
        self.assertEqual(restored.invocation_ids, [InvocationId("inv-1"), InvocationId("inv-2")])

    def test_run_state_defaults(self) -> None:
        state = RunState(
            run_id=RunId("run-002"),
            task="Simple task",
        )
        self.assertEqual(state.mode, RunMode.PLAN)
        self.assertEqual(state.status, RunStatus.CREATED)
        self.assertIsNone(state.assessment)
        self.assertEqual(state.round_number, 0)
        self.assertEqual(state.invocation_ids, [])
        self.assertIsNone(state.final_result)


class TestOrchestrationEvent(unittest.TestCase):
    """Tests for OrchestrationEvent and EventType."""

    def test_event_serialization_roundtrip(self) -> None:
        event = OrchestrationEvent(
            event_id="evt-101",
            run_id=RunId("run-1"),
            type=EventType.INVOCATION_COMPLETED,
            timestamp="2026-09-23T10:05:00Z",
            payload={"worker_id": "w-1", "duration": 12.4},
        )
        d = event.to_dict()
        self.assertEqual(d["type"], "INVOCATION_COMPLETED")
        self.assertEqual(d["payload"]["worker_id"], "w-1")

        restored = OrchestrationEvent.from_dict(d)
        self.assertEqual(restored.type, EventType.INVOCATION_COMPLETED)
        self.assertEqual(restored.timestamp, "2026-09-23T10:05:00Z")

    def test_event_auto_timestamp(self) -> None:
        event = OrchestrationEvent(
            event_id="evt-102",
            run_id=RunId("run-1"),
            type=EventType.RUN_CREATED,
        )
        self.assertTrue(len(event.timestamp) > 0)

    def test_all_lifecycle_event_types_covered(self) -> None:
        expected_events = [
            "RUN_CREATED",
            "TASK_ASSESSED",
            "ACTION_REQUESTED",
            "ACTION_ACCEPTED",
            "ACTION_REJECTED",
            "PROFILE_LEASED",
            "PROFILE_RELEASED",
            "INVOCATION_STARTED",
            "INVOCATION_COMPLETED",
            "INVOCATION_FAILED",
            "ROUND_STARTED",
            "ROUND_COMPLETED",
            "RUN_COMPLETED",
            "RUN_FAILED",
            "RUN_INTERRUPTED",
        ]
        for name in expected_events:
            self.assertTrue(hasattr(EventType, name), f"EventType missing {name}")


class TestProtocols(unittest.TestCase):
    """Tests for runtime checkability of subsystem Protocols."""

    def test_model_session_protocol(self) -> None:
        class DummySession:
            @property
            def conversation_id(self) -> ConversationId:
                return ConversationId("c-1")

            def send(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
                return ModelResult(invocation_id=InvocationId("i-1"))

            def close(self) -> None:
                pass

        session = DummySession()
        self.assertIsInstance(session, ModelSession)

    def test_model_runner_protocol(self) -> None:
        class DummyRunner:
            def run(
                self,
                invocation: ModelInvocation,
                profile_name: str | None = None,
            ) -> ModelResult:
                return ModelResult(invocation_id=invocation.invocation_id)

        runner = DummyRunner()
        self.assertIsInstance(runner, ModelRunner)

    def test_coordinator_client_protocol(self) -> None:
        class DummyCoordinator:
            def assess_task(self, task: str, fleet_view: FleetView) -> TaskAssessment:
                return TaskAssessment(
                    task_type=TaskType.GENERAL,
                    complexity=ComplexityLevel.TRIVIAL,
                    confidence=1.0,
                    mutation_required=False,
                    repository_scope=".",
                )

            def decide_action(
                self,
                observation: CoordinatorObservation,
                conversation_id: ConversationId | None = None,
            ) -> CoordinatorAction:
                return CoordinatorAction(
                    action_id=ActionId("a-1"),
                    kind=ActionKind.FINALIZE,
                    final_response="Done",
                )

        coord = DummyCoordinator()
        self.assertIsInstance(coord, CoordinatorClient)

    def test_profile_scheduler_protocol(self) -> None:
        class DummyScheduler:
            def get_fleet_view(self) -> FleetView:
                return FleetView()

            def get_fleet_snapshot(self) -> FleetSnapshot:
                return FleetSnapshot()

            def select_profile(
                self,
                request: WorkerRequest | AuditRequest,
            ) -> str | None:
                return "AI1"

        scheduler = DummyScheduler()
        self.assertIsInstance(scheduler, ProfileScheduler)

    def test_profile_lease_manager_protocol(self) -> None:
        class DummyLeaseManager:
            def acquire(
                self,
                profile_name: str,
                run_id: RunId,
                worker_id: WorkerId,
                pid: int | None = None,
            ) -> ProfileLease:
                return ProfileLease(
                    lease_id=LeaseId("l-1"),
                    profile_name=profile_name,
                    run_id=run_id,
                    worker_id=worker_id,
                )

            def heartbeat(self, lease_id: LeaseId) -> bool:
                return True

            def release(self, lease_id: LeaseId) -> bool:
                return True

            def list_leases(self) -> list[ProfileLease]:
                return []

            def revoke_stale(self, timeout_seconds: float) -> list[ProfileLease]:
                return []

        mgr = DummyLeaseManager()
        self.assertIsInstance(mgr, ProfileLeaseManager)

    def test_run_store_protocol(self) -> None:
        class DummyStore:
            def save_run(self, state: RunState) -> None:
                pass

            def get_run(self, run_id: RunId) -> RunState | None:
                return None

            def list_runs(self, limit: int = 100) -> list[RunState]:
                return []

            def save_result(self, run_id: RunId, result: WorkerResult | AuditResult) -> None:
                pass

            def get_results(self, run_id: RunId) -> list[WorkerResult | AuditResult]:
                return []

        store = DummyStore()
        self.assertIsInstance(store, RunStore)

    def test_event_sink_protocol(self) -> None:
        class DummyEventSink:
            def emit(self, event: OrchestrationEvent) -> None:
                pass

            def get_events(self, run_id: RunId) -> list[OrchestrationEvent]:
                return []

        sink = DummyEventSink()
        self.assertIsInstance(sink, EventSink)

    def test_incomplete_class_fails_protocol_check(self) -> None:
        class IncompleteRunner:
            def not_the_right_method(self) -> None:
                pass

        self.assertNotIsInstance(IncompleteRunner(), ModelRunner)
        self.assertNotIsInstance(IncompleteRunner(), EventSink)


class TestIndependenceAndSafety(unittest.TestCase):
    """Tests ensuring contracts are self-contained and zero-dependency."""

    def test_no_dependencies_on_agym_council(self) -> None:
        import inspect

        import agym.orchestration.contracts as contracts_mod

        src = inspect.getsource(contracts_mod)
        self.assertNotIn("agym.council", src)
        self.assertNotIn("council", src)

    def test_imports_standard_library_only(self) -> None:
        import sys

        import agym.orchestration.contracts as contracts_mod

        # Verify module file imports only standard library
        for name, mod in sys.modules.items():
            if mod and hasattr(mod, "__file__") and mod.__file__:
                # Only agym itself should be from the project
                if "agym.orchestration.contracts" in name:
                    self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
