"""Tests for orchestration protocol, schemas, parsers, and prompt builders.

Covers:
- Coordinator system prompt (WHAT vs HOW boundary, permitted requests, forbidden actions)
- Assessment prompt (Round 0 TaskAssessment + CoordinatorAction requirement, fleet capacity)
- Observation prompt and serialization (results, failures, rejections, budget, fleet view)
- Privacy invariant: Strictly no internal profile names exposed to coordinator
- Standardized infrastructure failure representations and formatting
- Synthesis prompt formatting without internal orchestration metadata
- Valid first response parsing (pure JSON, markdown code fence, leading/trailing prose)
- Valid worker, audit, executor, and finalize actions
- Invalid JSON handling (prose only, truncated syntax, wrong root types)
- Unknown action kinds and schema violations
- Profile name injection rejection (root, worker, auditor, assessment, case variations)
- Command injection field rejection (command, argv, shell, environment, env)
- Mutating constraints (mutating only for EXECUTOR, multiple mutating workers rejected)
- Duplicate WorkerId detection and rejection
- Empty worker/auditor wave rejection
- Unknown referenced worker rejection when prior context is supplied
"""

from __future__ import annotations

import json
import unittest

from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    AuditRequest,
    AuditResult,
    BudgetUsage,
    ComplexityLevel,
    CoordinatorAction,
    CoordinatorObservation,
    ExecutionStrategy,
    FailureClass,
    FleetView,
    InvocationId,
    InvocationStatus,
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
    build_synthesis_prompt,
    format_coordinator_observation,
    format_infrastructure_failure,
    get_coordinator_system_prompt,
)
from agym.orchestration.protocol import (
    AUDIT_REQUEST_SCHEMA,
    COORDINATOR_ACTION_SCHEMA,
    INITIAL_RESPONSE_SCHEMA,
    TASK_ASSESSMENT_SCHEMA,
    WORKER_REQUEST_SCHEMA,
    ActionValidationError,
    ForbiddenFieldError,
    InfrastructureFailure,
    InfrastructureFailureReason,
    InitialCoordinatorResponse,
    MalformedResponseError,
    ProtocolError,
    ProtocolSchemaError,
    check_forbidden_fields,
    create_action_rejected_failure,
    create_budget_rejected_failure,
    create_profile_unavailable_failure,
    create_quota_exhausted_failure,
    create_timeout_failure,
    create_worker_failed_failure,
    extract_json_payload,
    failure_to_worker_result,
    get_coordinator_action_schema,
    get_initial_response_schema,
    get_task_assessment_schema,
    parse_coordinator_action,
    parse_initial_response,
    validate_coordinator_action,
)


class TestCoordinatorSystemPrompt(unittest.TestCase):
    """Tests for the coordinator system prompt."""

    def test_system_prompt_defines_what_vs_how_boundary(self) -> None:
        prompt = get_coordinator_system_prompt()
        self.assertIn("WHAT", prompt)
        self.assertIn("HOW", prompt)
        self.assertIn("You decide WHAT work is useful", prompt)
        self.assertIn("AGYM decides HOW it executes", prompt)

    def test_system_prompt_enumerates_allowed_requests(self) -> None:
        prompt = COORDINATOR_SYSTEM_PROMPT
        self.assertIn("workers", prompt)
        self.assertIn("specialists", prompt)
        self.assertIn("audits", prompt)
        self.assertIn("synthesis", prompt)
        self.assertIn("another round", prompt)
        self.assertIn("executor", prompt)
        self.assertIn("finalize", prompt)

    def test_system_prompt_enumerates_forbidden_actions(self) -> None:
        prompt = COORDINATOR_SYSTEM_PROMPT
        self.assertIn("Name AGYM profiles", prompt)
        self.assertIn("Run shell commands", prompt)
        self.assertIn("Spawn processes", prompt)
        self.assertIn("Override budgets", prompt)
        self.assertIn("Override leases", prompt)
        self.assertIn("Override workspace restrictions", prompt)
        self.assertIn("Recursively create agents itself", prompt)

    def test_system_prompt_mentions_forbidden_field_rejection(self) -> None:
        prompt = COORDINATOR_SYSTEM_PROMPT
        for field_name in ("profile", "command", "argv", "shell", "environment"):
            self.assertIn(field_name, prompt)


