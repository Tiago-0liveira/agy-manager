from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.orchestration.artifacts import ArtifactWriter, safe_artifact_filename
from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    AuditRequest,
    AuditResult,
    ComplexityLevel,
    CoordinatorAction,
    CoordinatorObservation,
    EventType,
    ExecutionStrategy,
    FleetView,
    InvocationStatus,
    RunId,
    RunMode,
    RunState,
    TaskAssessment,
    TaskType,
    WorkerId,
    WorkerRequest,
    WorkerResult,
    WorkerRole,
    WorkspaceMode,
)
from agym.orchestration.coordinator import CoordinatorClient
from agym.orchestration.engine import OrchestrationEngine
from agym.orchestration.persistence import FileRunStore
from agym.orchestration.prompts import format_coordinator_observation
from agym.orchestration.protocol import parse_coordinator_action
from agym.orchestration.runner import FakeModelRunner
from agym.orchestration.streaming import extract_activity_description
from agym.orchestration.ui import TerminalEventSink
from agym.orchestration.wiring import build_budget_for_depth


class OrchestratorV2Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileRunStore(Path(self.tmp.name) / "runs")
        self.engine = OrchestrationEngine(
            scheduler=mock.Mock(),
            lease_manager=mock.Mock(),
            store=self.store,
            runner=FakeModelRunner(),
        )
        self._counter = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_state(
        self,
        complexity: ComplexityLevel,
        *,
        mode: RunMode = RunMode.PLAN,
        mutation_required: bool = False,
        value_of_auditing: float = 0.0,
    ) -> RunState:
        self._counter += 1
        rid = RunId(f"v2-{self._counter}")
        state = self.store.create_run(rid, "test task", mode=mode)
        state.assessment = TaskAssessment(
            task_type=TaskType.GENERAL,
            complexity=complexity,
            confidence=0.8,
            mutation_required=mutation_required,
            repository_scope=".",
            value_of_parallel_reasoning=1.0,
            value_of_auditing=value_of_auditing,
            summary="test",
        )
        self.store.save_run(state)
        return state


class TestQualityGates(OrchestratorV2Base):
    def _ready_high(self, state: RunState) -> None:
        q = state.quality_state
        q.independent_perspectives = 3
        q.independent_worker_ids = ["w1", "w2", "w3"]
        q.audits_completed = 1
        q.audit_worker_ids = ["a1"]
        q.synthesis_completed = 1
        q.synthesis_worker_ids = ["s1"]
        q.final_critique_completed = 1
        q.synthesis_critique_worker_ids = ["c1"]

    def test_high_cannot_finalize_without_audit(self) -> None:
        state = self.make_state(ComplexityLevel.LARGE)
        self._ready_high(state)
        state.quality_state.audits_completed = 0
        state.quality_state.audit_worker_ids = []
        missing = self.engine._check_finalization(state)
        self.assertTrue(any("audit" in item.lower() for item in missing))

    def test_high_cannot_finalize_without_synthesis_critique(self) -> None:
        state = self.make_state(ComplexityLevel.VERY_LARGE)
        self._ready_high(state)
        state.quality_state.final_critique_completed = 0
        state.quality_state.synthesis_critique_worker_ids = []
        missing = self.engine._check_finalization(state)
        self.assertTrue(any("critiqued" in item.lower() for item in missing))

    def test_medium_cannot_finalize_without_synthesis(self) -> None:
        state = self.make_state(ComplexityLevel.MEDIUM)
        state.quality_state.independent_perspectives = 2
        state.quality_state.independent_worker_ids = ["w1", "w2"]
        missing = self.engine._check_finalization(state)
        self.assertTrue(any("synthesis" in item.lower() for item in missing))

    def test_medium_audit_is_advisory_not_a_hard_gate(self) -> None:
        state = self.make_state(ComplexityLevel.MEDIUM, value_of_auditing=0.9)
        state.quality_state.independent_perspectives = 2
        state.quality_state.independent_worker_ids = ["w1", "w2"]
        state.quality_state.synthesis_completed = 1
        state.quality_state.synthesis_worker_ids = ["s1"]
        self.assertEqual(self.engine._check_finalization(state), [])

    def test_critical_findings_block_and_resolution_allows_finalization(self) -> None:
        state = self.make_state(ComplexityLevel.LARGE)
        self._ready_high(state)
        state.quality_state.open_critical_findings = ["shutdown race"]
        self.assertTrue(any("critical" in item.lower() for item in self.engine._check_finalization(state)))
        state.quality_state.open_critical_findings = []
        self.assertEqual(self.engine._check_finalization(state), [])

    def test_high_priority_disagreement_blocks_high_finalization(self) -> None:
        state = self.make_state(ComplexityLevel.LARGE)
        self._ready_high(state)
        state.quality_state.disagreements = ["ownership remains unresolved"]
        self.assertTrue(any("disagreement" in item.lower() for item in self.engine._check_finalization(state)))

    def test_finalize_rejection_is_formatted_back_to_coordinator(self) -> None:
        observation = CoordinatorObservation(
            finalization_rejection=[
                "the synthesis has not been independently critiqued",
                "1 critical finding remains unresolved",
            ],
        )
        prompt = format_coordinator_observation(observation)
        self.assertIn("FINALIZE Rejected by AGYM", prompt)
        self.assertIn("independently critiqued", prompt)
        self.assertIn("critical finding", prompt)


