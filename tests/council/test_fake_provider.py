"""Unit, boundary, and adversarial tests for AGYM Council FakeProviderAdapter.

Covers:
- Capability discovery and model catalog discovery
- Account readiness checking
- Streaming turn execution and event structure
- Canonical output section formatting
- Zero-billable API cost invariant
- Failure and status code injection (rate-limited, timeout, needs-auth, malformed-output)
- Synthetic latency and active cancellation
- Role-specific dissent simulation (critic objection & synthesizer dissent retention)
- Conversation handle continuity and isolation
- Crash recovery attempt reconciliation
- High-concurrency multi-worker execution
"""

from __future__ import annotations

import asyncio
import time
import unittest

from agym.council.models import (
    AccountAuthStatus,
    AttemptSnapshot,
    AttemptStatus,
    TurnEvent,
    TurnRequest,
    TurnResult,
    TurnStatus,
)
from agym.council.providers.fake import FakeProviderAdapter


class TestFakeProviderBasics(unittest.IsolatedAsyncioTestCase):
    """Tier 1: Functional baseline tests for FakeProviderAdapter."""

    async def asyncSetUp(self) -> None:
        self.adapter = FakeProviderAdapter(default_latency=0.0)

    async def test_capabilities(self) -> None:
        caps = await self.adapter.capabilities("profile-test")
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.resume_conversation)
        self.assertTrue(caps.cancellation)
        self.assertTrue(caps.token_usage)
        self.assertTrue(caps.model_discovery)

    async def test_discover_models(self) -> None:
        models = await self.adapter.discover_models("profile-test")
        self.assertGreaterEqual(len(models), 4)
        model_ids = {m.id for m in models}
        self.assertTrue({"fake-model-pro", "fake-model-fast"}.issubset(model_ids))
        for m in models:
            self.assertGreater(m.context_window, 0)

    async def test_check_account_ready(self) -> None:
        st = await self.adapter.check_account("profile-test")
        self.assertEqual(st.status, AccountAuthStatus.READY)
        self.assertIn("ready", st.message.lower())

    async def test_check_account_empty_raises_or_unverified(self) -> None:
        st = await self.adapter.check_account("")
        self.assertEqual(st.status, AccountAuthStatus.UNVERIFIED)

    async def test_run_turn_streaming_events(self) -> None:
        req = TurnRequest(
            attempt_id="att-stream-1",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w-stream",
            account_ref="prof-1",
            model="fake-model-pro",
            prompt="Analyze options",
            role="analyst",
        )

        items = []
        async for item in self.adapter.run_turn(req):
            items.append(item)

        # Initial item must be TurnEvent with event_type="started"
        self.assertIsInstance(items[0], TurnEvent)
        self.assertEqual(items[0].event_type, "started")

        # Intermediate items must be TurnEvent with event_type="delta"
        delta_events = [x for x in items if isinstance(x, TurnEvent) and x.event_type == "delta"]
        self.assertGreater(len(delta_events), 0)

        # Terminal item must be TurnResult with status="success"
        terminal = items[-1]
        self.assertIsInstance(terminal, TurnResult)
        self.assertEqual(terminal.status, TurnStatus.SUCCESS)

    async def test_run_turn_required_json_sections(self) -> None:
        req = TurnRequest(
            attempt_id="att-sections-1",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof-1",
            model="fake-model-pro",
            prompt="Standard task",
            role="analyst",
        )

        result: TurnResult | None = None
        async for item in self.adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item

        self.assertIsNotNone(result)
        self.assertIsNotNone(result.structured_output)
        struct = result.structured_output
        self.assertIn("findings", struct)
        self.assertIn("evidence_or_assumptions", struct)
        self.assertIn("uncertainties", struct)
        self.assertIn("next_action", struct)

    async def test_zero_billable_guarantee(self) -> None:
        req = TurnRequest(
            attempt_id="att-billable-1",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof-1",
            model="fake-model-pro",
            prompt="Free call test",
        )
        result = None
        async for item in self.adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item

        self.assertIsNotNone(result)
        self.assertEqual(result.usage.get("cost_usd"), 0.0)