class TestAssessmentPrompt(unittest.TestCase):
    """Tests for the Round 0 assessment prompt builder."""

    def setUp(self) -> None:
        self.fleet = FleetView(
            available_profiles=3,
            max_parallel=3,
            standard_capacity=3,
            high_effort_capacity=2,
            boost_capacity=1,
            quota_band_counts={"HIGH": 2, "MEDIUM": 1},
        )

    def test_build_assessment_prompt_includes_task_and_fleet(self) -> None:
        prompt = build_assessment_prompt("Refactor authentication module", self.fleet, "agym/auth")
        self.assertIn("Refactor authentication module", prompt)
        self.assertIn("agym/auth", prompt)
        self.assertIn("Available Profiles: 3", prompt)
        self.assertIn("Max Parallel Concurrency: 3", prompt)
        self.assertIn("High Effort Tier Capacity: 2", prompt)
        self.assertIn("Boost Tier Capacity: 1", prompt)

    def test_build_assessment_prompt_requires_task_assessment_and_action(self) -> None:
        prompt = build_assessment_prompt("Add feature X", self.fleet)
        self.assertIn("TaskAssessment", prompt)
        self.assertIn("CoordinatorAction", prompt)
        self.assertIn("assessment", prompt)
        self.assertIn("action", prompt)
        self.assertIn("Do NOT provide an arbitrary prose-only answer", prompt)

    def test_build_assessment_prompt_does_not_contain_profile_identities(self) -> None:
        prompt = build_assessment_prompt("Task", self.fleet)
        self.assertNotIn("profile_name", prompt)
        self.assertNotIn("profile-1", prompt)
        self.assertNotIn("account-1", prompt)
        self.assertNotIn("lease_id", prompt)


