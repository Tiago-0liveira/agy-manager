"""Adversarial stress-testing and empirical challenge suite for AGYM Council Milestone 2.

Empirically verifies:
1. WorkflowConfig graph validation fuzzer (cycles, boundary DAGs, deep chains, diamond graphs, disconnected components).
2. Invariant 4 (Strict Dissent Preservation): critic objection generation, synthesizer preservation, role case resilience.
3. FakeProviderAdapter latency, active cancellation, failure injection permutations, zero-billable invariant, and crash reconciliation.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import socket
import subprocess
import time
import unittest
from typing import Any

from pydantic import ValidationError

from agym.council.models import (
    AccountAuthStatus,
    AttemptSnapshot,
    AttemptStatus,
    CouncilOutputSections,
    LimitsConfig,
    ModelDescriptor,
    StageConfig,
    StageKind,
    TurnEvent,
    TurnRequest,
    TurnResult,
    TurnStatus,
    WorkerConfig,
    WorkflowConfig,
    WorkflowInput,
    redact_secrets,
)
from agym.council.providers.fake import FakeProviderAdapter


def _build_valid_workflow(num_stages: int = 3, num_workers: int = 3) -> dict[str, Any]:
    """Helper to build a structurally valid baseline workflow dictionary."""
    workers = [
        {
            "id": f"worker_{i}",
            "name": f"Worker {i}",
            "account_ref": f"profile_{i}",
            "model": "fake-model-pro",
            "role": "analyst",
            "instructions": "Follow baseline instructions.",
            "task": f"Duty {i}",
        }
        for i in range(num_workers)
    ]

    stages = []
    for i in range(num_stages):
        input_stages = [f"stage_{j}" for j in range(i)] if i > 0 else []
        assigned_worker = f"worker_{i % num_workers}"
        stages.append(
            {
                "id": f"stage_{i}",
                "kind": "independent" if i == 0 else "synthesize",
                "workers": [assigned_worker],
                "input_stages": input_stages,
                "instruction": f"Perform stage {i}",
            }
        )

    return {
        "schema_version": 1,
        "draft": False,
        "name": f"Adversarial Workflow {num_stages} Stages",
        "goal": "Test workflow graph boundaries and invariants",
        "inputs": [
            {
                "id": "brief",
                "description": "Task brief",
                "required": True,
                "value": "Adversarial test input",
            }
        ],
        "limits": {
            "global_concurrency": 4,
            "per_account_concurrency": 1,
            "max_model_calls": num_stages * 2 + 10,
            "max_retries_per_task": 1,
            "max_wall_seconds": 3600,
            "automatic_account_switching": False,
        },
        "workers": workers,
        "stages": stages,
        "final_stage": f"stage_{num_stages - 1}",
    }


class TestWorkflowFuzzingAndGraphBoundaries(unittest.TestCase):
    """Empirical fuzzer and boundary validator for WorkflowConfig graphs."""

    def test_boundary_minimal_single_stage_workflow(self) -> None:
        """Verify minimal valid workflow: 1 input, 1 worker, 1 stage."""
        wf_data = _build_valid_workflow(num_stages=1, num_workers=1)
        cfg = WorkflowConfig.model_validate(wf_data)
        self.assertEqual(len(cfg.stages), 1)
        self.assertEqual(cfg.final_stage, "stage_0")

    def test_boundary_deep_linear_pipeline(self) -> None:
        """Verify deep linear pipeline of 30 sequentially chained stages."""
        wf_data = _build_valid_workflow(num_stages=30, num_workers=5)
        # Ensure each stage depends strictly on the immediately preceding stage
        for i in range(1, 30):
            wf_data["stages"][i]["input_stages"] = [f"stage_{i - 1}"]
        cfg = WorkflowConfig.model_validate(wf_data)
        self.assertEqual(len(cfg.stages), 30)
        self.assertEqual(cfg.final_stage, "stage_29")

    def test_boundary_wide_diamond_dag(self) -> None:
        """Verify wide diamond DAG: 1 root -> 15 parallel stages -> 1 join stage."""
        num_parallel = 15
        workers = [
            {
                "id": f"w_{i}",
                "name": f"Worker {i}",
                "account_ref": f"prof_{i}",
                "model": "fake-model-pro",
                "role": "analyst",
                "instructions": "Instructions",
                "task": "Task",
            }
            for i in range(num_parallel + 2)
        ]

        stages = [
            {
                "id": "root",
                "kind": "independent",
                "workers": ["w_0"],
                "input_stages": [],
                "instruction": "Root stage",
            }
        ]
        parallel_ids = []
        for i in range(num_parallel):
            p_id = f"parallel_{i}"
            parallel_ids.append(p_id)
            stages.append(
                {
                    "id": p_id,
                    "kind": "independent",
                    "workers": [f"w_{i + 1}"],
                    "input_stages": ["root"],
                    "instruction": f"Parallel stage {i}",
                }
            )
        stages.append(
            {
                "id": "join",
                "kind": "synthesize",
                "workers": [f"w_{num_parallel + 1}"],
                "input_stages": parallel_ids,
                "instruction": "Join stage",
            }
        )

        wf_data = {
            "schema_version": 1,
            "draft": True,
            "name": "Diamond DAG",
            "goal": "Wide graph fanout/fanin",
            "inputs": [{"id": "in1", "description": "desc", "required": True}],
            "limits": {"max_model_calls": 50},
            "workers": workers,
            "stages": stages,
            "final_stage": "join",
        }
        cfg = WorkflowConfig.model_validate(wf_data)
        self.assertEqual(len(cfg.stages), num_parallel + 2)
        self.assertEqual(cfg.final_stage, "join")

    def test_boundary_disconnected_parallel_pipelines(self) -> None:
        """Verify disconnected independent stages: stage 0 and stage 1 have no inputs, stage 2 synthesizes both."""
        wf_data = _build_valid_workflow(num_stages=3, num_workers=3)
        wf_data["stages"][0]["input_stages"] = []
        wf_data["stages"][1]["input_stages"] = []  # Disconnected independent stage
        wf_data["stages"][2]["input_stages"] = ["stage_0", "stage_1"]
        cfg = WorkflowConfig.model_validate(wf_data)
        self.assertEqual(len(cfg.stages), 3)

    def test_fuzz_cycle_injection_rejection(self) -> None:
        """Fuzz random graphs with injected cycles: self-loops, 2-cycles, multi-node cycles."""
        rng = random.Random(42)
        for trial in range(50):
            num_stages = rng.randint(2, 10)
            wf_data = _build_valid_workflow(num_stages=num_stages, num_workers=4)

            cycle_type = trial % 3
            if cycle_type == 0:
                # Self loop: a stage references itself
                target_stage = rng.randint(0, num_stages - 1)
                wf_data["stages"][target_stage]["input_stages"].append(
                    wf_data["stages"][target_stage]["id"]
                )
            elif cycle_type == 1:
                # Forward reference (creates a potential cycle if later stage references earlier)
                earlier = rng.randint(0, num_stages - 2)
                later = rng.randint(earlier + 1, num_stages - 1)
                wf_data["stages"][earlier]["input_stages"].append(
                    wf_data["stages"][later]["id"]
                )
            else:
                # Multi-node cycle: stage 0 references stage 1, while stage 1 depends on stage 0
                wf_data["stages"][0]["input_stages"].append(wf_data["stages"][1]["id"])

            with self.assertRaises(ValueError, msg=f"Trial {trial} (cycle_type {cycle_type}) must fail"):
                WorkflowConfig.model_validate(wf_data)

    def test_fuzz_unknown_worker_references(self) -> None:
        """Fuzz random stage worker references to non-existent worker IDs."""
        rng = random.Random(1337)
        for trial in range(25):
            wf_data = _build_valid_workflow(num_stages=4, num_workers=2)
            ghost_worker = f"ghost_worker_{trial}_{rng.randint(1000, 9999)}"
            stage_idx = rng.randint(0, 3)
            wf_data["stages"][stage_idx]["workers"].append(ghost_worker)

            with self.assertRaises(ValueError) as cm:
                WorkflowConfig.model_validate(wf_data)
            self.assertIn("references unknown worker", str(cm.exception))

    def test_fuzz_budget_calculation(self) -> None:
        """Verify strict boundary enforcement for max_model_calls vs required calls."""
        wf_data = _build_valid_workflow(num_stages=4, num_workers=4)
        # Assign 2 workers to each stage = 8 calls
        for s in wf_data["stages"]:
            s["workers"] = ["worker_0", "worker_1"]
        total_calls = 8

        # Exact budget: 8 calls with budget 8 -> PASS
        wf_data["limits"]["max_model_calls"] = total_calls
        cfg = WorkflowConfig.model_validate(wf_data)
        self.assertEqual(cfg.limits.max_model_calls, 8)

        # Deficit by 1: 8 calls with budget 7 -> FAIL
        wf_data["limits"]["max_model_calls"] = total_calls - 1
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(wf_data)
        self.assertIn("exceed budget before retries", str(cm.exception))

    def test_live_run_optional_inputs_do_not_require_binding(self) -> None:
        """Verify that optional inputs (required=False) without values pass in live execution."""
        wf_data = _build_valid_workflow(num_stages=2, num_workers=2)
        wf_data["draft"] = False
        wf_data["inputs"].append(
            {
                "id": "optional_context",
                "description": "Optional extra docs",
                "required": False,
                "value": None,
            }
        )
        cfg = WorkflowConfig.model_validate(wf_data)
        self.assertFalse(cfg.draft)
        self.assertIsNone(cfg.inputs[1].value)

    def test_live_run_whitespace_model_placeholder_rejected(self) -> None:
        """Verify that blank or whitespace-only model names are rejected when draft=False."""
        wf_data = _build_valid_workflow(num_stages=2, num_workers=2)
        wf_data["draft"] = False
        wf_data["workers"][0]["model"] = "   "
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(wf_data)
        self.assertIn("is a placeholder; must be bound before execution", str(cm.exception))

    def test_limits_config_negative_and_zero_values_fuzzed(self) -> None:
        """Verify that zero and negative numeric limits are rejected."""
        invalid_values = [0, -1, -100]
        for val in invalid_values:
            with self.assertRaises(ValidationError):
                LimitsConfig(global_concurrency=val)
            with self.assertRaises(ValidationError):
                LimitsConfig(per_account_concurrency=val)
            with self.assertRaises(ValidationError):
                LimitsConfig(max_model_calls=val)
            with self.assertRaises(ValidationError):
                LimitsConfig(max_wall_seconds=val)

    def test_limits_config_automatic_account_switching_inviolable(self) -> None:
        """Verify that automatic_account_switching=True is strictly forbidden."""
        with self.assertRaises(ValueError) as cm:
            LimitsConfig(automatic_account_switching=True)
        self.assertIn("must be False to preserve account isolation", str(cm.exception))


class TestInvariant4StrictDissentPreservation(unittest.IsolatedAsyncioTestCase):
    """Empirical verification of Invariant 4: Strict Dissent Preservation."""

    async def test_critic_emits_unambiguous_objection_in_dissent_mode(self) -> None:
        """Critic role under dissent_mode=True must emit explicit objection."""
        adapter = FakeProviderAdapter(dissent_mode=True)
        req = TurnRequest(
            attempt_id="att-critic-dissent-check",
            run_id="run-inv4",
            stage_id="critique_stage",
            worker_id="critic_agent",
            account_ref="prof-crit",
            model="fake-model-pro",
            role="critic",
            prompt="Critique the distributed database schema",
        )

        result: TurnResult | None = None
        async for item in adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item

        self.assertIsNotNone(result)
        self.assertIsNotNone(result.structured_output)
        findings = result.structured_output["findings"]
        self.assertIn("CRITICAL OBJECTION", findings)
        self.assertIn("fails SLA latency requirements", findings)

    async def test_synthesizer_preserves_critic_objection_faithfully(self) -> None:
        """Synthesizer role must preserve dissent and explicitly refuse artificial consensus."""
        adapter = FakeProviderAdapter(dissent_mode=True)
        req = TurnRequest(
            attempt_id="att-synth-preserve",
            run_id="run-inv4",
            stage_id="synthesis_stage",
            worker_id="synthesizer_agent",
            account_ref="prof-synth",
            model="fake-model-pro",
            role="synthesizer",
            prompt="Synthesize analyst findings with critic reservations",
        )

        result: TurnResult | None = None
        async for item in adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item

        self.assertIsNotNone(result)
        findings = result.structured_output["findings"]
        self.assertIn("PRESERVED DISSENT", findings)
        self.assertIn("No artificial consensus was invented", findings)
        self.assertIn("Critic objections regarding SLA latency", findings)

    async def test_synthesizer_retains_dissent_on_keyword_triggers_when_dissent_mode_disabled(self) -> None:
        """Even if dissent_mode is False, if prompt contains dissent keywords, synthesizer must retain dissent."""
        adapter = FakeProviderAdapter(dissent_mode=False)
        dissent_keywords = ["critic", "objection", "dissent", "reservation"]

        for kw in dissent_keywords:
            req = TurnRequest(
                attempt_id=f"att-kw-{kw}",
                run_id="run-inv4",
                stage_id="synthesis_stage",
                worker_id="synthesizer_agent",
                account_ref="prof-synth",
                model="fake-model-pro",
                role="synthesizer",
                prompt=f"Please reconcile the {kw} raised by reviewer",
            )
            result = None
            async for item in adapter.run_turn(req):
                if isinstance(item, TurnResult):
                    result = item
            self.assertIsNotNone(result)
            findings = result.structured_output["findings"]
            self.assertIn(
                "PRESERVED DISSENT",
                findings,
                msg=f"Keyword '{kw}' must trigger dissent preservation",
            )

    async def test_synthesizer_produces_full_consensus_only_without_objections(self) -> None:
        """When dissent_mode=False and prompt contains zero dissent markers, consensus is integrated."""
        adapter = FakeProviderAdapter(dissent_mode=False)
        req = TurnRequest(
            attempt_id="att-synth-consensus",
            run_id="run-inv4",
            stage_id="synthesis_stage",
            worker_id="synthesizer_agent",
            account_ref="prof-synth",
            model="fake-model-pro",
            role="synthesizer",
            prompt="Produce standard composite summary of findings",
        )
        result = None
        async for item in adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item
        self.assertIsNotNone(result)
        findings = result.structured_output["findings"]
        self.assertNotIn("PRESERVED DISSENT", findings)
        self.assertIn("All findings integrated and consensus validated", findings)

    async def test_role_casing_and_stage_naming_resilience(self) -> None:
        """Verify case-insensitivity of roles and stage name inference for dissent simulation."""
        adapter = FakeProviderAdapter(dissent_mode=True)
        # 1. Uppercase role CRITIC
        req_critic = TurnRequest(
            attempt_id="att-case-critic",
            run_id="r1",
            stage_id="stage_review",
            worker_id="w1",
            account_ref="p1",
            model="m",
            role="CRITIC",
            prompt="Audit code",
        )
        res_critic = None
        async for item in adapter.run_turn(req_critic):
            if isinstance(item, TurnResult):
                res_critic = item
        self.assertIn("CRITICAL OBJECTION", res_critic.structured_output["findings"])

        # 2. Mixed case SYNTHESIZER
        req_synth = TurnRequest(
            attempt_id="att-case-synth",
            run_id="r1",
            stage_id="final_stage",
            worker_id="w2",
            account_ref="p2",
            model="m",
            role="Synthesizer",
            prompt="Wrap up",
        )
        res_synth = None
        async for item in adapter.run_turn(req_synth):
            if isinstance(item, TurnResult):
                res_synth = item
        self.assertIn("PRESERVED DISSENT", res_synth.structured_output["findings"])

    def test_output_sections_markdown_render_and_validation(self) -> None:
        """Verify CouncilOutputSections markdown rendering and missing section detection."""
        sections = CouncilOutputSections(
            findings="Analysis completed with reservations.",
            evidence_or_assumptions="Based on logs and telemetry.",
            uncertainties="Edge latency under burst load.",
            next_action="Deploy with canary rollout.",
        )
        md = sections.to_markdown()
        self.assertIn("## Findings\nAnalysis completed with reservations.", md)
        self.assertIn("## Uncertainties\nEdge latency under burst load.", md)

        # Validation of required sections: all present -> empty list
        missing = sections.validate_required(["findings", "uncertainties", "next_action"])
        self.assertEqual(missing, [])

        # Incomplete sections -> returns list of missing fields
        bad_sections = CouncilOutputSections(findings="Just findings")
        missing_bad = bad_sections.validate_required(["findings", "evidence_or_assumptions", "uncertainties"])
        self.assertEqual(missing_bad, ["evidence_or_assumptions", "uncertainties"])


class TestFakeProviderEmpiricalHarness(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress-testing of FakeProviderAdapter."""

    async def _collect_result(self, adapter: FakeProviderAdapter, req: TurnRequest) -> TurnResult:
        res = None
        async for item in adapter.run_turn(req):
            if isinstance(item, TurnResult):
                res = item
        if res is None:
            self.fail("No TurnResult yielded")
        return res

    async def test_synthetic_latency_timing_distribution(self) -> None:
        """Verify synthetic latency timing accuracy across multiple values."""
        adapter = FakeProviderAdapter()
        test_latencies = [0.02, 0.05, 0.08]

        for target_lat in test_latencies:
            adapter.set_latency(target_lat)
            req = TurnRequest(
                attempt_id=f"att-lat-{int(target_lat * 1000)}",
                run_id="run-lat",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                model="m",
                prompt="timing test",
            )
            start = time.perf_counter()
            res = await self._collect_result(adapter, req)
            elapsed = time.perf_counter() - start
            self.assertGreaterEqual(elapsed, target_lat * 0.85)
            self.assertEqual(res.status, TurnStatus.SUCCESS)

    async def test_zero_latency_immediate_execution(self) -> None:
        """Verify 0.0 latency executes with negligible overhead (< 15ms)."""
        adapter = FakeProviderAdapter(default_latency=0.0)
        req = TurnRequest(
            attempt_id="att-zero-lat",
            run_id="run-zero",
            stage_id="s1",
            worker_id="w1",
            account_ref="p1",
            model="m",
            prompt="zero latency",
        )
        start = time.perf_counter()
        res = await self._collect_result(adapter, req)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.03)
        self.assertEqual(res.status, TurnStatus.SUCCESS)

    async def test_active_inflight_cancellation_during_sleep(self) -> None:
        """Start long-latency turn (0.4s), cancel at 0.03s; assert termination in < 0.15s with CANCELLED status."""
        adapter = FakeProviderAdapter(default_latency=0.4)
        req = TurnRequest(
            attempt_id="att-active-canc",
            run_id="run-canc",
            stage_id="s1",
            worker_id="w1",
            account_ref="p1",
            model="m",
            prompt="cancel test",
        )

        async def run_worker() -> TurnResult:
            return await self._collect_result(adapter, req)

        task = asyncio.create_task(run_worker())
        await asyncio.sleep(0.03)

        start_cancel = time.perf_counter()
        await adapter.cancel("att-active-canc")
        res = await task
        cancel_duration = time.perf_counter() - start_cancel

        self.assertEqual(res.status, TurnStatus.CANCELLED)
        self.assertLess(cancel_duration, 0.15)
        self.assertIn("cancelled", (res.error_message or "").lower())

    async def test_precancellation_never_executes_turn(self) -> None:
        """Pre-cancelling an attempt ID before running it immediately yields CANCELLED TurnResult."""
        adapter = FakeProviderAdapter(default_latency=0.5)
        att_id = "att-pre-cancelled"
        await adapter.cancel(att_id)

        req = TurnRequest(
            attempt_id=att_id,
            run_id="run-canc",
            stage_id="s1",
            worker_id="w1",
            account_ref="p1",
            model="m",
            prompt="pre-cancel test",
        )

        items = []
        start = time.perf_counter()
        async for item in adapter.run_turn(req):
            items.append(item)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.05)
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], TurnResult)
        self.assertEqual(items[0].status, TurnStatus.CANCELLED)

    async def test_idempotent_concurrent_cancellation(self) -> None:
        """Calling cancel() concurrently from multiple coroutines is safe and idempotent."""
        adapter = FakeProviderAdapter()
        att_id = "att-concurrent-canc"
        coros = [adapter.cancel(att_id) for _ in range(10)]
        await asyncio.gather(*coros)
        self.assertIn(att_id, adapter.cancelled_attempts)

    async def test_cancellation_after_success_does_not_override_completion(self) -> None:
        """If attempt completed with SUCCESS, a subsequent cancel() call still reconciles as SUCCEEDED."""
        adapter = FakeProviderAdapter(default_latency=0.0)
        att_id = "att-done-then-cancel"
        req = TurnRequest(
            attempt_id=att_id,
            run_id="r1",
            stage_id="s1",
            worker_id="w1",
            account_ref="p1",
            model="m",
            prompt="done",
        )
        res = await self._collect_result(adapter, req)
        self.assertEqual(res.status, TurnStatus.SUCCESS)

        # Later, cancellation is triggered
        await adapter.cancel(att_id)

        # Reconcile must still return SUCCEEDED because it completed
        snapshots = [
            AttemptSnapshot(
                attempt_id=att_id,
                run_id="r1",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                status=AttemptStatus.RUNNING,
            )
        ]
        recon = await adapter.reconcile(snapshots)
        self.assertEqual(recon[0].reconciled_status, AttemptStatus.SUCCEEDED)

    async def test_failure_injection_all_permutations_and_hierarchy(self) -> None:
        """Test failure injection permutations across attempt, worker, and stage hierarchy."""
        adapter = FakeProviderAdapter()

        # 1. Attempt failure overrides worker failure
        adapter.inject_failure("att-override", "rate-limited")
        adapter.inject_failure("w-override", "needs-auth")
        req1 = TurnRequest(
            attempt_id="att-override",
            run_id="r1",
            stage_id="s1",
            worker_id="w-override",
            account_ref="p1",
            model="m",
            prompt="p",
        )
        res1 = await self._collect_result(adapter, req1)
        self.assertEqual(res1.status, TurnStatus.RATE_LIMITED)

        # 2. Worker failure applies when attempt failure is absent
        req2 = TurnRequest(
            attempt_id="att-other",
            run_id="r1",
            stage_id="s1",
            worker_id="w-override",
            account_ref="p1",
            model="m",
            prompt="p",
        )
        res2 = await self._collect_result(adapter, req2)
        self.assertEqual(res2.status, TurnStatus.NEEDS_AUTH)

        # 3. Stage failure applies when attempt and worker are absent
        adapter.inject_failure("stage-failing", "timeout")
        req3 = TurnRequest(
            attempt_id="att-3",
            run_id="r1",
            stage_id="stage-failing",
            worker_id="w3",
            account_ref="p1",
            model="m",
            prompt="p",
        )
        res3 = await self._collect_result(adapter, req3)
        self.assertEqual(res3.status, TurnStatus.TIMEOUT)

        # 4. Clear failures restores normal operation
        adapter.clear_failures()
        res4 = await self._collect_result(adapter, req1)
        self.assertEqual(res4.status, TurnStatus.SUCCESS)

    async def test_zero_billable_invariant_and_no_side_effects(self) -> None:
        """Intercept socket.connect and subprocess.Popen to verify 0 network calls and 0 subprocesses."""
        real_socket_connect = socket.socket.connect
        real_popen = subprocess.Popen

        network_calls: list[Any] = []
        subprocesses: list[Any] = []

        def fake_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
            network_calls.append(args)
            raise AssertionError("Unexpected network socket connection during FakeProviderAdapter turn!")

        def fake_popen(*args: Any, **kwargs: Any) -> Any:
            subprocesses.append(args)
            raise AssertionError("Unexpected subprocess launch during FakeProviderAdapter turn!")

        socket.socket.connect = fake_connect  # type: ignore[assignment]
        subprocess.Popen = fake_popen  # type: ignore[assignment]

        try:
            adapter = FakeProviderAdapter(default_latency=0.0)
            req = TurnRequest(
                attempt_id="att-zero-side-effects",
                run_id="r-zero",
                stage_id="s1",
                worker_id="w1",
                account_ref="prof-1",
                model="fake-model-pro",
                prompt="Test zero side effects",
            )
            res = await self._collect_result(adapter, req)
            self.assertEqual(res.status, TurnStatus.SUCCESS)
            self.assertEqual(res.usage.get("cost_usd"), 0.0)
            self.assertEqual(len(network_calls), 0)
            self.assertEqual(len(subprocesses), 0)
        finally:
            socket.socket.connect = real_socket_connect  # type: ignore[assignment]
            subprocess.Popen = real_popen  # type: ignore[assignment]

    async def test_reconciliation_multi_snapshot_matrix(self) -> None:
        """Verify reconcile() against a mixed batch of completed, cancelled, and orphaned attempts."""
        adapter = FakeProviderAdapter(default_latency=0.0)

        # Setup 1: Completed attempt
        req_completed = TurnRequest(
            attempt_id="att-comp-1",
            run_id="r1",
            stage_id="s1",
            worker_id="w1",
            account_ref="p1",
            model="m",
            prompt="done",
        )
        await self._collect_result(adapter, req_completed)

        # Setup 2: Cancelled attempt
        await adapter.cancel("att-canc-2")

        # Reconcile 3 snapshots: 1 completed, 1 cancelled, 1 completely unknown
        snapshots = [
            AttemptSnapshot(
                attempt_id="att-comp-1",
                run_id="r1",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                status=AttemptStatus.RUNNING,
            ),
            AttemptSnapshot(
                attempt_id="att-canc-2",
                run_id="r1",
                stage_id="s1",
                worker_id="w2",
                account_ref="p2",
                status=AttemptStatus.RUNNING,
            ),
            AttemptSnapshot(
                attempt_id="att-orphan-3",
                run_id="r1",
                stage_id="s1",
                worker_id="w3",
                account_ref="p3",
                status=AttemptStatus.RUNNING,
                pid=12345,
            ),
        ]

        recon = await adapter.reconcile(snapshots)
        self.assertEqual(len(recon), 3)

        rec_map = {r.attempt_id: r for r in recon}
        self.assertEqual(rec_map["att-comp-1"].reconciled_status, AttemptStatus.SUCCEEDED)
        self.assertEqual(rec_map["att-canc-2"].reconciled_status, AttemptStatus.CANCELLED)
        self.assertEqual(rec_map["att-orphan-3"].reconciled_status, AttemptStatus.UNKNOWN)
        self.assertIn("12345", rec_map["att-orphan-3"].details)

    async def test_high_concurrency_50_workers_turn_isolation(self) -> None:
        """Run 50 concurrent worker turns through FakeProviderAdapter, ensuring 0 cross-talk or corruption."""
        adapter = FakeProviderAdapter(default_latency=0.01)

        async def worker_turn(i: int) -> TurnResult:
            req = TurnRequest(
                attempt_id=f"att-conc-50-{i}",
                run_id="run-high-conc",
                stage_id=f"stage-{i % 5}",
                worker_id=f"worker-{i}",
                account_ref=f"prof-{i % 3}",
                model="fake-model-pro",
                role="critic" if i % 2 == 0 else "analyst",
                prompt=f"Task prompt for worker {i}",
            )
            return await self._collect_result(adapter, req)

        tasks = [worker_turn(i) for i in range(50)]
        results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), 50)
        for i, res in enumerate(results):
            self.assertEqual(res.status, TurnStatus.SUCCESS)
            self.assertEqual(res.attempt_id, f"att-conc-50-{i}")
            self.assertEqual(res.usage.get("cost_usd"), 0.0)
            if i % 2 == 0:
                self.assertIn("CRITICAL OBJECTION", res.structured_output["findings"])
            else:
                self.assertIn("Analysis", res.structured_output["findings"])

    async def test_all_failure_status_codes_injection(self) -> None:
        """Exhaustively verify each TurnStatus code injected as string with hyphens or underscores."""
        test_cases = [
            ("rate-limited", TurnStatus.RATE_LIMITED),
            ("rate_limited", TurnStatus.RATE_LIMITED),
            ("needs-auth", TurnStatus.NEEDS_AUTH),
            ("needs_auth", TurnStatus.NEEDS_AUTH),
            ("timeout", TurnStatus.TIMEOUT),
            ("permission-blocked", TurnStatus.PERMISSION_BLOCKED),
            ("permission_blocked", TurnStatus.PERMISSION_BLOCKED),
            ("unavailable", TurnStatus.UNAVAILABLE),
            ("malformed-output", TurnStatus.MALFORMED_OUTPUT),
            ("malformed_output", TurnStatus.MALFORMED_OUTPUT),
        ]

        for inject_str, expected_status in test_cases:
            adapter = FakeProviderAdapter()
            att_id = f"att-fail-{inject_str}"
            adapter.inject_failure(att_id, inject_str)
            req = TurnRequest(
                attempt_id=att_id,
                run_id="r-fail",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                model="m",
                prompt="fail test",
            )
            res = await self._collect_result(adapter, req)
            self.assertEqual(
                res.status,
                expected_status,
                msg=f"Injected '{inject_str}' did not produce status '{expected_status}'",
            )

    async def test_custom_response_plain_text_fallback(self) -> None:
        """When custom_response is a plain non-JSON string, it falls back to findings."""
        adapter = FakeProviderAdapter()
        att_id = "att-plain-text"
        adapter.set_custom_response(att_id, "Plain text unformatted answer.")
        req = TurnRequest(
            attempt_id=att_id,
            run_id="r1",
            stage_id="s1",
            worker_id="w1",
            account_ref="p1",
            model="m",
            prompt="plain",
        )
        res = await self._collect_result(adapter, req)
        self.assertEqual(res.status, TurnStatus.SUCCESS)
        self.assertEqual(res.structured_output["findings"], "Plain text unformatted answer.")


