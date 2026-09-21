"""Tests for Durable Staged Engine, Atomic Barriers & Dissent Preservation (Features 24–28, 31)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agym.council.artifacts import ArtifactStore
from agym.council.context import DISSENT_MANDATE
from agym.council.engine import CouncilEngine, RunResult, StageResult
from agym.council.models import (
    CouncilOutputSections,
    ExecutionMode,
    LimitsConfig,
    Run,
    RunStatus,
    StageConfig,
    StageKind,
    StageStatus,
    TurnResult,
    TurnStatus,
    WorkerConfig,
    WorkflowConfig,
    WorkflowInput,
)
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.scheduler import CapacityScheduler
from agym.council.storage import (
    create_run,
    get_run,
    get_stage,
    init_db,
    list_artifacts_for_run,
    list_issues_for_run,
)


class TestEngineBarriersAndLifecycle(unittest.IsolatedAsyncioTestCase):
    """Deep integration tests for CouncilEngine, barriers, dissent, and state lifecycles."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "council_test.db"
        self.conn = init_db(self.db_path)
        self.artifact_store = ArtifactStore(self.root / "cas")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _create_multi_stage_workflow(self) -> WorkflowConfig:
        """Helper to create a 3-stage workflow: Analysis -> Critique -> Synthesis."""
        w_analyst = WorkerConfig(
            id="worker-analyst",
            name="Analyst",
            account_ref="acc-alpha",
            model="fake-model-pro",
            role="Analyst",
            instructions="Analyze data accurately.",
            task="Analyze data.",
        )
        w_critic = WorkerConfig(
            id="worker-critic",
            name="Critic",
            account_ref="acc-beta",
            model="fake-model-fast",
            role="Critic",
            instructions="Identify risks and flaws.",
            task="Find objections and flaws.",
        )
        w_synth = WorkerConfig(
            id="worker-synth",
            name="Synthesizer",
            account_ref="acc-gamma",
            model="fake-model-pro",
            role="Synthesizer",
            instructions="Synthesize all perspectives.",
            task="Provide balanced conclusion.",
        )

        stage_1 = StageConfig(
            id="stage-1-analysis",
            kind=StageKind.INDEPENDENT,
            workers=["worker-analyst"],
            instruction="Produce initial analysis.",
            required_sections=["findings", "evidence_or_assumptions", "uncertainties", "next_action"],
        )
        stage_2 = StageConfig(
            id="stage-2-critique",
            kind=StageKind.CRITIQUE,
            workers=["worker-critic"],
            input_stages=["stage-1-analysis"],
            instruction="Critique the stage 1 findings.",
            required_sections=["findings", "evidence_or_assumptions", "uncertainties", "next_action"],
        )
        stage_3 = StageConfig(
            id="stage-3-synthesis",
            kind=StageKind.SYNTHESIZE,
            workers=["worker-synth"],
            input_stages=["stage-1-analysis", "stage-2-critique"],
            instruction="Synthesize findings and critiques.",
            required_sections=["findings", "evidence_or_assumptions", "uncertainties", "next_action"],
        )

        return WorkflowConfig(
            name="Multi-Stage Review",
            goal="Reach a robust decision with preserved dissent",
            inputs=[WorkflowInput(id="doc", description="Input document", value="content-123")],
            limits=LimitsConfig(global_concurrency=4, per_account_concurrency=1, max_retries_per_task=1),
            execution_mode=ExecutionMode.SUPPLIED_EVIDENCE,
            workers=[w_analyst, w_critic, w_synth],
            stages=[stage_1, stage_2, stage_3],
            final_stage="stage-3-synthesis",
        )

    async def test_end_to_end_ordered_stages_completion(self) -> None:
        """Feature 24: Ordered Stage Loop runs stages in sequence to full COMPLETED state."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter(default_latency=0.01)
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        result = await engine.execute_run(run_id)

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(len(result.stage_results), 3)
        self.assertTrue(all(sr.status == StageStatus.COMPLETED for sr in result.stage_results))
        self.assertIsNotNone(result.final_deliverable_artifact_id)

        # Verify DB state
        run_row = get_run(self.conn, run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row["status"], "COMPLETED")
        self.assertIsNotNone(run_row["finished_at"])

    async def test_atomic_stage_release_barrier(self) -> None:
        """Feature 25: Artifacts remain unreleased until stage completion barrier commits."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        # Run only stage 1
        stage_1_res = await engine.run_stage(run_id, "stage-1-analysis")
        self.assertEqual(stage_1_res.status, StageStatus.COMPLETED)

        # All stage 1 artifacts must now be released = 1
        stage_1_artifacts = list_artifacts_for_run(self.conn, run_id, stage_id="stage-1-analysis")
        self.assertTrue(len(stage_1_artifacts) > 0)
        # Verify output artifact is released
        output_arts = [a for a in stage_1_artifacts if a["name"].startswith("output_")]
        self.assertEqual(len(output_arts), 1)
        self.assertEqual(output_arts[0]["released"], 1)

    async def test_output_section_enforcement(self) -> None:
        """Feature 26: Worker output missing required sections triggers MALFORMED_OUTPUT."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        # Invalidate custom response for worker-analyst to miss next_action
        bad_response = {
            "findings": "Some findings",
            "evidence_or_assumptions": "Some evidence",
            "uncertainties": "Some uncertainties",
            # "next_action" is missing!
        }
        fake_provider.set_custom_response("worker-analyst", bad_response)

        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        stage_res = await engine.run_stage(run_id, "stage-1-analysis")

        # Because next_action is missing, worker retries and fails, so stage fails
        self.assertEqual(stage_res.status, StageStatus.NEEDS_ATTENTION)
        worker_res = stage_res.worker_results["worker-analyst"]
        self.assertEqual(worker_res.status, TurnStatus.MALFORMED_OUTPUT)
        self.assertIn("next_action", worker_res.error_message or "")

    async def test_zero_tolerance_failure_policy_halts_synthesis(self) -> None:
        """Feature 28: Required worker failure transitions run to NEEDS_ATTENTION; halts pipeline."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        # Inject failure into stage 1 worker
        fake_provider.inject_failure("worker-analyst", "Simulated fatal error in analysis")

        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        result = await engine.execute_run(run_id)

        # Run must enter NEEDS_ATTENTION immediately after stage 1 failure
        self.assertEqual(result.status, RunStatus.NEEDS_ATTENTION)
        self.assertIn("worker-analyst", result.error_message or "")

        # Downstream stages 2 and 3 must NOT have been executed
        stage_2_row = get_stage(self.conn, run_id, "stage-2-critique")
        stage_3_row = get_stage(self.conn, run_id, "stage-3-synthesis")
        self.assertIsNotNone(stage_2_row)
        self.assertIsNotNone(stage_3_row)
        self.assertEqual(stage_2_row["status"], "PENDING")
        self.assertEqual(stage_3_row["status"], "PENDING")

        # Unreleased draft artifacts must not be released
        released_arts = list_artifacts_for_run(self.conn, run_id, released_only=True)
        self.assertEqual(len(released_arts), 0)

        # Audit issue must be recorded
        issues = list_issues_for_run(self.conn, run_id)
        self.assertTrue(any("worker-analyst" in i["message"] for i in issues))

    async def test_strict_dissent_preservation_in_synthesis(self) -> None:
        """Feature 27: Critic dissent sets dissent_recorded and mandates non-invention of consensus."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter(dissent_mode=True)
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        result = await engine.execute_run(run_id)

        self.assertEqual(result.status, RunStatus.COMPLETED)

        # Verify dissent_recorded is True in SQLite
        run_row = get_run(self.conn, run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row["dissent_recorded"], 1)

        # Check that synthesizer's rendered prompt artifact contains DISSENT_MANDATE
        synth_artifacts = list_artifacts_for_run(self.conn, run_id, stage_id="stage-3-synthesis")
        prompt_arts = [a for a in synth_artifacts if a["name"].startswith("prompt_")]
        self.assertTrue(len(prompt_arts) > 0)

        prompt_hash = prompt_arts[0]["content_hash"]
        prompt_bytes = self.artifact_store.get(prompt_hash)
        prompt_text = prompt_bytes.decode("utf-8")

        self.assertIn("IMPORTANT DISSENT MANDATE:", prompt_text)
        self.assertIn(DISSENT_MANDATE, prompt_text)

    async def test_pause_and_resume_run(self) -> None:
        """Feature 31: Run can be paused, inspected, and resumed to completion."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        # Run stage 1 manually
        res1 = await engine.run_stage(run_id, "stage-1-analysis")
        self.assertEqual(res1.status, StageStatus.COMPLETED)

        # Pause the run
        engine.pause_run(run_id)
        run_row = get_run(self.conn, run_id)
        self.assertEqual(run_row["status"], "PAUSED")

        # Resume run: executes remaining stages (stage 2 and stage 3)
        res_resumed = await engine.resume_run(run_id)
        self.assertEqual(res_resumed.status, RunStatus.COMPLETED)

        final_run_row = get_run(self.conn, run_id)
        self.assertEqual(final_run_row["status"], "COMPLETED")

    async def test_cancel_run_terminates_active_attempts(self) -> None:
        """Feature 31: Cancel run marks in-flight attempts and run as CANCELLED."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        engine.cancel_run(run_id)

        run_row = get_run(self.conn, run_id)
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row["status"], "CANCELLED")

        # Calling execute_run on cancelled run returns CANCELLED result immediately
        res = await engine.execute_run(run_id)
        self.assertEqual(res.status, RunStatus.CANCELLED)

    async def test_resolve_run_from_needs_attention(self) -> None:
        """Feature 31: Resolve run transitions NEEDS_ATTENTION run back to READY."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        fake_provider.inject_failure("worker-analyst", "Transient error")

        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        # Execute run to cause failure
        res = await engine.execute_run(run_id)
        self.assertEqual(res.status, RunStatus.NEEDS_ATTENTION)

        # Resolve the run
        engine.resolve_run(run_id, action="retry_with_fix", resolution_note="Fixed transient issue")

        resolved_row = get_run(self.conn, run_id)
        self.assertEqual(resolved_row["status"], "READY")

        # Clear failure and re-run to completion!
        fake_provider.clear_failures()
        res_retry = await engine.execute_run(run_id)
        self.assertEqual(res_retry.status, RunStatus.COMPLETED)

    async def test_budget_exhaustion_halts_execution(self) -> None:
        """Feature 31: Exceeding max_model_calls transitions run to BUDGET_EXHAUSTED."""
        wf = self._create_multi_stage_workflow()
        wf_dict = wf.model_dump(mode="json")
        wf_dict["limits"]["max_model_calls"] = 3
        run_id = create_run(self.conn, wf_dict)

        # Simulate prior calls that exhausted budget before stage 1
        self.conn.execute(
            "UPDATE runs SET total_model_calls = 3 WHERE run_id = ?", (run_id,)
        )

        fake_provider = FakeProviderAdapter()
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        res = await engine.execute_run(run_id)
        self.assertEqual(res.status, RunStatus.BUDGET_EXHAUSTED)

        run_row = get_run(self.conn, run_id)
        self.assertEqual(run_row["status"], "BUDGET_EXHAUSTED")

    async def test_cancel_run_does_not_poison_scheduler_for_subsequent_runs(self) -> None:
        """Cancelling run 1 must not cancel the scheduler or break run 2 on the same engine."""
        wf = self._create_multi_stage_workflow()
        run_id_1 = create_run(self.conn, wf)
        run_id_2 = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        # Cancel run 1
        engine.cancel_run(run_id_1)
        self.assertEqual(get_run(self.conn, run_id_1)["status"], "CANCELLED")

        # Run 2 on the same engine instance must succeed without CancelledError
        res_2 = await engine.execute_run(run_id_2)
        self.assertEqual(res_2.status, RunStatus.COMPLETED)

    async def test_wall_clock_budget_exhaustion(self) -> None:
        """Feature 31: Exceeding max_wall_seconds transitions run to BUDGET_EXHAUSTED."""
        wf = self._create_multi_stage_workflow()
        wf_dict = wf.model_dump(mode="json")
        wf_dict["limits"]["max_wall_seconds"] = 1
        run_id = create_run(self.conn, wf_dict)

        fake_provider = FakeProviderAdapter()
        fake_provider.default_latency = 1.1

        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        res = await engine.execute_run(run_id)
        self.assertEqual(res.status, RunStatus.BUDGET_EXHAUSTED)
        self.assertIn("wall clock", res.error_message.lower())

        run_row = get_run(self.conn, run_id)
        self.assertEqual(run_row["status"], "BUDGET_EXHAUSTED")

    async def test_resolve_run_resets_failed_and_running_workers(self) -> None:
        """Feature 31: resolve_run resets worker states in both FAILED and RUNNING back to IDLE."""
        wf = self._create_multi_stage_workflow()
        run_id = create_run(self.conn, wf)

        self.conn.execute("UPDATE runs SET status = 'NEEDS_ATTENTION' WHERE run_id = ?", (run_id,))
        self.conn.execute(
            "UPDATE stages SET status = 'NEEDS_ATTENTION' WHERE run_id = ? AND stage_id = 'stage-1-analysis'",
            (run_id,),
        )
        self.conn.execute(
            "UPDATE workers SET status = 'FAILED' WHERE run_id = ? AND worker_id = 'worker-analyst'",
            (run_id,),
        )
        self.conn.execute(
            "UPDATE workers SET status = 'RUNNING' WHERE run_id = ? AND worker_id = 'worker-critic'",
            (run_id,),
        )

        fake_provider = FakeProviderAdapter()
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        engine.resolve_run(run_id, action="manual_intervention")

        workers = self.conn.execute("SELECT worker_id, status FROM workers WHERE run_id = ?", (run_id,)).fetchall()
        for w in workers:
            self.assertEqual(w["status"], "IDLE", f"Worker {w['worker_id']} was not reset to IDLE")


if __name__ == "__main__":
    unittest.main()