class TestObservationPrompt(unittest.TestCase):
    """Tests for observation prompt formatting and privacy preservation."""

    def test_format_coordinator_observation_full(self) -> None:
        obs = CoordinatorObservation(
            round_number=2,
            budget_usage=BudgetUsage(
                invocations=5,
                rounds=2,
                boost_invocations=1,
                retries=0,
                runtime_seconds=124.5,
            ),
            fleet_view=FleetView(
                available_profiles=4,
                max_parallel=4,
                standard_capacity=4,
                high_effort_capacity=2,
                boost_capacity=1,
                quota_band_counts={"HIGH": 4},
            ),
            completed_results=[
                WorkerResult(
                    worker_id=WorkerId("w-arch"),
                    role=WorkerRole.ARCHITECTURE,
                    status=InvocationStatus.SUCCEEDED,
                    response="Architecture proposal complete.",
                    structured_data={"suggested_modules": ["auth", "session"]},
                ),
                AuditResult(
                    worker_id=WorkerId("a-sec"),
                    findings=["Missing csrf check in session handler", "Weak token entropy"],
                    response="Security review completed with 2 findings.",
                ),
            ],
            failed_results=[
                WorkerResult(
                    worker_id=WorkerId("w-perf"),
                    role=WorkerRole.PERFORMANCE,
                    status=InvocationStatus.FAILED,
                    failure=FailureClass.RETRYABLE,
                    response="Model connection reset",
                )
            ],
            rejected_requests=[
                WorkerRequest(
                    worker_id=WorkerId("w-extra"),
                    role=WorkerRole.DEBUGGING,
                    objective="Inspect logs",
                )
            ],
        )

        formatted = format_coordinator_observation(obs)

        # Basic sections
        self.assertIn("## Round 2 Observation", formatted)
        self.assertIn("Invocations Used: 5", formatted)
        self.assertIn("Rounds Completed: 2", formatted)
        self.assertIn("Boost Invocations Used: 1", formatted)
        self.assertIn("Runtime Elapsed: 124.5s", formatted)

        # Fleet view
        self.assertIn("Available Profiles: 4", formatted)
        self.assertIn("Max Parallel Concurrency: 4", formatted)

        # Completed results
        self.assertIn("w-arch", formatted)
        self.assertIn("ARCHITECTURE", formatted)
        self.assertIn("Architecture proposal complete.", formatted)
        self.assertIn("suggested_modules", formatted)
        self.assertIn("a-sec", formatted)
        self.assertIn("Missing csrf check in session handler", formatted)

        # Failed results
        self.assertIn("w-perf", formatted)
        self.assertIn("RETRYABLE", formatted)
        self.assertIn("Model connection reset", formatted)

        # Rejected requests
        self.assertIn("w-extra", formatted)
        self.assertIn("Inspect logs", formatted)

        # Privacy invariant: strictly no profile names
        self.assertNotIn("profile_name", formatted)
        self.assertNotIn("lease_id", formatted)
        self.assertNotIn("pid", formatted)

    def test_format_coordinator_observation_empty(self) -> None:
        obs = CoordinatorObservation(round_number=1)
        formatted = build_observation_prompt(obs)
        self.assertIn("## Round 1 Observation", formatted)
        self.assertIn("(None)", formatted)

    def test_coordinator_observation_redacts_profile_identity_from_errors(self) -> None:
        """Regression test for W4-03: coordinator observations redact profile paths and identities from errors."""
        obs = CoordinatorObservation(
            round_number=1,
            failed_results=[
                WorkerResult(
                    worker_id=WorkerId("w-leak"),
                    role=WorkerRole.GENERAL,
                    status=InvocationStatus.FAILED,
                    failure=FailureClass.RETRYABLE,
                    response=(
                        "Process failed in /home/user/.local/share/agym/profiles/account-secret/settings.json: "
                        "profile 'account-secret' could not obtain token from profiles/account-secret/token.json"
                    ),
                )
            ],
        )
        formatted = format_coordinator_observation(obs)
        # Physical profile identity must never appear
        self.assertNotIn("account-secret", formatted)
        self.assertIn("[REDACTED]", formatted)


class TestSynthesisPrompt(unittest.TestCase):
    """Tests for synthesis worker prompt generation."""

    def test_build_synthesis_prompt_includes_expected_sections(self) -> None:
        assessment = TaskAssessment(
            task_type=TaskType.ARCHITECTURE,
            complexity=ComplexityLevel.MEDIUM,
            confidence=0.85,
            mutation_required=False,
            repository_scope="agym/orchestration",
            summary="Refactor orchestration contracts and protocols",
        )
        workers = [
            WorkerResult(
                worker_id=WorkerId("w-1"),
                role=WorkerRole.ARCHITECTURE,
                response="Propose modular protocol parser.",
            ),
            WorkerResult(
                worker_id=WorkerId("w-2"),
                role=WorkerRole.MINIMAL_CHANGE,
                response="Keep changes strictly within protocol.py and prompts.py.",
            ),
        ]
        audits = [
            AuditResult(
                worker_id=WorkerId("a-1"),
                findings=["Ensure forbidden fields check is recursive."],
                response="Audit report on safety.",
            )
        ]

        prompt = build_synthesis_prompt(
            task="Design protocol subsystem",
            assessment=assessment,
            worker_results=workers,
            audit_results=audits,
            repository_facts="Repo uses Python 3.10+ standard library dataclasses.",
            objective="Deliver finalized protocol design.",
        )

        self.assertIn("Design protocol subsystem", prompt)
        self.assertIn("ARCHITECTURE", prompt)
        self.assertIn("Refactor orchestration contracts and protocols", prompt)
        self.assertIn("Repo uses Python 3.10+ standard library dataclasses.", prompt)
        self.assertIn("Deliver finalized protocol design.", prompt)
        self.assertIn("Propose modular protocol parser.", prompt)
        self.assertIn("Keep changes strictly within protocol.py and prompts.py.", prompt)
        self.assertIn("Ensure forbidden fields check is recursive.", prompt)

        # Must NOT expose orchestration internals
        self.assertNotIn("lease_id", prompt)
        self.assertNotIn("invocation_id", prompt)
        self.assertNotIn("profile_name", prompt)
        self.assertNotIn("pid", prompt)


