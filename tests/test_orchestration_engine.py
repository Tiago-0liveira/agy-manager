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
import concurrent.futures
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Sequence
import unittest
from unittest.mock import patch
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
    ActionRejectedError,
    BudgetExceededError,
    DryRunPlan,
    EngineError,
    OrchestrationEngine,
    build_auditor_prompt,
    build_worker_prompt,
)
from agym.orchestration.leases import ProfileLeaseManager
from agym.orchestration.persistence import FileRunStore
from agym.orchestration.runner import AntigravityRunner, FakeModelRunner
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

    def test_engine_enforces_hard_budget_after_rejections_and_long_worker_timeout(self) -> None:
        """Regression test for W4-02: engine enforces hard budget on rejections and clamps timeouts."""
        # 1. Test rejection limit prevents infinite coordinator loop
        invalid_actions = [
            CoordinatorAction(
                action_id=ActionId(f"act-inv-{i}"),
                kind=ActionKind.RUN_WORKERS,
                workers=[WorkerRequest(worker_id=WorkerId(f"w-{i}"), role=WorkerRole.GENERAL)],
            )
            for i in range(10)
        ]
        # Force these actions to be rejected by making budget max_invocations = 0
        zero_budget = OrchestrationBudget(max_invocations=0, max_consecutive_rejections=2)
        coord_rejections = FakeCoordinator(actions=invalid_actions)
        with self.assertRaises(EngineError) as ctx:
            self.engine.run(
                task="Test rejection bound",
                budget=zero_budget,
                coordinator=coord_rejections,
                raise_on_error=True,
            )
        self.assertTrue(isinstance(ctx.exception, (ActionRejectedError, BudgetExceededError)))

        # Also verify that with raise_on_error=False it safely returns FAILED state
        coord_rejections_2 = FakeCoordinator(actions=invalid_actions)
        state = self.engine.run(
            task="Test rejection bound no-raise",
            budget=zero_budget,
            coordinator=coord_rejections_2,
            raise_on_error=False,
        )
        self.assertEqual(state.status, RunStatus.FAILED)

        # 2. Test that worker timeout is clamped to remaining runtime budget
        timed_budget = OrchestrationBudget(max_runtime_seconds=1.0)
        worker_act = CoordinatorAction(
            action_id=ActionId("act-long-timeout"),
            kind=ActionKind.RUN_WORKERS,
            workers=[
                WorkerRequest(
                    worker_id=WorkerId("w-oversized"),
                    role=WorkerRole.GENERAL,
                    timeout_seconds=99999.0,
                )
            ],
        )
        fin_act = CoordinatorAction(action_id=ActionId("act-fin"), kind=ActionKind.FINALIZE, final_response="Done")
        coord_timeout = FakeCoordinator(actions=[worker_act, fin_act])
        self.runner.add_response("w ok")

        run_state = self.engine.run(
            task="Test clamped timeout",
            budget=timed_budget,
            coordinator=coord_timeout,
        )
        self.assertEqual(run_state.status, RunStatus.COMPLETED)
        # Verify the dispatched invocation had its timeout clamped
        matching = [inv for inv in self.runner.invocations if inv.worker_id == WorkerId("w-oversized")]
        self.assertTrue(len(matching) > 0)
        self.assertLessEqual(matching[0].timeout_seconds, 1.0)

    def test_concurrent_resume_cannot_revoke_live_run_leases(self) -> None:
        """W4-04: Concurrent resume cannot revoke leases owned by live runs or double-run work."""
        import os
        from agym.orchestration.engine import _ACTIVE_RUNS, _ACTIVE_RUNS_LOCK

        rid = RunId("run-concurrent-test")
        # 1. Simulate an active run with a lease owned by the current live PID
        lease = self.lease_manager.acquire("profile_alpha", run_id=rid, worker_id=WorkerId("w-1"))
        self.assertIsNotNone(lease)

        # Create a run in RUNNING status in the store
        run_state = self.run_store.create_run(run_id=rid, task="Concurrent test task")
        run_state.status = RunStatus.RUNNING
        self.run_store.save_run(run_state)

        # Case A: active in-process
        with _ACTIVE_RUNS_LOCK:
            _ACTIVE_RUNS.add(str(rid))

        try:
            with self.assertRaises(EngineError) as ctx:
                self.engine.resume(rid, coordinator=FakeCoordinator())
            self.assertIn("already active", str(ctx.exception).lower())
        finally:
            with _ACTIVE_RUNS_LOCK:
                _ACTIVE_RUNS.discard(str(rid))

        # Case B: active in another process holding the run file lock
        run_lock = self.engine._get_run_lock(rid)
        run_lock.acquire()
        try:
            with self.assertRaises(EngineError) as ctx:
                self.engine.resume(rid, coordinator=FakeCoordinator())
            self.assertIn("already active", str(ctx.exception).lower())
        finally:
            run_lock.release()

        # Verify that the lease for profile_alpha was NOT revoked by the resume attempts
        active_lease = self.lease_manager.get_lease("profile_alpha")
        self.assertIsNotNone(active_lease)
        self.assertEqual(active_lease.lease_id, lease.lease_id)
        self.assertEqual(active_lease.run_id, rid)

        # Case C: Even if _cleanup_leases is invoked directly, verify it skips leases owned by live external PIDs
        external_live_pid = os.getppid() if os.getppid() > 0 and os.getppid() != os.getpid() else 1
        lease2 = self.lease_manager.acquire(
            "profile_beta", run_id=rid, worker_id=WorkerId("w-2"), pid=external_live_pid
        )
        self.assertIsNotNone(lease2)

        # Call _cleanup_leases
        self.engine._cleanup_leases(rid)

        # Profile beta lease must still be active because external_live_pid is alive
        beta_lease = self.lease_manager.get_lease("profile_beta")
        self.assertIsNotNone(beta_lease)
        self.assertEqual(beta_lease.lease_id, lease2.lease_id)

    def test_profile_credential_exclusivity_across_coordinator_workers_and_processes(self) -> None:
        """W4-06: Coordinator profile is leased exclusively and excluded from workers; Windows credential locking protects across processes."""
        from agym.orchestration.leases import ProfileAlreadyLeasedError
        from agym.orchestration.runner import get_wincred_cross_process_lock

        # Part 1: Coordinator profile exclusivity
        coord = FakeCoordinator(
            actions=[
                CoordinatorAction(
                    action_id=ActionId("act-1"),
                    kind=ActionKind.RUN_WORKERS,
                    workers=[
                        WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL),
                        WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.GENERAL),
                    ],
                ),
                CoordinatorAction(
                    action_id=ActionId("act-fin"),
                    kind=ActionKind.FINALIZE,
                    final_response="All work done",
                ),
            ]
        )
        coord.profile_name = "profile_alpha"
        self.runner.add_response("worker 1 done")
        self.runner.add_response("worker 2 done")

        # Run with coordinator using profile_alpha
        state = self.engine.run(task="Exclusivity test", coordinator=coord)
        self.assertEqual(state.status, RunStatus.COMPLETED)

        # Verify that dispatched workers never received profile_alpha (it was coordinator-exclusive)
        worker_profiles = [profile for inv, profile in self.runner.calls]
        self.assertTrue(len(worker_profiles) >= 2)
        for p in worker_profiles:
            self.assertNotEqual(p, "profile_alpha")

        # Part 2: If coordinator profile is already leased elsewhere, run fails to double-lease
        lease_ext = self.lease_manager.acquire("profile_alpha", run_id="run-external", worker_id=WorkerId("w-ext"))
        self.assertIsNotNone(lease_ext)

        coord_blocked = FakeCoordinator()
        coord_blocked.profile_name = "profile_alpha"
        with self.assertRaises((ProfileAlreadyLeasedError, EngineError)):
            self.engine.run(task="Blocked coordinator run", coordinator=coord_blocked, raise_on_error=True)

        self.lease_manager.release(lease_ext)

        # Part 3: Cross-process Windows credential lock existence and acquisition
        wincred_lock = get_wincred_cross_process_lock()
        self.assertIsNotNone(wincred_lock)
        self.assertTrue(wincred_lock.acquire())
        self.assertTrue(wincred_lock.path.exists())
        wincred_lock.release()

    def test_cancellation_never_allows_work_to_continue_after_run_is_interrupted(self) -> None:
        """W4-07: Cancellation preserves CancelledError and stops all work before leases are released."""
        worker_started = threading.Event()
        worker_cancelled = threading.Event()
        worker_continued = threading.Event()

        action1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-cancel"), role=WorkerRole.GENERAL)],
        )
        action2 = CoordinatorAction(action_id=ActionId("act-2"), kind=ActionKind.FINALIZE, final_response="Done")
        coord = FakeCoordinator(actions=[action1, action2])

        async def hanging_runner(invocation: ModelInvocation, profile_name: str | None = None) -> ModelResult:
            worker_started.set()
            try:
                await asyncio.sleep(5.0)
                worker_continued.set()
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise
            return ModelResult(invocation_id=invocation.invocation_id, status=InvocationStatus.SUCCEEDED, response="OK")

        self.runner.run_async = hanging_runner  # type: ignore[assignment]

        # Case 1: Direct asyncio task cancellation
        async def run_direct_cancellation() -> None:
            task = asyncio.create_task(
                self.engine.run_async(
                    task="Direct cancel test",
                    coordinator=coord,
                    run_id="run-direct-cancel",
                )
            )
            while not worker_started.is_set():
                await asyncio.sleep(0.01)

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run_direct_cancellation())

        self.assertTrue(worker_cancelled.is_set())
        self.assertFalse(worker_continued.is_set())
        self.assertEqual(self.lease_manager.list_leases(), [])

        # Case 2: Synchronous wrapper inside an existing running event loop
        worker_started.clear()
        worker_cancelled.clear()
        worker_continued.clear()

        coord2 = FakeCoordinator(actions=[action1, action2])

        async def run_sync_wrapper_in_loop() -> None:
            orig_submit = concurrent.futures.ThreadPoolExecutor.submit
            interrupted = False

            def mock_submit(executor_self: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
                fut = orig_submit(executor_self, fn, *args, **kwargs)
                orig_result = fut.result

                def intercepted_result(timeout: float | None = None) -> Any:
                    if timeout == 5.0:
                        return orig_result(timeout=timeout)
                    while not worker_started.is_set():
                        time.sleep(0.01)
                    raise KeyboardInterrupt("Simulated Ctrl+C")

                fut.result = intercepted_result  # type: ignore[assignment]
                return fut

            with patch.object(concurrent.futures.ThreadPoolExecutor, "submit", side_effect=mock_submit, autospec=True):
                try:
                    self.engine.run(
                        task="Sync wrapper interrupt test",
                        coordinator=coord2,
                        run_id="run-sync-int",
                        raise_on_interrupt=True,
                    )
                except KeyboardInterrupt:
                    interrupted = True

            self.assertTrue(interrupted)

        def thread_target() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(run_sync_wrapper_in_loop())
            finally:
                loop.close()

        t = threading.Thread(target=thread_target)
        t.start()
        t.join(timeout=10.0)

        self.assertTrue(worker_cancelled.is_set())
        self.assertFalse(worker_continued.is_set())
        self.assertEqual(self.lease_manager.list_leases(), [])

    def test_run_shutdown_leaves_no_worker_descendants_or_coordinator_session(self) -> None:
        """W4-08: Process-tree cleanup leaves no worker child descendants, and coordinator session is closed on terminal paths."""
        from agym.orchestration.leases import is_pid_alive
        import subprocess
        import sys

        # Part 1: Verify runner's process tree cancellation terminates both root and child descendants
        real_runner = AntigravityRunner(profile_store=self.profile_store)
        script = (
            "import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(p.pid, flush=True); "
            "time.sleep(60)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None
        child_pid_line = proc.stdout.readline().decode().strip()
        child_pid = int(child_pid_line)
        parent_pid = proc.pid

        self.assertTrue(is_pid_alive(parent_pid))
        self.assertTrue(is_pid_alive(child_pid))

        # Register with runner and cancel
        real_runner._register_process(RunId("run-w408"), InvocationId("inv-tree"), proc)  # type: ignore[arg-type]
        real_runner.cancel_run("run-w408")

        # Verify neither parent nor child descendant survived
        self.assertFalse(is_pid_alive(parent_pid))
        self.assertFalse(is_pid_alive(child_pid))

        # Part 2: Verify coordinator session is closed from terminal finally path on both normal and error runs
        coord_normal = FakeCoordinator(
            actions=[CoordinatorAction(action_id=ActionId("act-1"), kind=ActionKind.FINALIZE, final_response="Done")]
        )
        self.engine.run(task="Normal run test", coordinator=coord_normal, run_id="run-coord-norm")
        self.assertTrue(coord_normal.closed)

        coord_error = FakeCoordinator(
            actions=[RuntimeError("Engine coordinator error")]
        )
        try:
            self.engine.run(task="Error run test", coordinator=coord_error, run_id="run-coord-err", raise_on_error=True)
        except RuntimeError:
            pass
        self.assertTrue(coord_error.closed)

    def test_resume_recovers_from_missing_or_expired_coordinator_session_without_reexecuting_completed_work(self) -> None:
        """W4-11: Resume succeeds with missing or expired coordinator session without reexecuting completed work."""
        rid = RunId("run-w411-resume")
        action_finalize = CoordinatorAction(
            action_id=ActionId("act-fin"),
            kind=ActionKind.FINALIZE,
            final_response="Final answer after recovery",
        )

        state = self.run_store.create_run(run_id=rid, task="Test task W4-11")
        state.status = RunStatus.INTERRUPTED
        state.round_number = 1
        state.coordinator_conversation_id = None
        self.run_store.save_run(state)

        worker_res = WorkerResult(
            worker_id=WorkerId("w-done"),
            role=WorkerRole.GENERAL,
            status=InvocationStatus.SUCCEEDED,
            invocation_id=InvocationId("inv-w-done"),
            response="Prior completed work from round 1",
        )
        self.run_store.save_result(rid, worker_res)

        invocation_calls: list[str] = []
        orig_runner_run = self.runner.run
        def tracking_run(inv: ModelInvocation, profile_name: str | None = None) -> ModelResult:
            invocation_calls.append(str(inv.worker_id))
            return orig_runner_run(inv, profile_name)
        self.runner.run = tracking_run  # type: ignore[assignment]

        # Case A: Missing coordinator session ID
        coord_resume_missing = FakeCoordinator(actions=[action_finalize])
        resumed_state = self.engine.resume(rid, coordinator=coord_resume_missing)

        self.assertEqual(resumed_state.status, RunStatus.COMPLETED)
        self.assertEqual(resumed_state.final_result, "Final answer after recovery")
        self.assertNotIn("w-done", invocation_calls)
        self.assertEqual(len(coord_resume_missing.decide_calls), 1)
        obs = coord_resume_missing.decide_calls[0]
        self.assertEqual(len(obs.completed_results), 1)
        self.assertEqual(obs.completed_results[0].worker_id, WorkerId("w-done"))

        # Case B: Expired coordinator session ID
        rid2 = RunId("run-w411-expired")
        state2 = self.run_store.create_run(run_id=rid2, task="Test task W4-11 Expired")
        state2.status = RunStatus.INTERRUPTED
        state2.round_number = 1
        state2.coordinator_conversation_id = ConversationId("expired-session-conv")
        self.run_store.save_run(state2)
        self.run_store.save_result(rid2, worker_res)

        class ExpiringCoordinator(FakeCoordinator):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.resumed_with_expired = False
                self.recovered_fresh = False

            def resume(self, run_id: Any = None, conversation_id: Any = None, run_state: Any = None) -> None:
                if conversation_id == "expired-session-conv":
                    self.resumed_with_expired = True
                    raise RuntimeError("Conversation expired on remote backend")
                self.recovered_fresh = True
                self.conversation_id = ConversationId("fresh-conv-id")

        coord_expiring = ExpiringCoordinator(actions=[action_finalize])
        resumed_state2 = self.engine.resume(rid2, coordinator=coord_expiring)

        self.assertEqual(resumed_state2.status, RunStatus.COMPLETED)
        self.assertTrue(coord_expiring.resumed_with_expired)
        self.assertTrue(coord_expiring.recovered_fresh)
        self.assertNotIn("w-done", invocation_calls)

    def test_worker_identity_remains_unique_and_consistent_across_rounds_and_resume(self) -> None:
        """W4-12: Verify worker identities are unique across rounds/resumes and visible to auditors."""
        from agym.orchestration.protocol import ActionValidationError, validate_coordinator_action

        # 1. Direct validation check: Reusing worker ID triggers validation rejection
        dup_action = CoordinatorAction(
            action_id=ActionId("act-dup"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL, objective="do work")],
        )
        with self.assertRaises(ActionValidationError) as ctx:
            validate_coordinator_action(dup_action, known_worker_ids={"w-1"})
        self.assertIn("already used", str(ctx.exception))

        # 2. Engine integration: Round 1 runs w-1, Round 2 attempts w-1 (rejected), then uses w-2
        action_round1 = CoordinatorAction(
            action_id=ActionId("act-1"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL, objective="run 1")],
        )
        action_round2_dup = CoordinatorAction(
            action_id=ActionId("act-2-dup"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL, objective="run 1 duplicate")],
        )
        action_round2_valid = CoordinatorAction(
            action_id=ActionId("act-2-valid"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-2"), role=WorkerRole.GENERAL, objective="run 2")],
        )
        action_round3_finalize = CoordinatorAction(
            action_id=ActionId("act-fin"),
            kind=ActionKind.FINALIZE,
            final_response="done",
        )

        coordinator = FakeCoordinator(actions=[
            action_round1,
            action_round2_dup,
            action_round2_valid,
            action_round3_finalize,
        ])

        state = self.engine.run("Test worker identity uniqueness", coordinator=coordinator)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        # Calls:
        # decide[0] -> initial obs -> returns act-1
        # decide[1] -> obs with completed w-1 -> returns act-2-dup (rejected)
        # decide[2] -> obs with rejected act-2-dup -> returns act-2-valid
        # decide[3] -> obs with completed w-2 -> returns act-fin
        self.assertGreaterEqual(len(coordinator.decide_calls), 4)
        rej_obs = coordinator.decide_calls[2]
        self.assertTrue(any("already used" in (f.response or "") for f in rej_obs.failed_results))

        # 3. Resuming a run where a prior worker failed: auditor can target both completed and failed workers
        rid = RunId("run-w412-resume")
        r_state = self.run_store.create_run(run_id=rid, task="Resume test")
        r_state.status = RunStatus.INTERRUPTED
        r_state.round_number = 1

        w1_res = WorkerResult(
            worker_id=WorkerId("w-1"),
            role=WorkerRole.GENERAL,
            status=InvocationStatus.SUCCEEDED,
            response="W1 succeeded",
        )
        w2_res = WorkerResult(
            worker_id=WorkerId("w-2"),
            role=WorkerRole.GENERAL,
            status=InvocationStatus.FAILED,
            response="W2 failed execution",
        )
        self.run_store.save_run(r_state)
        self.run_store.save_result(rid, w1_res)
        self.run_store.save_result(rid, w2_res)

        # Auditor auditing failed w-2
        auditor_action = CoordinatorAction(
            action_id=ActionId("act-audit"),
            kind=ActionKind.RUN_AUDITORS,
            auditors=[AuditRequest(worker_id=WorkerId("aud-1"), target_worker_ids=[WorkerId("w-2")], focus="check w-2")],
        )
        res_coord = FakeCoordinator(actions=[auditor_action, action_round3_finalize])
        resumed = self.engine.resume(rid, coordinator=res_coord)
        self.assertEqual(resumed.status, RunStatus.COMPLETED)

    def test_completed_run_persists_final_artifact(self) -> None:
        """W4-13: Completed run calls store.finalize_run and persists final.json artifact."""
        action_fin = CoordinatorAction(
            action_id=ActionId("act-fin-artifact"),
            kind=ActionKind.FINALIZE,
            final_response="Artifact persistence test successful",
        )
        coord = FakeCoordinator(actions=[action_fin])

        # Track finalize_run calls to ensure engine does not swallow API mismatch
        original_finalize = self.run_store.finalize_run
        finalize_called: list[tuple[Any, ...]] = []

        def tracking_finalize(*args: Any, **kwargs: Any) -> RunState:
            finalize_called.append((args, kwargs))
            return original_finalize(*args, **kwargs)

        self.run_store.finalize_run = tracking_finalize  # type: ignore[assignment]

        state = self.engine.run(task="Final artifact test", coordinator=coord)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.final_result, "Artifact persistence test successful")

        # Verify finalize_run was invoked directly and successfully
        self.assertEqual(len(finalize_called), 1)

        # Verify final.json exists independently on disk with correct structure
        final_file = self.run_store.run_dir(state.run_id) / "final.json"
        self.assertTrue(final_file.is_file())
        with open(final_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("run_id"), str(state.run_id))
        self.assertEqual(data.get("final_result"), "Artifact persistence test successful")
        self.assertEqual(data.get("status"), "COMPLETED")
        self.assertIn("completed_at", data)

    def test_initial_coordinator_action_is_executed_and_dry_run_uses_one_turn(self) -> None:
        """W4-14: Round-0 coordinator action is executed and dry-run uses exactly one model turn."""
        from agym.orchestration.protocol import InitialCoordinatorResponse

        action_workers = CoordinatorAction(
            action_id=ActionId("act-initial-worker"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-init"), role=WorkerRole.GENERAL, objective="Initial task work")],
        )
        action_finalize = CoordinatorAction(
            action_id=ActionId("act-subsequent-finalize"),
            kind=ActionKind.FINALIZE,
            final_response="Work completed after initial action",
        )

        class StartupCoordinator(FakeCoordinator):
            def __init__(self, initial_action: CoordinatorAction, subsequent_actions: list[CoordinatorAction]) -> None:
                super().__init__(actions=subsequent_actions)
                self.initial_action = initial_action
                self.start_calls: list[tuple[str, FleetView]] = []

            def start_run(self, task: str, fleet_view: FleetView) -> InitialCoordinatorResponse:
                self.start_calls.append((task, fleet_view))
                return InitialCoordinatorResponse(
                    assessment=self.assessment,
                    action=self.initial_action,
                )

        # 1. Normal execution: Initial worker action is executed, NOT discarded for the subsequent finalize
        coord_run = StartupCoordinator(
            initial_action=action_workers,
            subsequent_actions=[action_finalize],
        )
        state = self.engine.run("Test initial action execution", coordinator=coord_run)
        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.final_result, "Work completed after initial action")

        # Verify w-init was executed
        results = self.run_store.get_results(state.run_id)
        w_res = [r for r in results if r.worker_id == "w-init"]
        self.assertEqual(len(w_res), 1)
        self.assertEqual(w_res[0].status, InvocationStatus.SUCCEEDED)

        # Verify decide_action was only called for Round 1 observation (with w-init completed result)
        self.assertEqual(len(coord_run.decide_calls), 1)
        self.assertEqual(len(coord_run.decide_calls[0].completed_results), 1)
        self.assertEqual(coord_run.decide_calls[0].completed_results[0].worker_id, WorkerId("w-init"))

        # 2. Dry run: Exactly one turn (start_run) used, decide_action never called
        coord_dry = StartupCoordinator(
            initial_action=action_workers,
            subsequent_actions=[action_finalize],
        )
        plan = self.engine.dry_run("Test initial action dry run", coordinator=coord_dry)
        self.assertEqual(len(coord_dry.start_calls), 1)
        self.assertEqual(len(coord_dry.decide_calls), 0)
        self.assertEqual(len(plan.planned_workers), 1)
        self.assertEqual(plan.planned_workers[0]["worker_id"], "w-init")

    def test_nonzero_exit_preserves_stdout_and_error_in_engine_and_store(self) -> None:
        """W4-15: Engine execution and run store preserve both stdout and error separately on worker failure."""
        action_worker = CoordinatorAction(
            action_id=ActionId("act-w"),
            kind=ActionKind.RUN_WORKERS,
            workers=[WorkerRequest(worker_id=WorkerId("w-failing"), role=WorkerRole.GENERAL, objective="Do work that fails")],
        )
        action_finalize = CoordinatorAction(
            action_id=ActionId("act-fin"),
            kind=ActionKind.FINALIZE,
            final_response="Run finished after failure",
        )
        coord = FakeCoordinator(actions=[action_worker, action_finalize])

        # Runner returns ModelResult with both response and error
        def failing_run(invocation: ModelInvocation, profile_name: str | None = None) -> ModelResult:
            return ModelResult(
                invocation_id=invocation.invocation_id,
                status=InvocationStatus.FAILED,
                exit_code=1,
                response="Partial valuable stdout before crash",
                error="Process exited with non-zero exit code 1: stack trace details",
            )

        self.runner.run = failing_run  # type: ignore[assignment]

        state = self.engine.run("Test preserving stdout on failure", coordinator=coord)
        self.assertEqual(state.status, RunStatus.COMPLETED)

        # 1. Check observation sent to coordinator contains both
        self.assertGreaterEqual(len(coord.decide_calls), 2)
        obs = coord.decide_calls[1]
        self.assertEqual(len(obs.failed_results), 1)
        f_res = obs.failed_results[0]
        self.assertEqual(f_res.worker_id, WorkerId("w-failing"))
        self.assertEqual(f_res.response, "Partial valuable stdout before crash")
        self.assertIn("non-zero exit code 1", f_res.error or "")

        # 2. Check run store results
        results = self.run_store.get_results(state.run_id)
        failing_results = [r for r in results if r.worker_id == "w-failing"]
        self.assertEqual(len(failing_results), 1)
        saved_res = failing_results[0]
        self.assertEqual(saved_res.response, "Partial valuable stdout before crash")
        self.assertIn("non-zero exit code 1", saved_res.error or "")

        # 3. Check outputs file
        out_file = self.run_store.run_dir(state.run_id) / "outputs" / f"{saved_res.invocation_id}.txt"
        self.assertTrue(out_file.is_file())
        self.assertEqual(out_file.read_text(encoding="utf-8"), "Partial valuable stdout before crash")


if __name__ == "__main__":
    unittest.main()
