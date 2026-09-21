"""Adversarial, Stress & Edge-Case Verification Suite for Milestone 4 (Features 22–32)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agym.council.artifacts import ArtifactStore, PathTraversalError
from agym.council.context import (
    DISSENT_MANDATE,
    assemble_prompt,
    setup_attempt_workspace,
)
from agym.council.engine import CouncilEngine
from agym.council.models import (
    ArtifactRef,
    Attempt,
    AttemptStatus,
    ExecutionMode,
    LimitsConfig,
    RunStatus,
    StageConfig,
    StageContextPolicy,
    StageKind,
    StageStatus,
    TurnResult,
    TurnStatus,
    WorkerConfig,
    WorkflowConfig,
    WorkflowInput,
)
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.recovery import reconcile_startup_crashes
from agym.council.scheduler import CapacityScheduler, claim_attempt_atomic
from agym.council.storage import (
    create_attempt,
    create_run,
    get_attempt,
    get_run,
    get_stage,
    init_db,
    list_artifacts_for_run,
    list_issues_for_run,
    list_released_artifacts_for_stages,
    update_attempt,
    update_run_status,
)


class TestAdversarialScheduler(unittest.IsolatedAsyncioTestCase):
    """High-concurrency stress testing and invariant verification for CapacityScheduler."""

    async def test_high_concurrency_dual_lease_invariants(self) -> None:
        """20 workers across 5 accounts: per-account is strictly <= 1 and global <= 4."""
        global_limit = 4
        per_account_limit = 1
        scheduler = CapacityScheduler(global_limit=global_limit, per_account_limit=per_account_limit)

        num_accounts = 5
        workers_per_account = 4
        total_workers = num_accounts * workers_per_account

        lock = asyncio.Lock()
        global_active = 0
        account_active: dict[str, int] = {f"acc_{i}": 0 for i in range(num_accounts)}
        max_seen_global = 0
        max_seen_per_account: dict[str, int] = {f"acc_{i}": 0 for i in range(num_accounts)}
        completed_tasks = 0

        async def worker_sim(worker_id: int, account_id: str) -> None:
            nonlocal global_active, max_seen_global, completed_tasks
            async with scheduler.acquire_lease(account_id):
                async with lock:
                    global_active += 1
                    account_active[account_id] += 1

                    if global_active > max_seen_global:
                        max_seen_global = global_active
                    if account_active[account_id] > max_seen_per_account[account_id]:
                        max_seen_per_account[account_id] = account_active[account_id]

                    # Strict assertions inside active lease
                    self.assertLessEqual(account_active[account_id], per_account_limit)
                    self.assertLessEqual(global_active, global_limit)

                # Simulated payload
                await asyncio.sleep(0.01)

                async with lock:
                    account_active[account_id] -= 1
                    global_active -= 1
                    completed_tasks += 1

        tasks = [
            asyncio.create_task(worker_sim(i, f"acc_{i % num_accounts}"))
            for i in range(total_workers)
        ]

        await asyncio.gather(*tasks)

        self.assertEqual(completed_tasks, total_workers)
        self.assertLessEqual(max_seen_global, global_limit)
        self.assertEqual(max_seen_global, global_limit)
        for acc, m in max_seen_per_account.items():
            self.assertEqual(m, 1, f"Account {acc} exceeded serialization limit!")


class TestAdversarialBarriersAndLeakage(unittest.IsolatedAsyncioTestCase):
    """Stress tests verifying physical barrier isolation and zero premature leakage."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "adv_test.db"
        self.conn = init_db(self.db_path)
        self.artifact_store = ArtifactStore(self.root / "cas")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_early_finisher_leakage_barrier(self) -> None:
        """In a 2-worker stage, early finisher's output must remain invisible until stage completion."""
        w1 = WorkerConfig(
            id="w-fast",
            name="Fast Worker",
            account_ref="acc-1",
            model="fake-model-fast",
            role="Analyst",
            instructions="fast",
            task="task",
        )
        w2 = WorkerConfig(
            id="w-slow",
            name="Slow Worker",
            account_ref="acc-2",
            model="fake-model-pro",
            role="Analyst",
            instructions="slow",
            task="task",
        )

        stage = StageConfig(
            id="stage-parallel",
            kind=StageKind.INDEPENDENT,
            workers=["w-fast", "w-slow"],
            instruction="parallel work",
        )

        wf = WorkflowConfig(
            name="Parallel Stage Leak Test",
            goal="Ensure zero early finisher leakage",
            inputs=[WorkflowInput(id="doc", description="desc", value="val")],
            workers=[w1, w2],
            stages=[stage],
            final_stage="stage-parallel",
        )
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        # Hook into w-fast completion to inspect DB before w-slow finishes
        barrier_verified = False

        original_execute = engine._execute_worker_turn

        async def intercepted_execute(*args: Any, **kwargs: Any) -> tuple[str, TurnResult]:
            nonlocal barrier_verified
            wid, res = await original_execute(*args, **kwargs)
            if wid == "w-fast":
                # Check DB at this instant: w-fast finished, but w-slow is still running
                # Query released artifacts from SQLite: must be 0!
                released_arts = list_released_artifacts_for_stages(self.conn, run_id, ["stage-parallel"])
                self.assertEqual(len(released_arts), 0)

                # Check w-fast draft artifact: must exist but released == 0
                all_arts = list_artifacts_for_run(self.conn, run_id, stage_id="stage-parallel")
                fast_art = next((a for a in all_arts if a["name"].startswith("output_stage-parallel_w-fast")), None)
                self.assertIsNotNone(fast_art)
                self.assertEqual(fast_art["released"], 0)
                barrier_verified = True
            return wid, res

        engine._execute_worker_turn = intercepted_execute  # type: ignore[assignment]

        res = await engine.run_stage(run_id, "stage-parallel")
        self.assertEqual(res.status, StageStatus.COMPLETED)
        self.assertTrue(barrier_verified, "Barrier verification hook did not run")

        # Now that stage is COMPLETED, all artifacts (prompts + outputs) must be released = 1
        final_released = list_released_artifacts_for_stages(self.conn, run_id, ["stage-parallel"])
        self.assertEqual(len(final_released), 4)
        output_arts = [a for a in final_released if a["name"].startswith("output_")]
        self.assertEqual(len(output_arts), 2)
        self.assertTrue(all(a["released"] == 1 for a in final_released))


    def test_traversal_attack_variations_on_workspace(self) -> None:
        """Various directory traversal payloads must be rejected when staging workspace."""
        res = self.artifact_store.store(b"Secret data")
        sha = res.content_hash

        attacks = [
            "../escaped.txt",
            "../../etc/shadow",
            "dir/../../../secret.key",
            "/etc/passwd",
            "\\Windows\\System32\\cmd.exe",
            "C:exploit.dll",
            "subdir/..",
            ".",
        ]

        for payload in attacks:
            art = ArtifactRef(
                id=sha,
                run_id="run-traversal",
                stage_id="stage-prior",
                worker_id="w1",
                name=payload,
                path="/tmp/test",
                size_bytes=11,
                sha256=sha,
                released=True,
            )
            with self.subTest(payload=payload):
                with self.assertRaises((PathTraversalError, ValueError)):
                    setup_attempt_workspace(
                        run_id="run-traversal",
                        stage_id="stage-curr",
                        worker_id="w-target",
                        attempt_id="att-x",
                        released_artifacts=[art],
                        artifact_store=self.artifact_store,
                        base_dir=self.root,
                    )


