"""Deterministic FakeProviderAdapter for AGYM Council.

Enables zero-cost, zero-token, fully reproducible testing of the Council
engine, dual-lease scheduler, release barriers, crash recovery, and web service.
Supports simulated latency, failure injection, synthetic dissent, active cancellation,
and startup attempt reconciliation.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from agym.council.models import (
    AccountAuthStatus,
    AccountStatus,
    AttemptReconciliation,
    AttemptSnapshot,
    AttemptStatus,
    CouncilOutputSections,
    ModelDescriptor,
    ProviderCapabilities,
    TurnEvent,
    TurnRequest,
    TurnResult,
    TurnStatus,
)
from agym.council.providers.base import ProviderAdapter


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeProviderAdapter(ProviderAdapter):
    """Deterministic, zero-billable model provider adapter for development and tests."""

    def __init__(
        self,
        default_latency: float = 0.0,
        dissent_mode: bool = True,
        fail_attempts: dict[str, str | Exception] | None = None,
        custom_responses: dict[str, dict[str, Any] | str] | None = None,
        available_models: list[ModelDescriptor] | None = None,
        account_statuses: dict[str, str | AccountStatus] | None = None,
        chunk_size: int = 50,
    ) -> None:
        self.default_latency = default_latency
        self.dissent_mode = dissent_mode
        self.fail_attempts = dict(fail_attempts or {})
        self.custom_responses = dict(custom_responses or {})
        self.account_statuses = dict(account_statuses or {})
        self.chunk_size = chunk_size

        # In-memory lifecycle registries
        self.active_tasks: dict[str, asyncio.Task[Any]] = {}
        self.completed_attempts: dict[str, TurnResult] = {}
        self.cancelled_attempts: set[str] = set()

        # Default simulated models
        self.available_models = available_models or [
            ModelDescriptor(
                id="fake-model-pro",
                display_name="Fake Pro (Reasoning)",
                provider="fake",
                context_window=1000000,
                capabilities=["structured_output", "streaming"],
            ),
            ModelDescriptor(
                id="fake-model-fast",
                display_name="Fake Fast (Flash)",
                provider="fake",
                context_window=128000,
                capabilities=["structured_output", "streaming"],
            ),
            ModelDescriptor(
                id="gemini-2.5-pro",
                display_name="Gemini 2.5 Pro (Simulated)",
                provider="fake",
                context_window=1000000,
                capabilities=["structured_output", "streaming"],
            ),
            ModelDescriptor(
                id="gemini-2.5-flash",
                display_name="Gemini 2.5 Flash (Simulated)",
                provider="fake",
                context_window=1000000,
                capabilities=["structured_output", "streaming"],
            ),
        ]

    # -----------------------------------------------------------------------
    # Dynamic Configuration
    # -----------------------------------------------------------------------

    def set_latency(self, latency: float) -> None:
        """Set simulated latency in seconds."""
        self.default_latency = latency

    def inject_failure(self, target: str, failure: str | Exception) -> None:
        """Register a failure for attempt_id, worker_id, or stage_id."""
        self.fail_attempts[target] = failure

    def clear_failures(self) -> None:
        """Reset all registered failures."""
        self.fail_attempts.clear()

    def set_dissent_mode(self, enabled: bool) -> None:
        """Toggle critic dissent simulation."""
        self.dissent_mode = enabled

    def set_account_status(self, account_ref: str, status: str | AccountStatus) -> None:
        """Override status for a specific account reference."""
        self.account_statuses[account_ref] = status

    def set_custom_response(self, target: str, response: dict[str, Any] | str) -> None:
        """Provide canned response text or JSON dictionary for a given target."""
        self.custom_responses[target] = response

    # -----------------------------------------------------------------------
    # ProviderAdapter ABC Implementation
    # -----------------------------------------------------------------------

    async def capabilities(self, account_ref: str | None = None) -> ProviderCapabilities:
        """Return declared provider capabilities."""
        return ProviderCapabilities(
            provider_name="fake",
            structured_output=True,
            resume_conversation=True,
            cancellation=True,
            tool_execution=False,
            token_usage=True,
            model_discovery=True,
            access_restrictions=False,
        )

    async def check_account(self, account_ref: str) -> AccountStatus:
        """Probe account readiness without leaking credentials."""
        if not account_ref or not str(account_ref).strip():
            return AccountStatus(
                account_ref=account_ref,
                status=AccountAuthStatus.UNVERIFIED,
                details="Missing account reference",
                message="Missing account reference",
            )

        if account_ref in self.account_statuses:
            cfg = self.account_statuses[account_ref]
            if isinstance(cfg, AccountStatus):
                return cfg
            return AccountStatus(
                account_ref=account_ref,
                status=cfg,
                details=f"Configured status: {cfg}",
                message=f"Configured status: {cfg}",
            )

        return AccountStatus(
            account_ref=account_ref,
            status=AccountAuthStatus.READY,
            details="Fake provider account is ready",
            message="Fake provider account is ready",
            cli_version="v1.0.0-fake",
        )

    async def discover_models(self, account_ref: str) -> list[ModelDescriptor]:
        """Return available model catalog."""
        return list(self.available_models)

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent | TurnResult]:
        """Execute a worker turn with deterministic responses, streaming, and error injection."""
        start_time = _utc_now_iso()

        # 1. Pre-cancellation check
        if request.attempt_id in self.cancelled_attempts:
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=TurnStatus.CANCELLED,
                error_message="Attempt cancelled before execution",
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            yield res
            return

        # 2. Failure injection check
        failure = (
            self.fail_attempts.get(request.attempt_id)
            or self.fail_attempts.get(request.worker_id)
            or self.fail_attempts.get(request.stage_id)
        )

        if failure is not None:
            if isinstance(failure, Exception):
                raise failure

            fail_str = str(failure).lower().replace("_", "-")
            if fail_str in ("malformed-output", "malformed_output"):
                yield TurnEvent(
                    attempt_id=request.attempt_id,
                    sequence=1,
                    event_type="started",
                    payload={"worker_id": request.worker_id, "model": request.model},
                )
                broken_json = '{"findings": "unterminated string...'
                yield TurnEvent(
                    attempt_id=request.attempt_id,
                    sequence=2,
                    event_type="delta",
                    delta=broken_json,
                )
                res = TurnResult(
                    attempt_id=request.attempt_id,
                    status=TurnStatus.MALFORMED_OUTPUT,
                    output_text=broken_json,
                    error_message="Malformed output: unexpected end of JSON",
                    error_code="MALFORMED_OUTPUT",
                    started_at=start_time,
                    finished_at=_utc_now_iso(),
                )
                self.completed_attempts[request.attempt_id] = res
                yield res
                return

            status_enum = TurnStatus(fail_str)
            res = TurnResult(
                attempt_id=request.attempt_id,
                status=status_enum,
                error_message=f"Simulated error: {fail_str}",
                error_code=fail_str.upper().replace("-", "_"),
                started_at=start_time,
                finished_at=_utc_now_iso(),
            )
            self.completed_attempts[request.attempt_id] = res
            yield res
            return

        # 3. Yield started event
        yield TurnEvent(
            attempt_id=request.attempt_id,
            sequence=1,
            event_type="started",
            payload={"worker_id": request.worker_id, "model": request.model},
        )

        # 4. Latency simulation with cancellation awareness
        if self.default_latency > 0:
            try:
                # Sleep in short slices to remain responsive to cancellation
                slices = max(1, int(self.default_latency / 0.02))
                slice_dur = self.default_latency / slices
                for _ in range(slices):
                    if request.attempt_id in self.cancelled_attempts:
                        raise asyncio.CancelledError()
                    await asyncio.sleep(slice_dur)
            except asyncio.CancelledError:
                self.cancelled_attempts.add(request.attempt_id)
                res = TurnResult(
                    attempt_id=request.attempt_id,
                    status=TurnStatus.CANCELLED,
                    error_message="Attempt cancelled during execution",
                    started_at=start_time,
                    finished_at=_utc_now_iso(),
                )
                self.completed_attempts[request.attempt_id] = res
                yield res
                return

        # 5. Generate deterministic content
        custom = (
            self.custom_responses.get(request.attempt_id)
            or self.custom_responses.get(request.worker_id)
            or self.custom_responses.get(request.stage_id)
        )

        if custom is not None:
            if isinstance(custom, dict):
                sections_dict = custom
                output_json = json.dumps(custom, indent=2)
            else:
                output_json = str(custom)
                try:
                    sections_dict = json.loads(output_json)
                except Exception:
                    sections_dict = {"findings": output_json}
        else:
            role = (request.role or "").lower()
            stage_id = request.stage_id.lower()

            if role in ("critic", "reviewer"):
                if self.dissent_mode:
                    findings = (
                        "CRITICAL OBJECTION: Proposal fails SLA latency requirements by 45% "
                        "and makes unverified consistency assumptions."
                    )
                    evidence = (
                        "Observed benchmark variance in supplied evidence; assumptions 2 and 4 "
                        "are contradicted by workload metrics."
                    )
                    uncertainties = (
                        "Unresolved whether alternative caching architecture can mitigate "
                        "latency within budget."
                    )
                    next_action = "Reject draft. Require revision stage before proceeding to synthesis."
                else:
                    findings = "Review passed: Proposal satisfies structural and throughput constraints."
                    evidence = "Matches standard patterns documented in baseline evidence."
                    uncertainties = "Minor questions regarding 10x traffic bursts."
                    next_action = "Approve proposal for stage synthesis."

            elif role in ("synthesizer", "coordinator") or "synthesize" in stage_id:
                prompt_lower = (request.prompt or "").lower()
                has_critic_objection = (
                    self.dissent_mode
                    or "critic" in prompt_lower
                    or "objection" in prompt_lower
                    or "dissent" in prompt_lower
                    or "reservation" in prompt_lower
                )
                if has_critic_objection:
                    findings = (
                        "Composite synthesis: Proposal approved with reservations. "
                        "PRESERVED DISSENT: Critic objections regarding SLA latency and memory budget "
                        "remain unresolved. No artificial consensus was invented."
                    )
                    evidence = "Analyst evidence accepted for core throughput; critic objections validated for peak load."
                    uncertainties = "Dispute regarding edge latency remains unverified without production stress tests."
                    next_action = "Deliver recommendation with attached dissent report for human decision."
                else:
                    findings = "Composite synthesis: All findings integrated and consensus validated across peer models."
                    evidence = "Evidence reconciled across all stage inputs."
                    uncertainties = "Standard deployment operational margins."
                    next_action = "Proceed to implementation."

            else:
                prompt_text = (request.prompt or "").strip()
                if not prompt_text:
                    findings = f"Analysis: Empty prompt received. Baseline default analysis completed for worker '{request.worker_id}'."
                else:
                    findings = (
                        f"Analysis: Viable path identified meeting objective for worker '{request.worker_id}'. "
                        "Baseline architecture satisfies 99.9% uptime."
                    )
                evidence = "Assumes standard container runtime with 4 vCPU and 16GB RAM."
                uncertainties = "Long-tail network latency under cross-region replication."
                next_action = "Submit analysis for peer review and critique."

            sections_dict = {
                "findings": findings,
                "evidence_or_assumptions": evidence,
                "uncertainties": uncertainties,
                "next_action": next_action,
            }

            # Populate any additional required sections
            for sec in request.required_sections:
                if sec not in sections_dict:
                    sections_dict[sec] = f"Generated {sec.replace('_', ' ')} for {request.worker_id}."

            output_json = json.dumps(sections_dict, indent=2)

        # 6. Stream thought and delta chunks
        seq = 2
        yield TurnEvent(
            attempt_id=request.attempt_id,
            sequence=seq,
            event_type="thought",
            delta=f"Analyzing prompt constraints, evaluating evidence for worker '{request.worker_id}', and formulating section findings...",
        )
        seq += 1

        chunk_size = max(1, self.chunk_size)
        for i in range(0, len(output_json), chunk_size):
            chunk = output_json[i : i + chunk_size]
            yield TurnEvent(
                attempt_id=request.attempt_id,
                sequence=seq,
                event_type="delta",
                delta=chunk,
            )
            seq += 1

        # 7. Construct and yield final TurnResult
        conv_handle = (
            request.conversation_handle
            or request.conversation_id
            or f"fake-conv-{request.worker_id}-{request.attempt_id[:6]}"
        )

        try:
            parsed_sections = CouncilOutputSections.model_validate(sections_dict)
        except Exception:
            parsed_sections = None

        res = TurnResult(
            attempt_id=request.attempt_id,
            status=TurnStatus.SUCCESS,
            conversation_handle=conv_handle,
            conversation_id=conv_handle,
            output_text=output_json,
            parsed_sections=parsed_sections,
            structured_output=sections_dict,
            observed_model=request.model or "fake-model-pro",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 250,
                "total_tokens": 350,
                "cost_usd": 0.0,
            },
            started_at=start_time,
            finished_at=_utc_now_iso(),
        )
        self.completed_attempts[request.attempt_id] = res
        yield res

    async def cancel(self, attempt_id: str) -> None:
        """Actively terminate an in-flight attempt. Must be idempotent."""
        self.cancelled_attempts.add(attempt_id)
        task = self.active_tasks.get(attempt_id)
        if task is not None and not task.done():
            task.cancel()

    async def reconcile(
        self, in_flight_attempts: list[AttemptSnapshot]
    ) -> list[AttemptReconciliation]:
        """Inspect interrupted attempts after restart and return reconciled statuses."""
        reconciled: list[AttemptReconciliation] = []
        for snap in in_flight_attempts:
            att_id = snap.attempt_id
            if att_id in self.completed_attempts:
                completed = self.completed_attempts[att_id]
                status = (
                    AttemptStatus.SUCCEEDED
                    if completed.status == TurnStatus.SUCCESS
                    else AttemptStatus.FAILED
                )
                reconciled.append(
                    AttemptReconciliation(
                        attempt_id=att_id,
                        reconciled_status=status,
                        reason="Attempt found in completed provider registry",
                        details="Attempt found in completed provider registry",
                        result=completed,
                    )
                )
            elif att_id in self.cancelled_attempts:
                reconciled.append(
                    AttemptReconciliation(
                        attempt_id=att_id,
                        reconciled_status=AttemptStatus.CANCELLED,
                        reason="Attempt was recorded as cancelled",
                        details="Attempt was recorded as cancelled",
                    )
                )
            else:
                reconciled.append(
                    AttemptReconciliation(
                        attempt_id=att_id,
                        reconciled_status=AttemptStatus.UNKNOWN,
                        reason=f"Process PID {snap.pid} not found in active fake provider state",
                        details=f"Process PID {snap.pid} not found in active fake provider state",
                    )
                )
        return reconciled