class TestInfrastructureFailures(unittest.TestCase):
    """Tests for standardized infrastructure failure objects and formatting."""

    def test_create_worker_failed_failure(self) -> None:
        fail = create_worker_failed_failure("w-1", "Out of memory", retryable=False)
        self.assertEqual(fail.reason, InfrastructureFailureReason.WORKER_FAILED)
        self.assertEqual(fail.failure_class, FailureClass.UNRECOVERABLE)
        self.assertEqual(fail.suggested_action, "replace_worker")
        formatted = format_infrastructure_failure(fail)
        self.assertIn("WORKER_FAILED", formatted)
        self.assertIn("Out of memory", formatted)
        self.assertIn("REPLACE_WORKER", formatted)

    def test_create_profile_unavailable_failure(self) -> None:
        fail = create_profile_unavailable_failure("w-2", role=WorkerRole.EXECUTOR)
        self.assertEqual(fail.reason, InfrastructureFailureReason.PROFILE_UNAVAILABLE)
        self.assertEqual(fail.suggested_action, "reduce_scope")
        result = failure_to_worker_result(fail, role=WorkerRole.EXECUTOR)
        self.assertEqual(result.worker_id, "w-2")
        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.failure, FailureClass.RECOVERABLE)

    def test_create_quota_exhausted_failure(self) -> None:
        fail = create_quota_exhausted_failure("w-3", strategy=ExecutionStrategy.BOOST)
        self.assertEqual(fail.reason, InfrastructureFailureReason.QUOTA_EXHAUSTED)
        self.assertEqual(fail.details["strategy"], "BOOST")

    def test_create_action_rejected_failure(self) -> None:
        fail = create_action_rejected_failure("act-1", "Mutating executor not permitted in current mode")
        self.assertEqual(fail.reason, InfrastructureFailureReason.ACTION_REJECTED)
        self.assertEqual(fail.action_id, "act-1")

    def test_create_budget_rejected_failure(self) -> None:
        fail = create_budget_rejected_failure("Max round limit 10 reached")
        self.assertEqual(fail.reason, InfrastructureFailureReason.BUDGET_REJECTED)
        self.assertEqual(fail.suggested_action, "finalize")

    def test_create_timeout_failure_compatibility_maps_to_stall(self) -> None:
        fail = create_timeout_failure("w-timeout", 180.0)
        self.assertEqual(fail.reason, InfrastructureFailureReason.STALL)
        self.assertEqual(fail.failure_class, FailureClass.RETRYABLE)
        self.assertEqual(fail.suggested_action, "retry")


class TestPayloadExtraction(unittest.TestCase):
    """Tests for extracting JSON from raw model outputs."""

    def test_extract_pure_json(self) -> None:
        raw = '{"action_id": "act-1", "kind": "FINALIZE"}'
        self.assertEqual(extract_json_payload(raw), raw)

    def test_extract_markdown_json_block(self) -> None:
        raw = """\
Here is my decision:
```json
{
  "action_id": "act-1",
  "kind": "FINALIZE"
}
```
Done!"""
        extracted = extract_json_payload(raw)
        data = json.loads(extracted)
        self.assertEqual(data["action_id"], "act-1")
        self.assertEqual(data["kind"], "FINALIZE")

    def test_extract_unlabelled_code_block(self) -> None:
        raw = """\
```
{"action_id": "act-2", "kind": "FINALIZE"}
```"""
        extracted = extract_json_payload(raw)
        self.assertEqual(json.loads(extracted)["action_id"], "act-2")

    def test_extract_embedded_braces(self) -> None:
        raw = 'My plan is {"action_id": "act-3", "kind": "FINALIZE"} thanks!'
        extracted = extract_json_payload(raw)
        self.assertEqual(json.loads(extracted)["action_id"], "act-3")

    def test_extract_empty_or_prose_only_raises(self) -> None:
        with self.assertRaises(MalformedResponseError):
            extract_json_payload("")
        with self.assertRaises(MalformedResponseError):
            extract_json_payload("   \n\t  ")
        with self.assertRaises(MalformedResponseError):
            extract_json_payload("I recommend that we run three workers to review the code.")


