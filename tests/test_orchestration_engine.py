"""Comprehensive tests for the AGYM deterministic orchestration engine.

Tests all 24 required scenarios using zero real Gemini quota:
1. trivial one-worker task
2. 2 parallel workers
3. 4 parallel workers
4. auditor after workers
5. adaptive second worker wave
6. synthesis
7. executor
8. worker failure
9. worker timeout
10. quota failure
11. scheduler insufficient capacity
12. retry on another profile
13. budget max_parallel rejection
14. max_invocations rejection
15. max_rounds rejection
16. boost limit rejection
17. invalid coordinator action
18. coordinator crash
19. Ctrl+C
20. leases always released
21. dry run launches no workers
22. resume after interrupted worker
23. resume after coordinator crash
24. finalization
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Sequence
import unittest
import uuid

from agym.cache import CacheManager
from agym.profiles import ProfileStore
from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    AuditRequest,
    AuditResult,
    ComplexityLevel,
    ConversationId,
    CoordinatorAction,
    CoordinatorObservation,
    EventType,
    ExecutionStrategy,
    FailureClass,
    FleetView,
    InvocationId,
    InvocationStatus,
    ModelInvocation,
    ModelResult,
    OrchestrationBudget,
    ProfileLease,
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
from agym.orchestration.engine import (
    DryRunPlan,
    EngineError,
    OrchestrationEngine,
    build_auditor_prompt,
    build_worker_prompt,
)
from agym.orchestration.leases import ProfileLeaseManager
from agym.orchestration.persistence import FileRunStore
from agym.orchestration.runner import FakeModelRunner
from agym.orchestration.scheduler import (
    InsufficientCapacityError,
    ProfileScheduler,
)


# ============================================================================
# Test Fixtures & Scriptable Fakes
# ============================================================================


class FakeCoordinator:
    """Deterministic scriptable coordinator for engine testing."""

    def __init__(
        self,
        assessment: TaskAssessment | None = None,
        actions: Sequence[CoordinatorAction | Exception] = (),
        conversation_id: str = "conv-fake-coord",
    ) -> None:
        self.assessment = assessment or TaskAssessment(
            task_type=TaskType.GENERAL,
            complexity=ComplexityLevel.SMALL,
            confidence=0.95,
            mutation_required=False,
            repository_scope=".",
            summary="Assessment summary for test",
        )
        self.actions: list[CoordinatorAction | Exception] = list(actions)
        self.conversation_id = ConversationId(conversation_id)
        self.decide_calls: list[CoordinatorObservation] = []
        self.assess_calls: list[tuple[str, FleetView]] = []
        self.closed = False

    def assess_task(self, task: str, fleet_view: FleetView) -> TaskAssessment:
        self.assess_calls.append((task, fleet_view))
        return self.assessment

    def decide_action(
        self,
        observation: CoordinatorObservation,
        conversation_id: ConversationId | None = None,
    ) -> CoordinatorAction:
        self.decide_calls.append(observation)
        if not self.actions:
            return CoordinatorAction(
                action_id=ActionId(f"act-auto-{uuid.uuid4().hex[:6]}"),
                kind=ActionKind.FINALIZE,
                final_response="Default auto final response",
            )
        act = self.actions.pop(0)
        if isinstance(act, Exception):
            raise act
        return act

    def close(self) -> None:
        self.closed = True


class BaseEngineTestCase(unittest.TestCase):
    """Base test case setting up isolated temporary filesystem roots."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="agym_test_engine_"))
        self.cfg_root = self.temp_dir / "cfg"
        self.data_root = self.temp_dir / "data"

        self.profile_store = ProfileStore(config_root=self.cfg_root, data_root=self.data_root)
        self.cache_manager = CacheManager(cache_root=self.data_root / "cache")
        self.lease_manager = ProfileLeaseManager(lease_root=self.data_root / "leases")
        self.run_store = FileRunStore(base_dir=self.data_root / "runs")
        self.runner = FakeModelRunner()

        # Create fleet of 4 healthy profiles
        self.profile_names = ["profile_alpha", "profile_beta", "profile_gamma", "profile_delta"]
        for p in self.profile_names:
            self.profile_store.create(p)
            self._set_quota(p, 0.90, 0.90)

        self.scheduler = ProfileScheduler(
            profile_store=self.profile_store,
            lease_manager=self.lease_manager,
            cache_manager=self.cache_manager,
            min_reserve=0.10,
            auth_checker=lambda _: True,
        )

        self.engine = OrchestrationEngine(
            scheduler=self.scheduler,
            lease_manager=self.lease_manager,
            store=self.run_store,
            runner=self.runner,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _set_quota(self, profile_name: str, five_hour: float, weekly: float) -> None:
        parsed_data = {
            "groups": [
                {
                    "name": "Gemini 2.5 Flash",
                    "buckets": [
                        {"id": "gemini_5h", "window": "5h", "remaining_fraction": five_hour},
                        {"id": "gemini_week", "window": "week", "remaining_fraction": weekly},
                    ],
                }
            ]
        }
        self.cache_manager.set_usage(profile_name, parsed_data, "")


# ============================================================================
# Engine Test Scenarios
# ============================================================================


class TestOrchestrationEngine(BaseEngineTestCase):

    def test_01_trivial_one_worker_task(self) -> None:
        """1. Trivial one-worker task: worker executes, then coordinator finalizes."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("w-1"),
                    role=WorkerRole.GENERAL,
                    objective="Answer simple question",
                )
            ],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-2"),
            kind=ActionKind.FINALIZE,
            final_response="Answer is 42",
        )
        coord = FakeCoordinator(actions=[action1, action2])
        self.runner.add_response("Worker 1 calculated 42")

        state = self.engine.run(task="What is 6 * 7?", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.final_result, "Answer is 42")
        self.assertEqual(state.budget_usage.invocations, 1)
        self.assertEqual(state.round_number, 1)

        # Verify results in persistence
        results = self.run_store.get_results(state.run_id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].worker_id, "w-1")
        self.assertEqual(results[0].response, "Worker 1 calculated 42")

        # Verify leases cleaned up
        self.assertEqual(self.lease_manager.list_leases(), [])

    def test_02_two_parallel_workers(self) -> None:
        """2. Two parallel workers dispatched simultaneously in one wave."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-arch"), role=WorkerRole.ARCHITECTURE, objective="Arch review"),
                WorkerRequest(worker_id=WorkerId("w-test"), role=WorkerRole.TESTING, objective="Test coverage"),
            ],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-2"),
            kind=ActionKind.FINALIZE,
            final_response="Both workers finished",
        )
        coord = FakeCoordinator(actions=[action1, action2])
        self.runner.add_response("Arch: Solid structure")
        self.runner.add_response("Testing: 100% tests pass")

        state = self.engine.run(task="Review design and test coverage", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.budget_usage.invocations, 2)
        results = self.run_store.get_results(state.run_id)
        self.assertEqual(len(results), 2)
        worker_ids = {r.worker_id for r in results}
        self.assertEqual(worker_ids, {"w-arch", "w-test"})
        self.assertEqual(self.lease_manager.list_leases(), [])

    def test_03_four_parallel_workers(self) -> None:
        """3. Four parallel workers all acquire distinct profiles simultaneously."""
        workers = [
            WorkerRequest(worker_id=WorkerId(f"w-{i}"), role=WorkerRole.GENERAL, objective=f"Obj {i}")
            for i in range(4)
        ]
        action1 = CoordinatorAction(action_id=ActionId("act-wave4"), kind=ActionKind.RUN_WORKERS, workers=workers)
        action2 = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="All 4 done")
        coord = FakeCoordinator(actions=[action1, action2])

        for i in range(4):
            self.runner.add_response(f"Result {i}")

        state = self.engine.run(task="Large parallel search", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.budget_usage.invocations, 4)

        # Check distinct profiles used
        calls = self.runner.calls
        self.assertEqual(len(calls), 4)
        profiles_used = {profile_name for _, profile_name in calls}
        self.assertEqual(len(profiles_used), 4)
        self.assertEqual(self.lease_manager.list_leases(), [])

    def test_04_auditor_after_workers(self) -> None:
        """4. Auditor reviewing completed workers from wave 1."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-workers"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL, objective="Propose change"),
                WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.SECURITY, objective="Security audit"),
            ],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-auditor"),
            kind=ActionKind.RUN_AUDITORS,
            auditors=[
                AuditRequest(
                    worker_id=WorkerId("audit-1"),
                    target_worker_ids=[WorkerId("w-1"), WorkerId("w-2")],
                    focus="edge cases",
                )
            ],
        )
        action3 = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Reviewed")
        coord = FakeCoordinator(actions=[action1, action2, action3])

        self.runner.add_response("W1: Patch proposed")
        self.runner.add_response("W2: No CVEs found")
        self.runner.add_response("- Finding: Missing null check\n- Finding: High memory usage")

        state = self.engine.run(task="Implement and audit", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.budget_usage.invocations, 3)

        results = self.run_store.get_results(state.run_id)
        audit_results = [r for r in results if isinstance(r, AuditResult)]
        self.assertEqual(len(audit_results), 1)
        self.assertIn("Missing null check", audit_results[0].findings)
        self.assertIn("High memory usage", audit_results[0].findings)
        self.assertEqual(self.lease_manager.list_leases(), [])

    def test_05_adaptive_second_worker_wave(self) -> None:
        """5. Coordinator inspects round 1 output and triggers adaptive round 2."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-initial"), role=WorkerRole.GENERAL, objective="Investigate")],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-2"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("w-followup"),
                    role=WorkerRole.DEBUGGING,
                    objective="Investigate bug discovered by w-initial",
                    context_worker_ids=[WorkerId("w-initial")],
                )
            ],
        )
        action3 = CoordinatorAction(action_id=ActionId("act-3"), kind=ActionKind.FINALIZE, final_response="Resolved")
        coord = FakeCoordinator(actions=[action1, action2, action3])

        self.runner.add_response("W-Initial: Discovered race condition in cache")
        self.runner.add_response("W-Followup: Fixed race condition with lock")

        state = self.engine.run(task="Debug intermittent failure", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.round_number, 2)
        self.assertEqual(state.budget_usage.invocations, 2)

    def test_06_synthesis(self) -> None:
        """6. Synthesis action consolidating prior worker findings."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-opt1"), role=WorkerRole.ARCHITECTURE, objective="Design Option A"),
                WorkerRequest(worker_id=WorkerId("w-opt2"), role=WorkerRole.ALTERNATIVE_DESIGN, objective="Design Option B"),
            ],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-2"),
            kind=ActionKind.RUN_SYNTHESIS,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("syn-1"),
                    role=WorkerRole.SYNTHESIZER,
                    objective="Synthesize Option A and Option B",
                )
            ],
        )
        action3 = CoordinatorAction(action_id=ActionId("act-3"), kind=ActionKind.FINALIZE, final_response="Unified")
        coord = FakeCoordinator(actions=[action1, action2, action3])

        self.runner.add_response("Opt1: Distributed approach")
        self.runner.add_response("Opt2: Monolithic approach")
        self.runner.add_response("Synthesized: Hybrid micro-core design")

        state = self.engine.run(task="Architectural redesign", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        results = self.run_store.get_results(state.run_id)
        syn_res = [r for r in results if r.worker_id == "syn-1"]
        self.assertEqual(len(syn_res), 1)
        self.assertEqual(syn_res[0].role, WorkerRole.SYNTHESIZER)
        self.assertIn("Hybrid micro-core", syn_res[0].response)

    def test_07_executor(self) -> None:
        """7. Executor invocation with MUTATING mode in IMPLEMENT mode."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-plan"), role=WorkerRole.GENERAL, objective="Plan edits")],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-2"),
            kind=ActionKind.RUN_EXECUTOR,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("exec-1"),
                    role=WorkerRole.EXECUTOR,
                    workspace_mode=WorkspaceMode.MUTATING,
                    objective="Apply patch to repo",
                )
            ],
        )
        action3 = CoordinatorAction(action_id=ActionId("act-3"), kind=ActionKind.FINALIZE, final_response="Applied")
        coord = FakeCoordinator(actions=[action1, action2, action3])

        self.runner.add_response("Plan: update file.txt")
        self.runner.add_response("Executor: modified file.txt successfully")

        state = self.engine.run(task="Apply code changes", mode=RunMode.IMPLEMENT, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        results = self.run_store.get_results(state.run_id)
        exec_res = [r for r in results if r.worker_id == "exec-1"]
        self.assertEqual(len(exec_res), 1)
        self.assertEqual(exec_res[0].role, WorkerRole.EXECUTOR)

    def test_08_worker_failure(self) -> None:
        """8. Worker returns an unrecoverable or nonzero error; coordinator adapts."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-fail"), role=WorkerRole.GENERAL, objective="Do doomed task")],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-2"),
            kind=ActionKind.FINALIZE,
            final_response="Gracefully concluded despite failure",
        )
        coord = FakeCoordinator(actions=[action1, action2])
        # Force a failure result from runner
        self.runner.add_response("failure")

        # Set budget max_retries = 0 so it fails directly without retry
        budget = OrchestrationBudget(max_retries=0)
        state = self.engine.run(task="Test failure handling", budget=budget, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        obs = coord.decide_calls[1]
        self.assertEqual(len(obs.failed_results), 1)
        self.assertEqual(obs.failed_results[0].worker_id, "w-fail")
        self.assertEqual(obs.failed_results[0].status, InvocationStatus.FAILED)

    def test_09_worker_timeout(self) -> None:
        """9. Worker execution times out and is returned as failure to coordinator."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-timeout"), role=WorkerRole.GENERAL, objective="Slow task")],
        )
        action2 = CoordinatorAction(action_id=ActionId("act-2"), kind=ActionKind.FINALIZE, final_response="Timed out")
        coord = FakeCoordinator(actions=[action1, action2])
        self.runner.add_response("timeout")

        budget = OrchestrationBudget(max_retries=0)
        state = self.engine.run(task="Test timeout", budget=budget, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        obs = coord.decide_calls[1]
        self.assertEqual(len(obs.failed_results), 1)
        self.assertEqual(obs.failed_results[0].failure, FailureClass.RETRYABLE)

    def test_10_quota_failure(self) -> None:
        """10. Low quota exhausts fleet capacity and rejects action safely."""
        # Set all profiles to 0% quota (exhausted)
        for p in self.profile_names:
            self._set_quota(p, 0.01, 0.01)

        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL, objective="Work")],
        )
        action2 = CoordinatorAction(action_id=ActionId("act-2"), kind=ActionKind.FINALIZE, final_response="No quota left")
        coord = FakeCoordinator(actions=[action1, action2])

        state = self.engine.run(task="Quota test", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        obs = coord.decide_calls[1]
        self.assertTrue(len(obs.rejected_requests) > 0 or len(obs.failed_results) > 0)

    def test_11_scheduler_insufficient_capacity(self) -> None:
        """11. Action requests more profiles than available; action rejected without crashing run."""
        # 4 profiles available, but coordinator requests 5 workers
        workers = [
            WorkerRequest(worker_id=WorkerId(f"w-{i}"), role=WorkerRole.GENERAL, objective=f"Obj {i}")
            for i in range(5)
        ]
        # Budget max_parallel = 10 so budget doesn't reject it first
        budget = OrchestrationBudget(max_parallel=10)
        action1 = CoordinatorAction(action_id=ActionId("act-overcapacity"), kind=ActionKind.RUN_WORKERS, workers=workers)
        action2 = CoordinatorAction(
            action_id=ActionId("act-retry-2"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL),
                WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.GENERAL),
            ],
        )
        action3 = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Recovered")
        coord = FakeCoordinator(actions=[action1, action2, action3])

        self.runner.add_response("Worker 1 done")
        self.runner.add_response("Worker 2 done")

        state = self.engine.run(task="Overcapacity recovery", budget=budget, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        # Action 1 was rejected and became observation
        first_obs = coord.decide_calls[1]
        self.assertEqual(len(first_obs.rejected_requests), 5)
        self.assertEqual(self.lease_manager.list_leases(), [])

    def test_12_retry_on_another_profile(self) -> None:
        """12. Worker fails with retryable error on profile A; retries successfully on profile B."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-retry"), role=WorkerRole.GENERAL, objective="Retryable task")],
        )
        action2 = CoordinatorAction(action_id=ActionId("act-2"), kind=ActionKind.FINALIZE, final_response="Retry worked")
        coord = FakeCoordinator(actions=[action1, action2])

        # Attempt 1 fails (retryable), Attempt 2 succeeds
        self.runner.add_response("failure")
        self.runner.add_response("Success on second profile!")

        budget = OrchestrationBudget(max_retries=2)
        state = self.engine.run(task="Test profile failover", budget=budget, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.budget_usage.retries, 1)

        calls = self.runner.calls
        self.assertEqual(len(calls), 2)
        prof1 = calls[0][1]
        prof2 = calls[1][1]
        self.assertNotEqual(prof1, prof2)

    def test_13_budget_max_parallel_rejection(self) -> None:
        """13. Action exceeding budget.max_parallel is rejected without crashing run."""
        budget = OrchestrationBudget(max_parallel=2)
        action1 = CoordinatorAction(
            action_id=ActionId("act-excess-parallel"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL),
                WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.GENERAL),
                WorkerRequest(worker_id=WorkerId("w-3"), role=WorkerRole.GENERAL),
            ],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-compliant"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-ok"), role=WorkerRole.GENERAL)],
        )
        action3 = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Done")
        coord = FakeCoordinator(actions=[action1, action2, action3])
        self.runner.add_response("Compliant worker finished")

        state = self.engine.run(task="Max parallel test", budget=budget, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        obs = coord.decide_calls[1]
        self.assertEqual(len(obs.rejected_requests), 3)
        self.assertIn("exceeding max_parallel limit of 2", obs.failed_results[0].response)

    def test_14_max_invocations_rejection(self) -> None:
        """14. Invocations exceeding budget.max_invocations are rejected."""
        budget = OrchestrationBudget(max_invocations=2)
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL),
                WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.GENERAL),
            ],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-exceed-inv"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-3"), role=WorkerRole.GENERAL)],
        )
        action3 = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Finished")
        coord = FakeCoordinator(actions=[action1, action2, action3])

        self.runner.add_response("w1 ok")
        self.runner.add_response("w2 ok")

        state = self.engine.run(task="Max invocations test", budget=budget, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.budget_usage.invocations, 2)
        obs = coord.decide_calls[2]
        self.assertEqual(len(obs.rejected_requests), 1)
        self.assertIn("max_invocations limit", obs.failed_results[0].response)

    def test_15_max_rounds_rejection(self) -> None:
        """15. Action proposed when round_number >= budget.max_rounds is rejected."""
        budget = OrchestrationBudget(max_rounds=1)
        action1 = CoordinatorAction(
            action_id=ActionId("act-round0"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL)],
        )
        action2 = CoordinatorAction(
            action_id=ActionId("act-round1-exceed"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.GENERAL)],
        )
        action3 = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Done")
        coord = FakeCoordinator(actions=[action1, action2, action3])
        self.runner.add_response("w1 ok")

        state = self.engine.run(task="Max rounds test", budget=budget, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        obs = coord.decide_calls[2]
        self.assertIn("Maximum rounds limit", obs.failed_results[0].response)

    def test_16_boost_limit_rejection(self) -> None:
        """16. Requesting more BOOST invocations than max_boost_invocations is rejected."""
        budget = OrchestrationBudget(max_boost_invocations=1)
        action1 = CoordinatorAction(
            action_id=ActionId("act-boost-excess"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-b1"), role=WorkerRole.GENERAL, strategy=ExecutionStrategy.BOOST),
                WorkerRequest(worker_id=WorkerId("w-b2"), role=WorkerRole.GENERAL, strategy=ExecutionStrategy.BOOST),
            ],
        )
        action2 = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Boost rejected")
        coord = FakeCoordinator(actions=[action1, action2])

        state = self.engine.run(task="Boost limit test", budget=budget, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        obs = coord.decide_calls[1]
        self.assertIn("BOOST invocations", obs.failed_results[0].response)

    def test_17_invalid_coordinator_action(self) -> None:
        """17. Structurally invalid coordinator action rejected safely via observation."""
        # Mutating worker in RUN_WORKERS (forbidden by protocol)
        invalid_raw = {
            "action_id": "act-invalid",
            "kind": "RUN_WORKERS",
            "workers": [
                {
                    "worker_id": "w-mutate-bad",
                    "role": "GENERAL",
                    "workspace_mode": "MUTATING",
                }
            ],
        }
        coord = FakeCoordinator()
        # Mock decide_action to return invalid dict first, then FINALIZE
        calls = [
            invalid_raw,
            CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Corrected"),
        ]

        def scripted_decide(obs: CoordinatorObservation, *args: Any, **kwargs: Any) -> Any:
            return calls.pop(0)

        coord.decide_action = scripted_decide  # type: ignore[assignment]

        state = self.engine.run(task="Invalid action test", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)

    def test_18_coordinator_crash(self) -> None:
        """18. Coordinator throws unhandled exception; engine marks run FAILED safely."""
        coord = FakeCoordinator(actions=[RuntimeError("Coordinator model crashed")])

        state = self.engine.run(task="Coordinator crash test", coordinator=coord)

        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertIn("Coordinator model crashed", state.final_result or "")
        self.assertEqual(self.lease_manager.list_leases(), [])

    def test_19_ctrl_c(self) -> None:
        """19. Interruption (Ctrl+C / KeyboardInterrupt) terminates runner, releases leases, sets INTERRUPTED."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-int"), role=WorkerRole.GENERAL)],
        )
        coord = FakeCoordinator(actions=[action1])

        def interrupt_runner(*args: Any, **kwargs: Any) -> Any:
            raise KeyboardInterrupt("Simulated Ctrl+C")

        self.runner.run = interrupt_runner  # type: ignore[assignment]

        state = self.engine.run(task="Interruption test", coordinator=coord, raise_on_interrupt=False)

        self.assertEqual(state.status, RunStatus.INTERRUPTED)
        self.assertTrue(coord.closed)
        self.assertEqual(self.lease_manager.list_leases(), [])

        # Verify RUN_INTERRUPTED event emitted
        events = self.run_store.get_events(state.run_id)
        int_evts = [e for e in events if e.type == EventType.RUN_INTERRUPTED]
        self.assertTrue(len(int_evts) > 0)

    def test_20_leases_always_released(self) -> None:
        """20. Leases guaranteed released in finally block even when worker crashes."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL),
                WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.GENERAL),
            ],
        )
        action2 = CoordinatorAction(action_id=ActionId("act-2"), kind=ActionKind.FINALIZE, final_response="Done")
        coord = FakeCoordinator(actions=[action1, action2])

        # Runner raises unexpected crash on worker 1, succeeds on worker 2
        def flaky_runner(inv: ModelInvocation, prof: str | None = None) -> ModelResult:
            if inv.worker_id == "w-1":
                raise OSError("Low-level OS socket error")
            return ModelResult(invocation_id=inv.invocation_id, status=InvocationStatus.SUCCEEDED, response="OK")

        self.runner.run = flaky_runner  # type: ignore[assignment]

        state = self.engine.run(task="Lease safety test", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        # All leases must be released!
        self.assertEqual(self.lease_manager.list_leases(), [])

    def test_21_dry_run_launches_no_workers(self) -> None:
        """21. Dry-run inspects fleet and validates first action without launching workers or acquiring leases."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-planned"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(worker_id=WorkerId("w-p1"), role=WorkerRole.ARCHITECTURE, objective="Arch"),
                WorkerRequest(worker_id=WorkerId("w-p2"), role=WorkerRole.SECURITY, objective="Security"),
            ],
        )
        coord = FakeCoordinator(actions=[action1])

        plan = self.engine.dry_run(task="Dry run planning test", coordinator=coord)

        self.assertIsInstance(plan, DryRunPlan)
        self.assertTrue(plan.is_valid)
        self.assertEqual(len(plan.planned_workers), 2)
        self.assertEqual(len(self.runner.invocations), 0)
        self.assertEqual(self.lease_manager.list_leases(), [])

        # Test display format
        display_str = plan.format_display()
        self.assertIn("=== Dry Run Plan", display_str)
        self.assertIn("w-p1", display_str)
        self.assertIn("w-p2", display_str)

    def test_22_resume_after_interrupted_worker(self) -> None:
        """22. Resume an interrupted run with in-progress worker; classifies failure and completes."""
        # 1. Create and interrupt a run with an active invocation
        run_id = RunId("run-resume-int-1")
        state = self.run_store.create_run(run_id=run_id, task="Resume interrupted task")

        # Record invocation started before crash
        inv_record = self.run_store.record_invocation_started(
            run_id,
            WorkerRequest(worker_id=WorkerId("w-orphan"), role=WorkerRole.GENERAL, objective="Unfinished work"),
            invocation_id="inv-orphan-1",
        )

        # Mark run interrupted
        self.run_store.mark_run_interrupted(run_id, reason="Simulated power loss")

        # 2. Resume run with a working coordinator
        action_fin = CoordinatorAction(
            action_id=ActionId("act-fin"),
            kind=ActionKind.FINALIZE,
            final_response="Successfully recovered after interruption",
        )
        coord = FakeCoordinator(actions=[action_fin])

        resumed_state = self.engine.resume(run_id, coordinator=coord)

        self.assertEqual(resumed_state.status, RunStatus.COMPLETED)
        self.assertEqual(resumed_state.final_result, "Successfully recovered after interruption")

        # Unfinished worker was classified as failed
        results = self.run_store.get_results(run_id)
        orphan_res = [r for r in results if r.worker_id == "w-orphan"]
        self.assertEqual(len(orphan_res), 1)
        self.assertEqual(orphan_res[0].status, InvocationStatus.FAILED)

    def test_23_resume_after_coordinator_crash(self) -> None:
        """23. Resume after coordinator crashed; starts new coordinator and completes."""
        run_id = RunId("run-resume-crash-1")
        crashing_coord = FakeCoordinator(actions=[RuntimeError("Coordinator model died")])

        # Run crashes
        failed_state = self.engine.run(task="Task doomed to crash", run_id=run_id, coordinator=crashing_coord)
        self.assertEqual(failed_state.status, RunStatus.FAILED)

        # Resume with working coordinator
        action_fin = CoordinatorAction(
            action_id=ActionId("act-fin"),
            kind=ActionKind.FINALIZE,
            final_response="Restored from crash",
        )
        working_coord = FakeCoordinator(actions=[action_fin])

        resumed_state = self.engine.resume(run_id, coordinator=working_coord)

        self.assertEqual(resumed_state.status, RunStatus.COMPLETED)
        self.assertEqual(resumed_state.final_result, "Restored from crash")

    def test_24_finalization(self) -> None:
        """24. Finalize action properly concludes run, saves final_result, and emits RUN_COMPLETED."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-fin-direct"),
            kind=ActionKind.FINALIZE,
            final_response="Direct conclusion without workers",
        )
        coord = FakeCoordinator(actions=[action1])

        state = self.engine.run(task="What time is it?", coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.final_result, "Direct conclusion without workers")

        # Verify RUN_COMPLETED event emitted
        events = self.run_store.get_events(state.run_id)
        completed_evts = [e for e in events if e.type == EventType.RUN_COMPLETED]
        self.assertEqual(len(completed_evts), 1)
        self.assertEqual(completed_evts[0].payload.get("final_result"), "Direct conclusion without workers")

        # Verify final.json created in persistence
        final_file = self.run_store.run_dir(state.run_id) / "final.json"
        self.assertTrue(final_file.exists())
        with open(final_file, "r", encoding="utf-8") as f:
            final_data = json.load(f)
        self.assertEqual(final_data.get("final_result"), "Direct conclusion without workers")

    # ========================================================================
    # Additional Edge Cases
    # ========================================================================

    def test_plan_mode_mutating_worker_rejected(self) -> None:
        """In PLAN mode, mutating workers are strictly forbidden."""
        action1 = CoordinatorAction(
            action_id=ActionId("act-mutate-in-plan"),
            kind=ActionKind.RUN_EXECUTOR,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("exec-plan"),
                    role=WorkerRole.EXECUTOR,
                    workspace_mode=WorkspaceMode.MUTATING,
                )
            ],
        )
        action2 = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Done")
        coord = FakeCoordinator(actions=[action1, action2])

        state = self.engine.run(task="Plan only", mode=RunMode.PLAN, coordinator=coord)

        self.assertEqual(state.status, RunStatus.COMPLETED)
        obs = coord.decide_calls[1]
        self.assertIn("not permitted in PLAN mode", obs.failed_results[0].response)

    def test_prompt_builders(self) -> None:
        """Verify build_worker_prompt and build_auditor_prompt format correct sections."""
        w_res = WorkerResult(
            worker_id=WorkerId("w-prev"),
            role=WorkerRole.GENERAL,
            response="Previous output",
        )
        wp = build_worker_prompt(
            task="Build feature",
            role=WorkerRole.ARCHITECTURE,
            objective="Analyze modules",
            repository_scope="src/core",
            context_results=[w_res],
        )
        self.assertIn("Build feature", wp)
        self.assertIn("ARCHITECTURE", wp)
        self.assertIn("Analyze modules", wp)
        self.assertIn("src/core", wp)
        self.assertIn("Previous output", wp)

        ap = build_auditor_prompt(
            task="Verify security",
            focus="XSS risks",
            repository_scope="web/",
            target_results=[w_res],
        )
        self.assertIn("Verify security", ap)
        self.assertIn("XSS risks", ap)
        self.assertIn("web/", ap)
        self.assertIn("Previous output", ap)


if __name__ == "__main__":
    unittest.main()