class TestFakeProviderErrorInjection(unittest.IsolatedAsyncioTestCase):
    """Tier 2: Boundary, synthetic latency, and error injection tests."""

    async def asyncSetUp(self) -> None:
        self.adapter = FakeProviderAdapter()

    async def _run_for_result(self, req: TurnRequest) -> TurnResult:
        result = None
        async for item in self.adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item
        if result is None:
            self.fail("No TurnResult was yielded")
        return result

    async def test_error_injection_rate_limited(self) -> None:
        self.adapter.inject_failure("att-rl", "rate-limited")
        req = TurnRequest(
            attempt_id="att-rl", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        res = await self._run_for_result(req)
        self.assertEqual(res.status, TurnStatus.RATE_LIMITED)

    async def test_error_injection_needs_auth(self) -> None:
        self.adapter.inject_failure("att-auth", "needs-auth")
        req = TurnRequest(
            attempt_id="att-auth", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        res = await self._run_for_result(req)
        self.assertEqual(res.status, TurnStatus.NEEDS_AUTH)

    async def test_error_injection_timeout(self) -> None:
        self.adapter.inject_failure("att-to", "timeout")
        req = TurnRequest(
            attempt_id="att-to", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        res = await self._run_for_result(req)
        self.assertEqual(res.status, TurnStatus.TIMEOUT)

    async def test_error_injection_permission_blocked(self) -> None:
        self.adapter.inject_failure("att-pb", "permission-blocked")
        req = TurnRequest(
            attempt_id="att-pb", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        res = await self._run_for_result(req)
        self.assertEqual(res.status, TurnStatus.PERMISSION_BLOCKED)

    async def test_error_injection_unavailable(self) -> None:
        self.adapter.inject_failure("att-un", "unavailable")
        req = TurnRequest(
            attempt_id="att-un", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        res = await self._run_for_result(req)
        self.assertEqual(res.status, TurnStatus.UNAVAILABLE)

    async def test_error_injection_malformed_output(self) -> None:
        self.adapter.inject_failure("att-mal", "malformed-output")
        req = TurnRequest(
            attempt_id="att-mal", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        res = await self._run_for_result(req)
        self.assertEqual(res.status, TurnStatus.MALFORMED_OUTPUT)
        self.assertIn("unterminated", res.output_text or "")

    async def test_error_injection_raw_exception(self) -> None:
        self.adapter.inject_failure("att-exc", RuntimeError("Simulated network socket crash"))
        req = TurnRequest(
            attempt_id="att-exc", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        with self.assertRaises(RuntimeError) as cm:
            async for _ in self.adapter.run_turn(req):
                pass
        self.assertIn("Simulated network socket crash", str(cm.exception))

    async def test_synthetic_latency_timing(self) -> None:
        self.adapter.set_latency(0.06)
        req = TurnRequest(
            attempt_id="att-lat", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        start = time.perf_counter()
        res = await self._run_for_result(req)
        elapsed = time.perf_counter() - start
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertEqual(res.status, TurnStatus.SUCCESS)

    async def test_cancellation_active_turn(self) -> None:
        self.adapter.set_latency(0.5)
        req = TurnRequest(
            attempt_id="att-cancel", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )

        async def run_in_bg() -> TurnResult | None:
            r = None
            async for item in self.adapter.run_turn(req):
                if isinstance(item, TurnResult):
                    r = item
            return r

        task = asyncio.create_task(run_in_bg())
        # Give task a moment to enter sleep
        await asyncio.sleep(0.04)
        await self.adapter.cancel("att-cancel")
        res = await task
        self.assertIsNotNone(res)
        self.assertEqual(res.status, TurnStatus.CANCELLED)

    async def test_cancellation_idempotent(self) -> None:
        # Cancelling an unknown or already cancelled attempt must not raise
        await self.adapter.cancel("nonexistent-1")
        await self.adapter.cancel("nonexistent-1")


class TestDissentSimulationAndRoles(unittest.IsolatedAsyncioTestCase):
    """Tier 3: Critic dissent and synthesizer dissent preservation tests."""

    async def test_critic_dissent_simulation_active(self) -> None:
        adapter = FakeProviderAdapter(dissent_mode=True)
        req = TurnRequest(
            attempt_id="att-critic-dissent",
            run_id="run-1",
            stage_id="critique",
            worker_id="critic",
            account_ref="prof-critic",
            model="fake-model-pro",
            role="critic",
            prompt="Critique the analyst proposal",
        )
        result = None
        async for item in adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item

        self.assertIsNotNone(result)
        findings = result.structured_output["findings"]
        self.assertIn("CRITICAL OBJECTION", findings)

    async def test_critic_dissent_simulation_disabled(self) -> None:
        adapter = FakeProviderAdapter(dissent_mode=False)
        req = TurnRequest(
            attempt_id="att-critic-nodissent",
            run_id="run-1",
            stage_id="critique",
            worker_id="critic",
            account_ref="prof-critic",
            model="fake-model-pro",
            role="critic",
            prompt="Review the proposal",
        )
        result = None
        async for item in adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item

        self.assertIsNotNone(result)
        findings = result.structured_output["findings"]
        self.assertNotIn("CRITICAL OBJECTION", findings)
        self.assertIn("Review passed", findings)

    async def test_synthesizer_preserves_critic_dissent(self) -> None:
        adapter = FakeProviderAdapter(dissent_mode=True)
        req = TurnRequest(
            attempt_id="att-synth",
            run_id="run-1",
            stage_id="answer",
            worker_id="coordinator",
            account_ref="prof-coord",
            model="fake-model-pro",
            role="synthesizer",
            prompt="Synthesize inputs including critic objection about latency",
        )
        result = None
        async for item in adapter.run_turn(req):
            if isinstance(item, TurnResult):
                result = item

        self.assertIsNotNone(result)
        findings = result.structured_output["findings"]
        # Mandatory invariant: Do not invent consensus!
        self.assertIn("PRESERVED DISSENT", findings)
        self.assertIn("No artificial consensus was invented", findings)

    async def test_conversation_handle_continuity(self) -> None:
        adapter = FakeProviderAdapter()
        req1 = TurnRequest(
            attempt_id="att-turn1",
            run_id="run-1",
            stage_id="independent",
            worker_id="analyst",
            account_ref="prof-1",
            model="fake-model-pro",
            prompt="Turn 1 prompt",
        )
        res1 = None
        async for item in adapter.run_turn(req1):
            if isinstance(item, TurnResult):
                res1 = item

        self.assertIsNotNone(res1)
        handle1 = res1.conversation_handle
        self.assertIsNotNone(handle1)

        # Turn 2 passes the conversation handle from Turn 1
        req2 = TurnRequest(
            attempt_id="att-turn2",
            run_id="run-1",
            stage_id="critique",
            worker_id="analyst",
            account_ref="prof-1",
            model="fake-model-pro",
            prompt="Turn 2 prompt",
            conversation_handle=handle1,
        )
        res2 = None
        async for item in adapter.run_turn(req2):
            if isinstance(item, TurnResult):
                res2 = item

        self.assertIsNotNone(res2)
        self.assertEqual(res2.conversation_handle, handle1)

    async def test_same_model_distinct_worker_conversations(self) -> None:
        adapter = FakeProviderAdapter()
        req_a = TurnRequest(
            attempt_id="att-worker-a", run_id="run-1", stage_id="stage-1",
            worker_id="worker_a", account_ref="prof-1", model="fake-model-pro", role="analyst", prompt="A"
        )
        req_b = TurnRequest(
            attempt_id="att-worker-b", run_id="run-1", stage_id="stage-1",
            worker_id="worker_b", account_ref="prof-2", model="fake-model-pro", role="critic", prompt="B"
        )

        res_a = None
        async for item in adapter.run_turn(req_a):
            if isinstance(item, TurnResult):
                res_a = item

        res_b = None
        async for item in adapter.run_turn(req_b):
            if isinstance(item, TurnResult):
                res_b = item

        self.assertNotEqual(res_a.conversation_handle, res_b.conversation_handle)
        self.assertIn("Analysis", res_a.structured_output["findings"])
        self.assertIn("CRITICAL OBJECTION", res_b.structured_output["findings"])


class TestReconciliationAndHighConcurrency(unittest.IsolatedAsyncioTestCase):
    """Tier 4: Crash recovery reconciliation and concurrent multi-worker execution."""

    async def test_reconcile_completed_attempt(self) -> None:
        adapter = FakeProviderAdapter()
        req = TurnRequest(
            attempt_id="att-rec-done", run_id="r1", stage_id="s1", worker_id="w1",
            account_ref="p1", model="m", prompt="p"
        )
        async for _ in adapter.run_turn(req):
            pass

        recon = await adapter.reconcile([
            AttemptSnapshot(attempt_id="att-rec-done", run_id="r1", stage_id="s1", worker_id="w1", account_ref="p1", status=AttemptStatus.RUNNING)
        ])
        self.assertEqual(len(recon), 1)
        self.assertEqual(recon[0].reconciled_status, AttemptStatus.SUCCEEDED)
        self.assertIsNotNone(recon[0].result)

    async def test_reconcile_cancelled_attempt(self) -> None:
        adapter = FakeProviderAdapter()
        await adapter.cancel("att-rec-cancel")

        recon = await adapter.reconcile([
            AttemptSnapshot(attempt_id="att-rec-cancel", run_id="r1", stage_id="s1", worker_id="w2", account_ref="p2", status=AttemptStatus.RUNNING)
        ])
        self.assertEqual(len(recon), 1)
        self.assertEqual(recon[0].reconciled_status, AttemptStatus.CANCELLED)

    async def test_reconcile_unknown_attempt(self) -> None:
        adapter = FakeProviderAdapter()
        recon = await adapter.reconcile([
            AttemptSnapshot(attempt_id="att-rec-unknown", run_id="r1", stage_id="s1", worker_id="w3", account_ref="p3", status=AttemptStatus.RUNNING, pid=99999)
        ])
        self.assertEqual(len(recon), 1)
        self.assertEqual(recon[0].reconciled_status, AttemptStatus.UNKNOWN)
        self.assertIn("99999", recon[0].details)

    async def test_concurrent_multi_worker_turns(self) -> None:
        adapter = FakeProviderAdapter(default_latency=0.01)

        async def worker_task(w_id: str, role: str) -> TurnResult:
            req = TurnRequest(
                attempt_id=f"att-{w_id}",
                run_id="run-concurrent",
                stage_id="stage-1",
                worker_id=w_id,
                account_ref=f"prof-{w_id}",
                model="fake-model-pro",
                role=role,
                prompt=f"Task for {w_id}",
            )
            r = None
            async for item in adapter.run_turn(req):
                if isinstance(item, TurnResult):
                    r = item
            return r

        results = await asyncio.gather(
            worker_task("coord", "coordinator"),
            worker_task("analyst", "analyst"),
            worker_task("critic", "critic"),
            worker_task("synth", "synthesizer"),
        )

        self.assertEqual(len(results), 4)
        for r in results:
            self.assertEqual(r.status, TurnStatus.SUCCESS)
            self.assertIn("findings", r.structured_output)


if __name__ == "__main__":
    unittest.main()