class TestCoordinatorV2(unittest.TestCase):
    def test_coordinator_defaults_to_high_effort(self) -> None:
        coordinator = CoordinatorClient(runner=FakeModelRunner())
        self.assertEqual(coordinator.strategy, ExecutionStrategy.HIGH_EFFORT)

    def test_action_reason_and_quality_update_parse_and_persist(self) -> None:
        action = parse_coordinator_action({
            "action_id": "a1",
            "kind": "FINALIZE",
            "reason": "Evidence is complete and all deterministic gates are satisfied.",
            "quality_update": {
                "open_questions": [],
                "disagreements": [],
                "open_critical_findings": [],
                "confidence": 0.93,
            },
            "final_response": "done",
        })
        self.assertEqual(action.reason, action.reason_summary)
        self.assertLessEqual(len(action.reason), 500)
        self.assertEqual(action.quality_update.confidence, 0.93)
        roundtrip = type(action).from_dict(action.to_dict())
        self.assertEqual(roundtrip.reason, action.reason)
        self.assertEqual(roundtrip.quality_update.open_critical_findings, [])

    def test_reason_over_500_characters_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_coordinator_action({
                "action_id": "a2",
                "kind": "FINALIZE",
                "reason": "x" * 501,
                "final_response": "done",
            })


class TestArtifactWriter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileRunStore(Path(self.tmp.name) / "runs")
        self.rid = RunId("artifact-run")
        self.store.create_run(self.rid, "artifact task")
        self.writer = ArtifactWriter(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_worker_markdown_artifact_created_with_metadata_and_safe_path(self) -> None:
        req = WorkerRequest(
            worker_id=WorkerId("../architecture"),
            role=WorkerRole.ARCHITECTURE,
            strategy=ExecutionStrategy.HIGH_EFFORT,
            objective="inspect",
        )
        result = WorkerResult(
            worker_id=req.worker_id,
            role=req.role,
            status=InvocationStatus.SUCCEEDED,
            response="Architecture result",
        )
        path = self.writer.write_result(self.rid, result, req, round_number=1)
        root = self.store.run_dir(self.rid).resolve()
        self.assertTrue(path.is_file())
        self.assertTrue(path.resolve().is_relative_to(root))
        self.assertNotIn("..", path.name)
        text = path.read_text(encoding="utf-8")
        self.assertIn("Worker: ../architecture", text)
        self.assertIn("Role: ARCHITECTURE", text)
        self.assertIn("Round: 1", text)
        self.assertIn("Strategy: HIGH_EFFORT", text)
        self.assertIn("Status: SUCCEEDED", text)
        self.assertIn("Architecture result", text)

    def test_audit_artifact_and_synthesis_versions_do_not_overwrite(self) -> None:
        audit_req = AuditRequest(
            worker_id=WorkerId("audit-1"),
            target_worker_ids=[WorkerId("w1")],
            focus="correctness",
            strategy=ExecutionStrategy.HIGH_EFFORT,
        )
        audit = AuditResult(
            worker_id=WorkerId("audit-1"),
            status=InvocationStatus.SUCCEEDED,
            response="Audit report",
        )
        audit_path = self.writer.write_result(self.rid, audit, audit_req, round_number=2)
        self.assertTrue(audit_path.is_file())

        req = WorkerRequest(
            worker_id=WorkerId("syn"),
            role=WorkerRole.SYNTHESIZER,
            objective="synthesize",
        )
        result = WorkerResult(
            worker_id=req.worker_id,
            role=req.role,
            status=InvocationStatus.SUCCEEDED,
            response="v1",
        )
        p1 = self.writer.write_result(self.rid, result, req, round_number=2, synthesis_version=1)
        result.response = "v2"
        p2 = self.writer.write_result(self.rid, result, req, round_number=3, synthesis_version=2)
        self.assertNotEqual(p1, p2)
        self.assertEqual(p1.read_text(encoding="utf-8").splitlines()[-1], "v1")
        self.assertEqual(p2.read_text(encoding="utf-8").splitlines()[-1], "v2")

    def test_final_md_created_and_persisted(self) -> None:
        state = self.store.finalize_run(self.rid, "# Final\n\nResult")
        final_path = Path(state.final_artifact_path)
        self.assertEqual(final_path.name, "final.md")
        self.assertTrue(final_path.is_file())
        self.assertEqual(final_path.read_text(encoding="utf-8"), "# Final\n\nResult\n")
        loaded = self.store.load_run(self.rid)
        self.assertEqual(loaded.final_artifact_path, state.final_artifact_path)

    def test_safe_filename_blocks_traversal(self) -> None:
        safe = safe_artifact_filename("../../../../tmp/pwn")
        self.assertNotIn("/", safe)
        self.assertNotIn("\\", safe)
        self.assertNotIn("..", safe)


class TestImplementModeGates(OrchestratorV2Base):
    def _ready_high_plan(self, state: RunState) -> None:
        q = state.quality_state
        q.independent_perspectives = 3
        q.independent_worker_ids = ["w1", "w2", "w3"]
        q.audits_completed = 1
        q.audit_worker_ids = ["plan-audit"]
        q.synthesis_completed = 1
        q.synthesis_worker_ids = ["s1"]
        q.final_critique_completed = 1
        q.synthesis_critique_worker_ids = ["critique"]

    def test_executor_success_alone_cannot_finalize_high_implementation(self) -> None:
        state = self.make_state(
            ComplexityLevel.LARGE,
            mode=RunMode.IMPLEMENT,
            mutation_required=True,
        )
        self._ready_high_plan(state)
        q = state.quality_state
        q.executor_completed = True
        q.last_executor_worker_id = "exec"
        missing = self.engine._check_finalization(state)
        self.assertTrue(any("verification" in item.lower() for item in missing))
        self.assertTrue(any("implementation audit" in item.lower() for item in missing))

    def test_latest_mutation_requires_new_verification_and_targeted_audit(self) -> None:
        state = self.make_state(
            ComplexityLevel.LARGE,
            mode=RunMode.IMPLEMENT,
            mutation_required=True,
        )
        self._ready_high_plan(state)
        q = state.quality_state
        q.executor_completed = True
        q.last_executor_worker_id = "exec-2"

        verify_action = parse_coordinator_action({
            "action_id": "verify",
            "kind": "RUN_WORKERS",
            "workers": [{
                "worker_id": "verify-1",
                "role": "TESTING",
                "workspace_mode": "READ_ONLY",
                "objective": "Verify the implementation",
            }],
        })
        verify_result = WorkerResult(
            worker_id=WorkerId("verify-1"),
            role=WorkerRole.TESTING,
            status=InvocationStatus.SUCCEEDED,
            response="verified",
        )
        self.engine._record_quality_evidence(state, verify_action, [verify_result])
        self.assertEqual(q.post_implementation_verifications, 1)

        unrelated = parse_coordinator_action({
            "action_id": "audit-unrelated",
            "kind": "RUN_AUDITORS",
            "auditors": [{
                "worker_id": "audit-u",
                "target_worker_ids": ["w1"],
                "focus": "old plan",
            }],
        }, known_worker_ids={"w1", "exec-2", "verify-1"})
        audit_result = AuditResult(
            worker_id=WorkerId("audit-u"),
            status=InvocationStatus.SUCCEEDED,
            response="ok",
        )
        self.engine._record_quality_evidence(state, unrelated, [audit_result])
        self.assertEqual(q.implementation_audits_completed, 0)

        impl_audit = parse_coordinator_action({
            "action_id": "audit-impl",
            "kind": "RUN_AUDITORS",
            "auditors": [{
                "worker_id": "audit-i",
                "target_worker_ids": ["exec-2", "verify-1"],
                "focus": "implementation correctness",
            }],
        }, known_worker_ids={"w1", "exec-2", "verify-1"})
        impl_result = AuditResult(
            worker_id=WorkerId("audit-i"),
            status=InvocationStatus.SUCCEEDED,
            response="ok",
        )
        self.engine._record_quality_evidence(state, impl_audit, [impl_result])
        self.assertEqual(q.implementation_audits_completed, 1)
        self.assertEqual(self.engine._check_finalization(state), [])

    def test_only_executor_can_mutate(self) -> None:
        mutating_analysis = WorkerRequest(
            worker_id=WorkerId("analysis"),
            role=WorkerRole.TESTING,
            workspace_mode=WorkspaceMode.MUTATING,
        )
        with self.assertRaises(ValueError):
            CoordinatorAction(
                action_id=ActionId("bad-mutation"),
                kind=ActionKind.RUN_WORKERS,
                workers=[mutating_analysis],
            )
        with self.assertRaises(ValueError):
            parse_coordinator_action({
                "action_id": "exec-readonly",
                "kind": "RUN_EXECUTOR",
                "workers": [{
                    "worker_id": "exec",
                    "role": "EXECUTOR",
                    "workspace_mode": "READ_ONLY",
                }],
            })


class TestV2Presentation(unittest.TestCase):
    def test_reason_objective_budget_activity_and_artifact_are_visible(self) -> None:
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="ui-v2")
        sink.emit(type_event("1", EventType.RUN_CREATED, {"task": "Fix lifecycle", "mode": "PLAN"}))
        sink.emit(type_event("2", EventType.TASK_ASSESSED, {
            "complexity": "LARGE",
            "coordinator_profile": "profile-3",
            "coordinator_strategy": "HIGH_EFFORT",
            "budget": {"max_invocations": 30, "max_rounds": 10},
        }))
        sink.emit(type_event("3", EventType.ROUND_STARTED, {
            "round_number": 1,
            "budget": {"max_invocations": 30, "max_rounds": 10},
            "budget_usage": {"invocations": 6, "rounds": 2, "runtime_seconds": 134},
        }))
        sink.emit(type_event("4", EventType.ACTION_REQUESTED, {
            "action": {
                "kind": "RUN_WORKERS",
                "reason": "Workers disagree about shutdown ownership.",
                "workers": [{
                    "worker_id": "failure-analysis",
                    "role": "DEBUGGING",
                    "strategy": "HIGH_EFFORT",
                    "objective": "Find lifecycle failure modes",
                }],
                "auditors": [],
            },
        }))
        sink.emit(type_event("5", EventType.INVOCATION_ACTIVITY, {
            "worker_id": "failure-analysis",
            "activity": "Inspecting lifecycle tests",
        }))
        sink.emit(type_event("6", EventType.ARTIFACT_WRITTEN, {
            "worker_id": "failure-analysis",
            "role": "DEBUGGING",
            "artifact_path": "artifacts/wave-01/debugging-failure-analysis.md",
        }))
        rendered = sink.render(use_color=False)
        self.assertIn("profile-3 · HIGH_EFFORT", rendered)
        self.assertIn("6/30 calls · 2/10 rounds", rendered)
        self.assertIn("RUN_WORKERS", rendered)
        self.assertIn("Workers disagree about shutdown ownership.", rendered)
        self.assertIn("Inspecting lifecycle tests", rendered)
        self.assertIn("Artifacts", rendered)
        self.assertIn("artifacts/wave-01/debugging-failure-analysis.md", rendered)
        self.assertIn("Task           Fix lifecycle", rendered)

    def test_non_tty_action_and_activity_are_safe_summaries(self) -> None:
        stream = io.StringIO()
        sink = TerminalEventSink(stream=stream, is_tty=False, use_color=False, run_id="ui-log")
        sink.emit(type_event("1", EventType.ACTION_REQUESTED, {
            "action": {
                "kind": "RUN_AUDITORS",
                "reason": "Review synthesis assumptions.",
                "workers": [],
                "auditors": [],
            },
        }))
        sink.emit(type_event("2", EventType.INVOCATION_ACTIVITY, {
            "worker_id": "audit-1",
            "activity": "Reviewing worker outputs",
        }))
        output = stream.getvalue()
        self.assertIn("[action] requested RUN_AUDITORS - Review synthesis assumptions.", output)
        self.assertIn("[worker] audit-1: Reviewing worker outputs", output)
        self.assertNotIn("step_update", output)

    def test_completion_does_not_dump_full_final_response(self) -> None:
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="ui-final")
        giant = "SECRET-FINAL-" + ("x" * 5000)
        sink.emit(type_event("final", EventType.RUN_COMPLETED, {
            "summary": giant,
            "final_result": giant,
            "final_artifact_path": "/tmp/run/deliverables/final.md",
        }))
        rendered = sink.render(use_color=False)
        self.assertIn("/tmp/run/deliverables/final.md", rendered)
        self.assertIn("Workers:", rendered)
        self.assertIn("Audits:", rendered)
        self.assertIn("agym orchestrate inspect ui-final", rendered)
        self.assertNotIn(giant, rendered)