class TestValidFirstResponse(unittest.TestCase):
    """Tests parsing the coordinator's initial Round 0 response."""

    def test_valid_first_response_pure_json(self) -> None:
        payload = {
            "assessment": {
                "task_type": "IMPLEMENTATION",
                "complexity": "MEDIUM",
                "confidence": 0.9,
                "mutation_required": True,
                "repository_scope": "agym/orchestration",
                "value_of_parallel_reasoning": 0.5,
                "value_of_auditing": 0.3,
                "summary": "Implement protocol subsystem",
                "proposed_initial_work": ["draft protocol", "add tests"],
            },
            "run_plan": {
                "goal": "Implement the protocol subsystem",
                "phases": ["Investigate", "Reconcile", "Synthesize", "Final Review"],
                "current_phase": "Investigate",
                "completion_criteria": ["Protocol is implemented and verified"],
            },
            "action": {
                "action_id": "act-0",
                "kind": "RUN_WORKERS",
                "workers": [
                    {
                        "worker_id": "w-impl-1",
                        "role": "GENERAL",
                        "strategy": "STANDARD",
                        "workspace_mode": "READ_ONLY",
                        "objective": "Survey code",
                    }
                ],
                "reason_summary": "Begin initial research wave",
            },
        }

        resp = parse_initial_response(json.dumps(payload))
        self.assertIsInstance(resp, InitialCoordinatorResponse)
        self.assertEqual(resp.assessment.task_type, TaskType.IMPLEMENTATION)
        self.assertEqual(resp.assessment.complexity, ComplexityLevel.MEDIUM)
        self.assertTrue(resp.assessment.mutation_required)
        self.assertEqual(resp.action.action_id, "act-0")
        self.assertEqual(resp.action.kind, ActionKind.RUN_WORKERS)
        self.assertEqual(len(resp.action.workers), 1)

        # Unpack as tuple
        assessment, action = resp
        self.assertEqual(assessment.task_type, TaskType.IMPLEMENTATION)
        self.assertEqual(action.action_id, "act-0")

    def test_valid_first_response_fenced_markdown(self) -> None:
        raw = """\
Plan assessment:
```json
{
  "assessment": {
    "task_type": "DEBUGGING",
    "complexity": "SMALL",
    "confidence": 0.95,
    "mutation_required": false,
    "repository_scope": "agym/cli.py"
  },
  "run_plan": {
    "goal": "Diagnose the bug",
    "phases": ["Investigate", "Synthesize", "Final Review"],
    "current_phase": "Investigate",
    "completion_criteria": ["The diagnosis is sufficient"]
  },
  "action": {
    "action_id": "act-init",
    "kind": "FINALIZE",
    "final_response": "Bug was trivial to diagnose."
  }
}
```
"""
        resp = parse_initial_response(raw)
        self.assertEqual(resp.assessment.task_type, TaskType.DEBUGGING)
        self.assertEqual(resp.action.kind, ActionKind.FINALIZE)
        self.assertEqual(resp.action.final_response, "Bug was trivial to diagnose.")

    def test_first_response_missing_assessment_raises(self) -> None:
        payload = {
            "action": {
                "action_id": "act-0",
                "kind": "FINALIZE",
                "final_response": "done",
            }
        }
        with self.assertRaises(ProtocolSchemaError) as cm:
            parse_initial_response(json.dumps(payload))
        self.assertIn("assessment", str(cm.exception))

    def test_first_response_missing_run_plan_raises(self) -> None:
        payload = {
            "assessment": {
                "task_type": "GENERAL",
                "complexity": "SMALL",
                "confidence": 1.0,
                "mutation_required": False,
                "repository_scope": "",
            },
            "action": {
                "action_id": "act-0",
                "kind": "FINALIZE",
                "final_response": "done",
            },
        }
        with self.assertRaises(ProtocolSchemaError) as cm:
            parse_initial_response(json.dumps(payload))
        self.assertIn("run_plan", str(cm.exception))

    def test_first_response_missing_action_raises(self) -> None:
        payload = {
            "assessment": {
                "task_type": "GENERAL",
                "complexity": "SMALL",
                "confidence": 1.0,
                "mutation_required": False,
                "repository_scope": "",
            },
            "run_plan": {
                "goal": "Answer the task",
                "phases": ["Investigate", "Final Review"],
                "current_phase": "Investigate",
                "completion_criteria": ["A usable result exists"],
            },
        }
        with self.assertRaises(ProtocolSchemaError) as cm:
            parse_initial_response(json.dumps(payload))
        self.assertIn("action", str(cm.exception))