class TestAdversarialRetriesAndCrashRecovery(unittest.IsolatedAsyncioTestCase):
    """Stress tests for task retries, error recovery, and simulated process crash."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "retry_crash.db"
        self.conn = init_db(self.db_path)
        self.artifact_store = ArtifactStore(self.root / "cas")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_retry_on_transient_failure_succeeds(self) -> None:
        """Worker fails attempt 1, succeeds attempt 2: stage completes successfully."""
        w1 = WorkerConfig(
            id="w-retry",
            name="Retry Worker",
            account_ref="acc-1",
            model="fake-model-pro",
            role="Analyst",
            instructions="inst",
            task="task",
        )
        stage = StageConfig(
            id="stage-retry",
            kind=StageKind.INDEPENDENT,
            workers=["w-retry"],
            instruction="inst",
        )
        wf = WorkflowConfig(
            name="Retry Test",
            goal="Goal",
            inputs=[WorkflowInput(id="i1", description="desc", value="val")],
            limits=LimitsConfig(max_retries_per_task=2),
            workers=[w1],
            stages=[stage],
            final_stage="stage-retry",
        )
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        attempt_count = 0
        original_run_turn = fake_provider.run_turn

        async def failing_once_turn(request: Any) -> Any:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                # Fail first attempt
                yield TurnResult(
                    attempt_id=request.attempt_id,
                    status=TurnStatus.UNAVAILABLE,
                    error_message="Transient network hiccup",
                )
            else:
                # Succeed second attempt
                async for item in original_run_turn(request):
                    yield item

        fake_provider.run_turn = failing_once_turn  # type: ignore[assignment]

        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        stage_res = await engine.run_stage(run_id, "stage-retry")
        self.assertEqual(stage_res.status, StageStatus.COMPLETED)
        self.assertEqual(attempt_count, 2)

        # Verify DB attempt records
        attempts = self.conn.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY attempt_number ASC", (run_id,)
        ).fetchall()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status"], "FAILED")
        self.assertEqual(attempts[1]["status"], "SUCCEEDED")

    async def test_context_continuity_continue_vs_fresh(self) -> None:
        """context: continue resumes conversation handle; context: fresh does not."""
        w1 = WorkerConfig(
            id="worker-w",
            name="Worker W",
            account_ref="acc-1",
            model="fake-model-pro",
            role="Analyst",
            instructions="inst",
            task="task",
        )
        stage_1 = StageConfig(
            id="s1",
            kind=StageKind.INDEPENDENT,
            workers=["worker-w"],
            instruction="step 1",
            context=StageContextPolicy.FRESH,
        )
        stage_2 = StageConfig(
            id="s2",
            kind=StageKind.INDEPENDENT,
            workers=["worker-w"],
            input_stages=["s1"],
            instruction="step 2",
            context=StageContextPolicy.CONTINUE,  # Must receive handle from s1!
        )
        stage_3 = StageConfig(
            id="s3",
            kind=StageKind.INDEPENDENT,
            workers=["worker-w"],
            input_stages=["s2"],
            instruction="step 3",
            context=StageContextPolicy.FRESH,  # Must be reset to None!
        )

        wf = WorkflowConfig(
            name="Context Policy Test",
            goal="Goal",
            inputs=[WorkflowInput(id="i1", description="desc", value="val")],
            workers=[w1],
            stages=[stage_1, stage_2, stage_3],
            final_stage="s3",
        )
        run_id = create_run(self.conn, wf)

        fake_provider = FakeProviderAdapter()
        observed_handles: dict[str, str | None] = {}

        original_run_turn = fake_provider.run_turn

        async def inspecting_turn(request: Any) -> Any:
            observed_handles[request.stage_id] = request.conversation_handle
            async for item in original_run_turn(request):
                yield item

        fake_provider.run_turn = inspecting_turn  # type: ignore[assignment]

        engine = CouncilEngine(
            db_path=self.db_path,
            artifact_store=self.artifact_store,
            provider=fake_provider,
            data_root=self.root,
        )

        res = await engine.execute_run(run_id)
        self.assertEqual(res.status, RunStatus.COMPLETED)

        # Stage 1: fresh -> handle should be None
        self.assertIsNone(observed_handles["s1"])
        # Stage 2: continue -> handle should be populated from s1's result
        self.assertIsNotNone(observed_handles["s2"])
        self.assertIn("conv-", observed_handles["s2"] or "")
        # Stage 3: fresh -> handle must be None again!
        self.assertIsNone(observed_handles["s3"])

    async def test_real_subprocess_termination_recovery(self) -> None:
        """Simulate an actual OS child process termination (kill -9) and verify crash recovery."""
        # Spawn a genuine long-running sleep process to simulate an active attempt
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        pid = proc.pid
        start_time = time.time()

        run_data = {
            "name": "Live Subprocess Crash Test",
            "goal": "Verify real PID kill recovery",
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
                    "id": "stage-proc-crash",
                    "kind": "independent",
                    "workers": ["w1"],
                    "instruction": "inst",
                }
            ],
            "final_stage": "stage-proc-crash",
        }
        run_id = create_run(self.conn, run_data)
        update_run_status(self.conn, run_id, status=RunStatus.RUNNING)

        attempt = Attempt(
            id="att-real-proc",
            run_id=run_id,
            stage_id="stage-proc-crash",
            worker_id="w1",
            account_ref="acc1",
            model="model1",
            status=AttemptStatus.RUNNING,
            pid=pid,
            process_start_time=start_time,
        )
        create_attempt(self.conn, attempt)

        # Terminate the process forcefully (simulate crash / SIGKILL)
        proc.kill()
        proc.wait()

        # Run crash reconciliation on startup
        reconciled = await reconcile_startup_crashes(self.conn)

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].attempt_id, "att-real-proc")
        self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.UNKNOWN)

        # Run and stage must be in NEEDS_ATTENTION
        run_row = get_run(self.conn, run_id)
        stage_row = get_stage(self.conn, run_id, "stage-proc-crash")
        self.assertEqual(run_row["status"], "NEEDS_ATTENTION")
        self.assertEqual(stage_row["status"], "NEEDS_ATTENTION")


if __name__ == "__main__":
    unittest.main()