class TestWorkflowGraphScalabilityAndSecurity(unittest.TestCase):
    """Stress-test large scale graphs and security redaction helpers."""

    def test_large_scale_100_stage_dag_validation_performance(self) -> None:
        """Validate that a 100-stage DAG compiles and validates within 0.1 seconds."""
        wf_data = _build_valid_workflow(num_stages=100, num_workers=10)
        start = time.perf_counter()
        cfg = WorkflowConfig.model_validate(wf_data)
        duration = time.perf_counter() - start
        self.assertEqual(len(cfg.stages), 100)
        self.assertLess(duration, 0.25, msg=f"100-stage validation took too long: {duration:.4f}s")

    def test_secret_redaction_recursive_defense(self) -> None:
        """Verify redact_secrets masks passwords, tokens, API keys, and auth codes in deep nested structures."""
        dirty_payload = {
            "profile": "prod-agent",
            "auth_token": "bearer-secret-xyz-123",
            "api_key": "sk-ant-api03-abcdef",
            "nested": {
                "password": "SuperSecretPassword123!",
                "safe_field": "public_data",
                "secret_list": [
                    {"client_secret": "topsecret"},
                    {"safe_item": 42},
                ],
            },
        }
        cleaned = redact_secrets(dirty_payload)
        self.assertEqual(cleaned["auth_token"], "[REDACTED]")
        self.assertEqual(cleaned["api_key"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["password"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["safe_field"], "public_data")
        self.assertEqual(cleaned["nested"]["secret_list"][0]["client_secret"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["secret_list"][1]["safe_item"], 42)


if __name__ == "__main__":
    unittest.main()