class TestValidActions(unittest.TestCase):
    """Tests parsing valid coordinator actions."""

    def test_valid_worker_action(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_WORKERS",
            "workers": [
                {
                    "worker_id": "w-1",
                    "role": "ARCHITECTURE",
                    "strategy": "HIGH_EFFORT",
                    "workspace_mode": "READ_ONLY",
                    "objective": "Evaluate architecture",
                },
                {
                    "worker_id": "w-2",
                    "role": "SECURITY",
                    "strategy": "STANDARD",
                    "workspace_mode": "READ_ONLY",
                    "objective": "Perform security review",
                },
            ],
            "reason_summary": "Parallel analysis wave",
        }
        action = parse_coordinator_action(payload)
        self.assertEqual(action.action_id, "act-1")
        self.assertEqual(action.kind, ActionKind.RUN_WORKERS)
        self.assertEqual(len(action.workers), 2)
        self.assertEqual(action.workers[0].role, WorkerRole.ARCHITECTURE)
        self.assertEqual(action.workers[1].role, WorkerRole.SECURITY)

    def test_valid_audit_action(self) -> None:
        payload = {
            "action_id": "act-2",
            "kind": "RUN_AUDITORS",
            "auditors": [
                {
                    "worker_id": "auditor-1",
                    "target_worker_ids": ["w-1", "w-2"],
                    "focus": "Correctness and safety",
                    "strategy": "STANDARD",
                    "stall_timeout_seconds": 240.0,
                }
            ],
            "reason_summary": "Audit wave",
        }
        action = parse_coordinator_action(payload, known_worker_ids={"w-1", "w-2"})
        self.assertEqual(action.kind, ActionKind.RUN_AUDITORS)
        self.assertEqual(len(action.auditors), 1)
        self.assertEqual(action.auditors[0].target_worker_ids, ["w-1", "w-2"])

    def test_valid_synthesis_action(self) -> None:
        payload = {
            "action_id": "act-syn",
            "kind": "RUN_SYNTHESIS",
            "workers": [
                {
                    "worker_id": "w-syn",
                    "role": "SYNTHESIZER",
                    "objective": "Consolidate findings",
                }
            ],
        }
        action = parse_coordinator_action(payload)
        self.assertEqual(action.kind, ActionKind.RUN_SYNTHESIS)
        self.assertEqual(action.workers[0].role, WorkerRole.SYNTHESIZER)

    def test_valid_executor_action(self) -> None:
        payload = {
            "action_id": "act-3",
            "kind": "RUN_EXECUTOR",
            "workers": [
                {
                    "worker_id": "w-exec",
                    "role": "EXECUTOR",
                    "workspace_mode": "MUTATING",
                    "objective": "Apply filesystem modifications",
                }
            ],
            "reason_summary": "Mutating execution step",
        }
        action = parse_coordinator_action(payload)
        self.assertEqual(action.kind, ActionKind.RUN_EXECUTOR)
        self.assertEqual(len(action.workers), 1)
        self.assertEqual(action.workers[0].role, WorkerRole.EXECUTOR)
        self.assertEqual(action.workers[0].workspace_mode, WorkspaceMode.MUTATING)

    def test_valid_finalize_action(self) -> None:
        payload = {
            "action_id": "act-4",
            "kind": "FINALIZE",
            "final_response": "The task has been successfully solved.",
            "reason_summary": "All deliverables satisfied",
        }
        action = parse_coordinator_action(payload)
        self.assertEqual(action.kind, ActionKind.FINALIZE)
        self.assertEqual(len(action.workers), 0)
        self.assertEqual(len(action.auditors), 0)
        self.assertEqual(action.final_response, "The task has been successfully solved.")