class TestDepthPresets(unittest.TestCase):
    def test_depth_presets_are_hard_maxima(self) -> None:
        quick = build_budget_for_depth("quick")
        balanced = build_budget_for_depth("balanced")
        deep = build_budget_for_depth("deep")
        self.assertEqual((quick.max_parallel, quick.max_invocations, quick.max_rounds), (2, 8, 4))
        self.assertEqual((balanced.max_parallel, balanced.max_invocations, balanced.max_rounds), (4, 20, 8))
        self.assertEqual((deep.max_parallel, deep.max_invocations, deep.max_rounds), (6, 36, 12))

    def test_unknown_depth_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_budget_for_depth("infinite")


class TestStreamingActivity(unittest.TestCase):
    def test_safe_activity_is_extracted_from_supported_event(self) -> None:
        line = '{"type":"step","step_update":{"tool_info":{"display_name":"Reading","path":"coordinator.py"}}}'
        self.assertEqual(extract_activity_description(line), "Reading coordinator.py")

    def test_unknown_or_result_events_do_not_pollute_ui(self) -> None:
        self.assertIsNone(extract_activity_description('{"type":"result","message":"full response"}'))
        self.assertIsNone(extract_activity_description('{"weird":"provider-specific"}'))


def type_event(event_id: str, event_type: EventType, payload: dict) -> object:
    from agym.orchestration.contracts import OrchestrationEvent
    return OrchestrationEvent(
        event_id=event_id,
        run_id=RunId("ui-v2" if event_id != "final" else "ui-final"),
        type=event_type,
        payload=payload,
    )


if __name__ == "__main__":
    unittest.main()
