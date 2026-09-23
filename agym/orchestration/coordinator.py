"""AGYM orchestration coordinator runtime.

Single responsibility: Maintain the coordinator model conversation.
Acts only as: state/context -> model -> validated decision.
Does not execute worker actions.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

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
    ModelRunner,
    ModelSession,
    OrchestrationBudget,
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
from agym.orchestration.prompts import (
    COORDINATOR_SYSTEM_PROMPT,
    build_assessment_prompt,
    build_observation_prompt,
    format_coordinator_observation,
)
from agym.orchestration.protocol import (
    ActionValidationError,
    ForbiddenFieldError,
    InitialCoordinatorResponse,
    MalformedResponseError,
    ProtocolError,
    ProtocolSchemaError,
    extract_json_payload,
    parse_coordinator_action,
    parse_initial_response,
    validate_coordinator_action,
)
from agym.orchestration.runner import classify_failure

logger = logging.getLogger("agym.orchestration.coordinator")

__all__ = [
    # Main coordinator implementation
    "CoordinatorClient",
    "CoordinatorRuntime",
    # Start and failure containers
    "CoordinatorStartResult",
    "CoordinatorFailure",
    # Exceptions
    "CoordinatorError",
    "CoordinatorCrashError",
    "CoordinatorTimeoutError",
    "CoordinatorClosedError",
]


# ============================================================================
# 1. Custom Exceptions & Failure Descriptors
# ============================================================================


class CoordinatorError(Exception):
    """Base exception for all coordinator errors."""


class CoordinatorCrashError(CoordinatorError):
    """Typed failure raised when the coordinator process crashes or fails fatally."""

    def __init__(
        self,
        message: str,
        *,
        run_id: RunId | str | None = None,
        conversation_id: ConversationId | str | None = None,
        round_number: int = 0,
        exit_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.run_id = RunId(run_id) if run_id is not None else None
        self.conversation_id = ConversationId(conversation_id) if conversation_id is not None else None
        self.round_number = round_number
        self.exit_code = exit_code
        self.cause = cause
        self.failure = CoordinatorFailure(
            error_type="CoordinatorCrash",
            message=message,
            run_id=self.run_id,
            conversation_id=self.conversation_id,
            round_number=self.round_number,
            exit_code=self.exit_code,
            cause=str(cause) if cause else None,
        )


class CoordinatorTimeoutError(CoordinatorCrashError):
    """Typed failure raised when a coordinator turn times out."""


class CoordinatorClosedError(CoordinatorError):
    """Raised when an operation is attempted on a closed coordinator client."""


@dataclass
class CoordinatorFailure:
    """Typed failure record describing coordinator crash or fatal error."""

    error_type: str
    message: str
    run_id: RunId | None = None
    conversation_id: ConversationId | None = None
    round_number: int = 0
    exit_code: int | None = None
    cause: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "run_id": str(self.run_id) if self.run_id else None,
            "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            "round_number": self.round_number,
            "exit_code": self.exit_code,
            "cause": self.cause,
        }


@dataclass
class CoordinatorStartResult:
    """Result of starting the coordinator runtime."""

    assessment: TaskAssessment
    action: CoordinatorAction
    conversation_id: ConversationId

    def __iter__(self):
        return iter((self.assessment, self.action, self.conversation_id))


# ============================================================================
# 2. Runner Session Adapter
# ============================================================================


class _RunnerSessionAdapter:
    """Adapts a bare ModelRunner into a persistent ModelSession."""

    def __init__(
        self,
        runner: ModelRunner,
        *,
        run_id: RunId,
        profile_name: str | None = None,
        strategy: ExecutionStrategy = ExecutionStrategy.STANDARD,
        timeout_seconds: float = 300.0,
        conversation_id: ConversationId | str | None = None,
    ) -> None:
        self._runner = runner
        self._run_id = run_id
        self._profile_name = profile_name
        self._strategy = strategy
        self._timeout = timeout_seconds
        self._conversation_id = ConversationId(conversation_id or f"conv-{run_id}")
        self._turn_count = 0
        self._is_closed = False

    @property
    def conversation_id(self) -> ConversationId:
        return self._conversation_id

    def send(self, prompt: str, timeout_seconds: float | None = None) -> ModelResult:
        if self._is_closed:
            raise RuntimeError("Session is closed")
        self._turn_count += 1
        inv_id = InvocationId(f"{self._conversation_id}-{self._turn_count}")
        invocation = ModelInvocation(
            invocation_id=inv_id,
            run_id=self._run_id,
            worker_id=WorkerId("coordinator"),
            role=WorkerRole.GENERAL,
            strategy=self._strategy,
            prompt=prompt,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else self._timeout,
            conversation_id=self._conversation_id,
        )
        res = self._runner.run(invocation, profile_name=self._profile_name)
        if res.conversation_id:
            self._conversation_id = res.conversation_id
        return res

    def close(self) -> None:
        self._is_closed = True


# ============================================================================
# 3. Coordinator Client Implementation
# ============================================================================


class CoordinatorClient:
    """Coordinator client runtime maintaining the coordinator model conversation.

    Conforms to CoordinatorClient protocol.
    Acts exclusively as: state/context -> model -> validated decision.
    Does not execute worker actions, choose physical profiles, or manipulate host processes.
    """

    def __init__(
        self,
        runner: ModelRunner | None = None,
        *,
        session: ModelSession | None = None,
        run_store: RunStore | None = None,
        run_id: RunId | str | None = None,
        profile_name: str | None = None,
        strategy: ExecutionStrategy = ExecutionStrategy.STANDARD,
        timeout_seconds: float = 300.0,
        max_correction_attempts: int = 2,
    ) -> None:
        if runner is None and session is None:
            raise ValueError("Either 'runner' or 'session' must be provided to CoordinatorClient")

        self.runner = runner
        self._session = session
        self.run_store = run_store
        self.run_id = RunId(run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
        self.profile_name = profile_name
        self.strategy = (
            ExecutionStrategy(strategy)
            if isinstance(strategy, str)
            else strategy
        )
        self.timeout_seconds = float(timeout_seconds)
        self.max_correction_attempts = int(max_correction_attempts)

        self._conversation_id: ConversationId | None = None
        if session is not None and getattr(session, "conversation_id", None):
            self._conversation_id = ConversationId(session.conversation_id)

        self._round_number = 0
        self._is_closed = False
        self._is_resumed = False
        self._known_worker_ids: set[WorkerId] = set()
        self._task_assessment: TaskAssessment | None = None
        self._initial_action: CoordinatorAction | None = None

    @property
    def conversation_id(self) -> ConversationId | None:
        """The persistent identifier of the current coordinator conversation."""
        if self._session is not None and getattr(self._session, "conversation_id", None):
            return ConversationId(self._session.conversation_id)
        return self._conversation_id

    @property
    def is_closed(self) -> bool:
        """Whether this coordinator client runtime has been closed."""
        return self._is_closed

    @property
    def round_number(self) -> int:
        """The current coordinator round number."""
        return self._round_number

    # ------------------------------------------------------------------------
    # Session Management & Creation
    # ------------------------------------------------------------------------

    def _create_session(
        self,
        conversation_id: ConversationId | str | None = None,
    ) -> ModelSession:
        """Create a new or resumed ModelSession instance."""
        cid = conversation_id or self._conversation_id
        if self.runner is not None and hasattr(self.runner, "create_session") and callable(self.runner.create_session):
            sig = inspect.signature(self.runner.create_session)
            kwargs: dict[str, Any] = {}
            if "profile_name" in sig.parameters:
                kwargs["profile_name"] = self.profile_name
            if "strategy" in sig.parameters:
                kwargs["strategy"] = self.strategy
            if "timeout_seconds" in sig.parameters:
                kwargs["timeout_seconds"] = self.timeout_seconds
            if "conversation_id" in sig.parameters:
                kwargs["conversation_id"] = str(cid) if cid else None
            return self.runner.create_session(**kwargs)
        elif self.runner is not None:
            return _RunnerSessionAdapter(
                runner=self.runner,
                run_id=self.run_id,
                profile_name=self.profile_name,
                strategy=self.strategy,
                timeout_seconds=self.timeout_seconds,
                conversation_id=cid,
            )
        raise CoordinatorError("Cannot create session: no ModelRunner available")

    # ------------------------------------------------------------------------
    # Start Coordinator
    # ------------------------------------------------------------------------

    def start(
        self,
        task: str,
        fleet_view: FleetView | None = None,
        run_mode: RunMode | str = RunMode.PLAN,
        budget: OrchestrationBudget | BudgetUsage | dict[str, Any] | None = None,
        repository_scope: str = "",
        workspace_info: str = "",
        run_id: RunId | str | None = None,
    ) -> CoordinatorStartResult:
        """Start the coordinator runtime session.

        Input:
            task: Task description
            fleet_view: FleetView aggregate capacity summary
            run_mode: RunMode (PLAN or IMPLEMENT)
            budget: OrchestrationBudget or budget summary
            repository_scope: Repository scope hints
            workspace_info: Workspace information
            run_id: Optional run ID override

        Returns:
            CoordinatorStartResult containing:
            - assessment: TaskAssessment
            - action: CoordinatorAction
            - conversation_id: ConversationId
        """
        if self._is_closed:
            raise CoordinatorClosedError("CoordinatorClient is closed")

        if run_id is not None:
            self.run_id = RunId(run_id)

        # Initialize session if not provided
        if self._session is None:
            self._session = self._create_session()

        effective_fleet = fleet_view or FleetView()

        # Build Round 0 prompt
        assessment_prompt = build_assessment_prompt(
            task=task,
            fleet_view=effective_fleet,
            repository_scope=repository_scope,
        )

        # Augment with run mode, budget, and workspace context if provided
        context_additions: list[str] = []
        if run_mode:
            mode_val = run_mode.value if hasattr(run_mode, "value") else str(run_mode)
            context_additions.append(f"### Run Mode\n{mode_val}\n")
        if budget is not None:
            b_dict = budget.to_dict() if hasattr(budget, "to_dict") else dict(budget)
            context_additions.append(f"### Budget Summary\n{json.dumps(b_dict, indent=2)}\n")
        if workspace_info.strip():
            context_additions.append(f"### Workspace Information\n{workspace_info.strip()}\n")

        if context_additions:
            # Insert additional context right above ### Current Fleet Capacity
            insertion_marker = "### Current Fleet Capacity"
            if insertion_marker in assessment_prompt:
                pre, post = assessment_prompt.split(insertion_marker, 1)
                assessment_prompt = f"{pre}{''.join(context_additions)}{insertion_marker}{post}"
            else:
                assessment_prompt = f"{assessment_prompt}\n\n{''.join(context_additions)}"

        full_prompt = f"{COORDINATOR_SYSTEM_PROMPT}\n\n{assessment_prompt}"

        # Send and parse initial response
        def _parse_init(raw: str | dict[str, Any]) -> InitialCoordinatorResponse:
            return parse_initial_response(raw)

        initial_response = self._send_and_parse(
            prompt=full_prompt,
            parser=_parse_init,
            schema_name="InitialCoordinatorResponse",
        )

        assessment = initial_response.assessment
        action = initial_response.action

        self._task_assessment = assessment
        self._initial_action = action
        self._round_number = 0

        # Record worker IDs from initial action
        for w in action.workers:
            self._known_worker_ids.add(w.worker_id)
        for a in action.auditors:
            self._known_worker_ids.add(a.worker_id)

        # Persist conversation ID and assessment as soon as known
        cid = self.conversation_id
        if cid is not None:
            self._persist_conversation_id()

        if self.run_store and self.run_id:
            if hasattr(self.run_store, "save_assessment"):
                try:
                    self.run_store.save_assessment(self.run_id, assessment)
                except Exception as exc:
                    logger.debug("Could not save assessment to run store: %s", exc)
            self._persist_coordinator_info(action=action)

        return CoordinatorStartResult(
            assessment=assessment,
            action=action,
            conversation_id=cid or ConversationId("unknown"),
        )

    def start_run(
        self,
        task: str,
        fleet_view: FleetView | None = None,
        run_id: RunId | str | None = None,
        run_mode: RunMode | str | None = None,
        budget: OrchestrationBudget | dict[str, Any] | None = None,
        workspace_info: str = "",
        repository_scope: str = "",
    ) -> CoordinatorStartResult:
        """Start coordinator run, returning assessment and initial action."""
        return self.start(
            task=task,
            fleet_view=fleet_view,
            run_id=run_id,
            run_mode=run_mode,
            budget=budget,
            workspace_info=workspace_info,
            repository_scope=repository_scope,
        )

    # ------------------------------------------------------------------------
    # Protocol Interface: assess_task
    # ------------------------------------------------------------------------

    def assess_task(self, task: str, fleet_view: FleetView) -> TaskAssessment:
        """Assess the nature, scope, and strategy for a new task.

        Part of CoordinatorClient protocol.
        """
        if self._task_assessment is not None:
            return self._task_assessment
        result = self.start(task=task, fleet_view=fleet_view)
        return result.assessment

    # ------------------------------------------------------------------------
    # Protocol Interface: decide_action
    # ------------------------------------------------------------------------

    def decide_action(
        self,
        observation: CoordinatorObservation,
        conversation_id: ConversationId | None = None,
    ) -> CoordinatorAction:
        """Propose the next orchestration action based on the observation.

        Part of CoordinatorClient protocol.
        Maintains the existing persistent session across rounds.
        """
        if self._is_closed:
            raise CoordinatorClosedError("CoordinatorClient is closed")

        # If Round 0 initial action was already produced by start()/assess_task()
        # and observation is the clean initial Round 0 observation, return it without a redundant model turn
        if (
            self._initial_action is not None
            and observation.round_number == 0
            and not observation.completed_results
            and not observation.failed_results
            and not observation.rejected_requests
        ):
            act = self._initial_action
            self._initial_action = None
            return act

        # Resume if session missing, or if explicit conversation_id is passed and differs
        if self._session is None:
            self.resume(conversation_id=conversation_id)
        elif conversation_id is not None and (self._conversation_id is None or self._conversation_id != conversation_id):
            self.resume(conversation_id=conversation_id)

        self._round_number = observation.round_number

        # Update known worker IDs with results from observation
        for res in observation.completed_results:
            self._known_worker_ids.add(res.worker_id)
        for res in observation.failed_results:
            self._known_worker_ids.add(res.worker_id)
        for req in observation.rejected_requests:
            self._known_worker_ids.add(req.worker_id)

        # Build observation prompt
        obs_prompt = format_coordinator_observation(observation)

        # If resumed, prepend AGYM-authoritative latest state
        is_first_turn_of_resume = self._is_resumed
        if is_first_turn_of_resume:
            auth_header = self._format_authoritative_state_prompt(observation)
            full_prompt = f"{auth_header}\n\n{obs_prompt}"
            self._is_resumed = False
        else:
            full_prompt = obs_prompt

        def _parse_action(raw: str | dict[str, Any]) -> CoordinatorAction:
            return parse_coordinator_action(raw, known_worker_ids=self._known_worker_ids)

        try:
            action = self._send_and_parse(
                prompt=full_prompt,
                parser=_parse_action,
                schema_name="CoordinatorAction",
            )
        except CoordinatorCrashError as exc:
            if is_first_turn_of_resume and self._conversation_id is not None:
                logger.warning(
                    "Resumed coordinator session with conversation ID %s failed (%s); "
                    "recovering with a fresh coordinator session seeded from authoritative state.",
                    self._conversation_id,
                    exc,
                )
                self.resume(conversation_id=None)
                auth_header = self._format_authoritative_state_prompt(observation)
                fresh_full_prompt = f"{auth_header}\n\n{obs_prompt}"
                action = self._send_and_parse(
                    prompt=fresh_full_prompt,
                    parser=_parse_action,
                    schema_name="CoordinatorAction",
                )
            else:
                raise

        # Record new worker IDs
        for w in action.workers:
            self._known_worker_ids.add(w.worker_id)
        for a in action.auditors:
            self._known_worker_ids.add(a.worker_id)

        # Persist coordinator info
        self._persist_coordinator_info(action=action, observation=observation)

        return action

    # ------------------------------------------------------------------------
    # Resume Support
    # ------------------------------------------------------------------------

    def resume(
        self,
        run_id: RunId | str | None = None,
        conversation_id: ConversationId | str | None = None,
        run_state: RunState | None = None,
    ) -> None:
        """Resume a coordinator session using AGYM-authoritative latest state.

        If a persistent coordinator process died but conversation ID exists:
        - creates replacement coordinator session
        - resumes known conversation
        - prepares AGYM-authoritative state injection
        """
        if self._is_closed:
            raise CoordinatorClosedError("Cannot resume a closed CoordinatorClient")

        if run_id is not None:
            self.run_id = RunId(run_id)

        cid = conversation_id
        if cid is None and run_state is not None:
            cid = run_state.coordinator_conversation_id

        if cid is None and self.run_store and self.run_id:
            if hasattr(self.run_store, "get_coordinator_info"):
                try:
                    info = self.run_store.get_coordinator_info(self.run_id)
                    if info and info.conversation_id:
                        cid = info.conversation_id
                except Exception:
                    pass
            if cid is None and hasattr(self.run_store, "get_run"):
                try:
                    st = self.run_store.get_run(self.run_id)
                    if st and st.coordinator_conversation_id:
                        cid = st.coordinator_conversation_id
                except Exception:
                    pass

        if cid is None:
            cid = self._conversation_id

        # Terminate any dead or existing session
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

        # Create replacement coordinator session (fresh session if cid is None)
        self._session = self._create_session(conversation_id=cid)
        if cid is not None:
            self._conversation_id = ConversationId(cid)
        else:
            self._conversation_id = None
        self._is_resumed = True

        # Sync round number and assessment from authoritative store if available
        if run_state is not None:
            self._round_number = run_state.round_number
            if run_state.assessment:
                self._task_assessment = run_state.assessment
        elif self.run_store and self.run_id and hasattr(self.run_store, "get_run"):
            try:
                st = self.run_store.get_run(self.run_id)
                if st is not None:
                    self._round_number = st.round_number
                    if st.assessment:
                        self._task_assessment = st.assessment
            except Exception:
                pass

        self._persist_conversation_id()

    def _format_authoritative_state_prompt(self, observation: CoordinatorObservation) -> str:
        """Format the AGYM-authoritative state prefix injected on resume."""
        lines = [
            "## RESUMED COORDINATOR SESSION - AGYM AUTHORITATIVE STATE",
            "This coordinator conversation was resumed following a restart.",
            "The following state provided by AGYM RunState is authoritative.",
            "Never assume model conversation memory is more authoritative than RunState.",
            f"- Run ID: {self.run_id}",
            f"- Round Number: {observation.round_number}",
        ]
        if self._task_assessment:
            lines.append(f"- Task Assessment Type: {self._task_assessment.task_type.value}")
            lines.append(f"- Task Complexity: {self._task_assessment.complexity.value}")
            lines.append(f"- Task Assessment Summary: {self._task_assessment.summary}")
        lines.append(f"- Completed Results Recorded: {len(observation.completed_results)}")
        lines.append(f"- Failed Results Recorded: {len(observation.failed_results)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------------
    # Persistence Helpers
    # ------------------------------------------------------------------------

    def _persist_conversation_id(self) -> None:
        """Store conversation ID as soon as known."""
        cid = self.conversation_id
        if cid is None or not self.run_store or not self.run_id:
            return

        if hasattr(self.run_store, "save_coordinator_info"):
            try:
                self.run_store.save_coordinator_info(
                    run_id=self.run_id,
                    conversation_id=cid,
                    round_number=self._round_number,
                    last_accepted_action=self._initial_action,
                )
                return
            except Exception as exc:
                logger.debug("Failed to save coordinator info: %s", exc)

        if hasattr(self.run_store, "get_run") and hasattr(self.run_store, "save_run"):
            try:
                state = self.run_store.get_run(self.run_id)
                if state is not None:
                    state.coordinator_conversation_id = cid
                    state.round_number = self._round_number
                    self.run_store.save_run(state)
            except Exception as exc:
                logger.debug("Failed to update run state: %s", exc)

    def _persist_coordinator_info(
        self,
        action: CoordinatorAction | None = None,
        observation: CoordinatorObservation | None = None,
    ) -> None:
        """Persist coordinator resume information."""
        cid = self.conversation_id
        if not self.run_store or not self.run_id:
            return

        if hasattr(self.run_store, "save_coordinator_info"):
            try:
                self.run_store.save_coordinator_info(
                    run_id=self.run_id,
                    conversation_id=cid,
                    round_number=self._round_number,
                    last_accepted_action=action,
                    latest_observation=observation,
                )
                return
            except Exception as exc:
                logger.debug("Failed to persist coordinator info: %s", exc)

        if hasattr(self.run_store, "get_run") and hasattr(self.run_store, "save_run"):
            try:
                state = self.run_store.get_run(self.run_id)
                if state is not None:
                    state.coordinator_conversation_id = cid
                    state.round_number = self._round_number
                    self.run_store.save_run(state)
            except Exception as exc:
                logger.debug("Failed to update run state: %s", exc)

    # ------------------------------------------------------------------------
    # Execution & Bounded Invalid Output Recovery
    # ------------------------------------------------------------------------

    def _send_and_parse(
        self,
        prompt: str,
        parser: Callable[[str | dict[str, Any]], Any],
        schema_name: str,
    ) -> Any:
        """Send prompt to the session and parse result with bounded correction attempts.

        Handles:
        - Process crashes -> Typed failure CoordinatorCrashError
        - Malformed output -> Bounded correction attempts up to max_correction_attempts
        - Conversation ID capture
        """
        session = self._session
        if session is None or self._is_closed:
            raise CoordinatorClosedError("Coordinator session is closed or not initialized")

        current_prompt = prompt
        last_protocol_error: Exception | None = None

        for attempt in range(self.max_correction_attempts + 1):
            try:
                res = session.send(current_prompt, timeout_seconds=self.timeout_seconds)
            except Exception as exc:
                raise CoordinatorCrashError(
                    f"Coordinator model process failed unexpectedly: {exc}",
                    run_id=self.run_id,
                    conversation_id=self.conversation_id,
                    round_number=self._round_number,
                    cause=exc,
                ) from exc

            # Capture conversation ID as soon as observed
            cid = getattr(session, "conversation_id", None) or res.conversation_id
            if cid and cid != self._conversation_id:
                self._conversation_id = ConversationId(cid)
                self._persist_conversation_id()

            # Classify mechanical execution failures
            if res.status != InvocationStatus.SUCCEEDED:
                err_text = res.error or "Model invocation failed"
                fail_class = classify_failure(res)

                # Check if it is a recoverable malformed payload
                if fail_class != FailureClass.RECOVERABLE:
                    if "timed out" in err_text.lower() or "timeout" in err_text.lower():
                        raise CoordinatorTimeoutError(
                            f"Coordinator model turn timed out: {err_text}",
                            run_id=self.run_id,
                            conversation_id=self.conversation_id,
                            round_number=self._round_number,
                            exit_code=res.exit_code,
                        )
                    raise CoordinatorCrashError(
                        f"Coordinator model process crashed: {err_text}",
                        run_id=self.run_id,
                        conversation_id=self.conversation_id,
                        round_number=self._round_number,
                        exit_code=res.exit_code,
                    )

            # Extract raw response
            raw_payload = res.structured_data if res.structured_data is not None else (res.response or "")

            try:
                parsed = parser(raw_payload)
                return parsed
            except (ProtocolError, ValueError, json.JSONDecodeError) as exc:
                last_protocol_error = exc
                if attempt < self.max_correction_attempts:
                    logger.warning(
                        "Coordinator output violated %s schema (attempt %d/%d): %s",
                        schema_name,
                        attempt + 1,
                        self.max_correction_attempts,
                        exc,
                    )
                    current_prompt = (
                        f"Your previous response violated the {schema_name} schema:\n"
                        f"{exc}\n\n"
                        f"Return only a corrected response adhering strictly to the JSON schema."
                    )
                else:
                    logger.error(
                        "Coordinator response repeatedly violated %s schema after %d attempts: %s",
                        schema_name,
                        self.max_correction_attempts + 1,
                        exc,
                    )
                    raise last_protocol_error

        if last_protocol_error is not None:
            raise last_protocol_error
        raise MalformedResponseError("Received empty or unparseable response from coordinator")

    # ------------------------------------------------------------------------
    # Close & Resource Cleanup
    # ------------------------------------------------------------------------

    def close(self) -> None:
        """Deterministically terminate the coordinator session and release resources."""
        if self._is_closed:
            return
        self._is_closed = True
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                logger.debug("Error while closing coordinator session: %s", exc)
            finally:
                self._session = None

    def __enter__(self) -> CoordinatorClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# Type alias for coordinator runtime
CoordinatorRuntime = CoordinatorClient