class TestRejectionAndValidation(unittest.TestCase):
    """Tests for protocol rejection, schema errors, and validation errors."""

    def test_invalid_json_raises_malformed_response_error(self) -> None:
        with self.assertRaises(MalformedResponseError):
            parse_coordinator_action("I think we should finish now.")

        with self.assertRaises(MalformedResponseError):
            parse_coordinator_action('{"action_id": "act-1", ')

        with self.assertRaises(MalformedResponseError):
            parse_coordinator_action("[1, 2, 3]")

    def test_unknown_action_kind_raises(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "SPAWN_AUTONOMOUS_FLEET",
            "workers": [],
        }
        with self.assertRaises(ProtocolSchemaError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("SPAWN_AUTONOMOUS_FLEET", str(cm.exception))

    def test_unknown_field_in_action_raises(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "FINALIZE",
            "unexpected_custom_field": "val",
        }
        with self.assertRaises(ProtocolSchemaError):
            parse_coordinator_action(payload)

    def test_profile_name_injection_at_root_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_WORKERS",
            "profile": "targ",
            "workers": [{"worker_id": "w1", "role": "GENERAL"}],
        }
        with self.assertRaises(ForbiddenFieldError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("profile", str(cm.exception).lower())

    def test_profile_name_injection_inside_worker_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_WORKERS",
            "workers": [
                {
                    "worker_id": "w1",
                    "role": "GENERAL",
                    "profile_name": "default",
                }
            ],
        }
        with self.assertRaises(ForbiddenFieldError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("profile_name", str(cm.exception).lower())

    def test_profile_name_injection_case_insensitive(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_WORKERS",
            "workers": [
                {
                    "worker_id": "w1",
                    "role": "GENERAL",
                    "PROFILE": "account-1",
                }
            ],
        }
        with self.assertRaises(ForbiddenFieldError):
            parse_coordinator_action(payload)

    def test_command_injection_fields_rejected(self) -> None:
        dangerous_fields = ["command", "argv", "shell", "environment", "env", "cmd", "process"]
        for dangerous in dangerous_fields:
            payload = {
                "action_id": "act-1",
                "kind": "RUN_WORKERS",
                "workers": [
                    {
                        "worker_id": "w1",
                        "role": "GENERAL",
                        dangerous: "rm -rf /",
                    }
                ],
            }
            with self.assertRaises(ForbiddenFieldError, msg=f"Failed to reject dangerous field {dangerous}"):
                parse_coordinator_action(payload)

    def test_multiple_mutating_executors_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_EXECUTOR",
            "workers": [
                {
                    "worker_id": "w-exec-1",
                    "role": "EXECUTOR",
                    "workspace_mode": "MUTATING",
                },
                {
                    "worker_id": "w-exec-2",
                    "role": "EXECUTOR",
                    "workspace_mode": "MUTATING",
                },
            ],
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("RUN_EXECUTOR", str(cm.exception))

    def test_mutation_by_normal_worker_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_WORKERS",
            "workers": [
                {
                    "worker_id": "w-1",
                    "role": "GENERAL",
                    "workspace_mode": "MUTATING",
                }
            ],
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("Mutating workers", str(cm.exception))

    def test_mutation_in_synthesis_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_SYNTHESIS",
            "workers": [
                {
                    "worker_id": "w-syn",
                    "role": "SYNTHESIZER",
                    "workspace_mode": "MUTATING",
                }
            ],
        }
        with self.assertRaises(ActionValidationError):
            parse_coordinator_action(payload)

    def test_duplicate_worker_ids_in_workers_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_WORKERS",
            "workers": [
                {"worker_id": "w-same", "role": "GENERAL"},
                {"worker_id": "w-same", "role": "ARCHITECTURE"},
            ],
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("Duplicate worker_id 'w-same'", str(cm.exception))

    def test_duplicate_worker_ids_in_auditors_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_AUDITORS",
            "auditors": [
                {"worker_id": "a-same", "target_worker_ids": ["w1"]},
                {"worker_id": "a-same", "target_worker_ids": ["w2"]},
            ],
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("Duplicate worker_id 'a-same'", str(cm.exception))

    def test_empty_worker_wave_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_WORKERS",
            "workers": [],
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("requires at least one worker", str(cm.exception))

    def test_empty_auditor_wave_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_AUDITORS",
            "auditors": [],
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("requires at least one auditor", str(cm.exception))

    def test_finalize_with_workers_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "FINALIZE",
            "workers": [{"worker_id": "w1", "role": "GENERAL"}],
            "final_response": "done",
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("cannot contain workers", str(cm.exception))

    def test_final_response_on_non_finalize_rejected(self) -> None:
        payload = {
            "action_id": "act-1",
            "kind": "RUN_WORKERS",
            "workers": [{"worker_id": "w1", "role": "GENERAL"}],
            "final_response": "premature response",
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload)
        self.assertIn("final_response is only permitted for FINALIZE", str(cm.exception))

    def test_invalid_referenced_worker_in_audit_rejected(self) -> None:
        payload = {
            "action_id": "act-aud",
            "kind": "RUN_AUDITORS",
            "auditors": [
                {
                    "worker_id": "a-1",
                    "target_worker_ids": ["w-existing", "w-nonexistent"],
                    "focus": "Review",
                }
            ],
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload, known_worker_ids={"w-existing"})
        self.assertIn("w-nonexistent", str(cm.exception))

    def test_invalid_referenced_worker_in_worker_context_rejected(self) -> None:
        payload = {
            "action_id": "act-w",
            "kind": "RUN_WORKERS",
            "workers": [
                {
                    "worker_id": "w-2",
                    "role": "GENERAL",
                    "context_worker_ids": ["w-ghost"],
                }
            ],
        }
        with self.assertRaises(ActionValidationError) as cm:
            parse_coordinator_action(payload, known_worker_ids={"w-1"})
        self.assertIn("w-ghost", str(cm.exception))


class TestJSONSchemas(unittest.TestCase):
    """Tests for schema definition accessors."""

    def test_schemas_have_required_structure(self) -> None:
        action_schema = get_coordinator_action_schema()
        self.assertEqual(action_schema["type"], "object")
        self.assertIn("action_id", action_schema["required"])
        self.assertIn("kind", action_schema["required"])
        self.assertFalse(action_schema["additionalProperties"])

        assessment_schema = get_task_assessment_schema()
        self.assertEqual(assessment_schema["type"], "object")
        self.assertIn("task_type", assessment_schema["required"])
        self.assertIn("complexity", assessment_schema["required"])
        self.assertFalse(assessment_schema["additionalProperties"])

        initial_schema = get_initial_response_schema()
        self.assertIn("assessment", initial_schema["required"])
        self.assertIn("action", initial_schema["required"])
        self.assertFalse(initial_schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
