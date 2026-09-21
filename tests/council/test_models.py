"""Unit and integration tests for AGYM Council domain models.

Covers:
- Blueprint presets validation
- Workflow DAG acyclicity and validation rules
- Limits configuration invariants
- Live run resolution vs draft mode
- Output section schema enforcement
- Turn contracts and enums
- Relational persistence models
- Redaction and serialization helpers
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from agym.council.models import (
    Account,
    AccountAuthStatus,
    ArtifactRef,
    Attempt,
    AttemptStatus,
    CouncilEvent,
    CouncilOutputSections,
    IdempotencyRecord,
    IssueRecord,
    IssueSeverity,
    LimitsConfig,
    PermissionPolicy,
    ProviderType,
    Run,
    RunStatus,
    StageConfig,
    StageContextPolicy,
    StageKind,
    TurnAllowance,
    TurnRequest,
    TurnResult,
    TurnStatus,
    WorkerConfig,
    WorkflowConfig,
    WorkflowInput,
    export_workflow_to_json,
    load_workflow_from_file,
    redact_secrets,
)

BLUEPRINT_DIR = Path(__file__).resolve().parents[2] / "agy_council_blueprint"


class TestBlueprintPresets(unittest.TestCase):
    """Verify that all official blueprint workflow presets load and validate cleanly."""

    def test_quick_council_preset(self) -> None:
        path = BLUEPRINT_DIR / "examples" / "quick_council.json"
        cfg = load_workflow_from_file(path)
        self.assertEqual(cfg.schema_version, 1)
        self.assertTrue(cfg.draft)
        self.assertEqual(len(cfg.workers), 3)
        self.assertEqual(len(cfg.stages), 3)
        self.assertEqual(cfg.final_stage, "answer")
        base_calls = sum(len(s.workers) for s in cfg.stages)
        self.assertEqual(base_calls, 6)
        self.assertLessEqual(base_calls, cfg.limits.max_model_calls)

    def test_research_and_design_preset(self) -> None:
        path = BLUEPRINT_DIR / "examples" / "research_and_design.json"
        cfg = load_workflow_from_file(path)
        self.assertEqual(len(cfg.workers), 4)
        self.assertEqual(len(cfg.stages), 6)
        self.assertEqual(cfg.final_stage, "dossier")
        base_calls = sum(len(s.workers) for s in cfg.stages)
        self.assertEqual(base_calls, 15)
        self.assertLessEqual(base_calls, cfg.limits.max_model_calls)

    def test_review_and_revise_preset(self) -> None:
        path = BLUEPRINT_DIR / "examples" / "review_and_revise.json"
        cfg = load_workflow_from_file(path)
        self.assertEqual(len(cfg.workers), 2)
        self.assertEqual(len(cfg.stages), 5)
        self.assertEqual(cfg.final_stage, "delivery")
        base_calls = sum(len(s.workers) for s in cfg.stages)
        self.assertEqual(base_calls, 5)
        self.assertLessEqual(base_calls, cfg.limits.max_model_calls)


class TestWorkflowValidationNegativeCases(unittest.TestCase):
    """Test validation failure modes and edge cases on WorkflowConfig."""

    def setUp(self) -> None:
        path = BLUEPRINT_DIR / "examples" / "quick_council.json"
        self.base_data = json.loads(path.read_text(encoding="utf-8"))

    def test_duplicate_worker_id(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["workers"].append(copy.deepcopy(d["workers"][0]))
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("Duplicate worker ID", str(cm.exception))

    def test_stage_references_unknown_worker(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["stages"][0]["workers"].append("unknown_worker_x")
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("unknown worker", str(cm.exception))

    def test_stage_duplicate_worker(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["stages"][0]["workers"].append(d["stages"][0]["workers"][0])
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("Duplicate workers detected", str(cm.exception))

    def test_future_input_stage_reference(self) -> None:
        d = copy.deepcopy(self.base_data)
        # First stage references the last stage
        d["stages"][0]["input_stages"].append(d["stages"][-1]["id"])
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("input_stages refers to future or missing", str(cm.exception))

    def test_self_input_stage_reference(self) -> None:
        d = copy.deepcopy(self.base_data)
        # Stage references itself
        d["stages"][1]["input_stages"].append(d["stages"][1]["id"])
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("input_stages refers to future or missing", str(cm.exception))

    def test_cyclic_input_stage_reference(self) -> None:
        d = copy.deepcopy(self.base_data)
        # Stage 0 attempts to reference stage 1 (forward/cycle)
        d["stages"][0]["input_stages"] = [d["stages"][1]["id"]]
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("input_stages refers to future or missing", str(cm.exception))

    def test_budget_exceeded(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["limits"]["max_model_calls"] = 2  # base calls is 6
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("Required calls (6) exceed budget", str(cm.exception))

    def test_mismatched_final_stage(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["final_stage"] = "nonexistent_final_stage"
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("Invalid final stage", str(cm.exception))

    def test_unsupported_schema_version(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["schema_version"] = 2
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("Unsupported schema version", str(cm.exception))

    def test_duplicate_stage_id(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["stages"].append(copy.deepcopy(d["stages"][0]))
        d["final_stage"] = d["stages"][-1]["id"]
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("Duplicate stage ID", str(cm.exception))

    def test_duplicate_input_id(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["inputs"].append(copy.deepcopy(d["inputs"][0]))
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("Duplicate input IDs", str(cm.exception))


class TestLimitsConstraints(unittest.TestCase):
    """Test boundary checks on LimitsConfig."""

    def test_positive_concurrency_limits(self) -> None:
        with self.assertRaises(ValidationError):
            LimitsConfig(global_concurrency=0)
        with self.assertRaises(ValidationError):
            LimitsConfig(per_account_concurrency=0)
        with self.assertRaises(ValidationError):
            LimitsConfig(max_model_calls=0)
        with self.assertRaises(ValidationError):
            LimitsConfig(max_wall_seconds=0)

    def test_retry_boundary(self) -> None:
        cfg = LimitsConfig(max_retries_per_task=0)
        self.assertEqual(cfg.max_retries_per_task, 0)
        with self.assertRaises(ValidationError):
            LimitsConfig(max_retries_per_task=-1)

    def test_automatic_account_switching_invariant(self) -> None:
        with self.assertRaises(ValueError) as cm:
            LimitsConfig(automatic_account_switching=True)
        self.assertIn("must be False to preserve account isolation", str(cm.exception))


class TestLiveRunBindingValidation(unittest.TestCase):
    """Test transitions between draft presets and live execution-ready workflows."""

    def setUp(self) -> None:
        path = BLUEPRINT_DIR / "examples" / "quick_council.json"
        self.base_data = json.loads(path.read_text(encoding="utf-8"))

    def test_draft_allows_placeholders(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["draft"] = True
        cfg = WorkflowConfig.model_validate(d)
        self.assertTrue(cfg.draft)
        self.assertEqual(cfg.workers[0].model, "<choose-discovered-model>")

    def test_live_run_rejects_placeholder_model(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["draft"] = False
        d["inputs"][0]["value"] = "Real brief text"
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("is a placeholder; must be bound before execution", str(cm.exception))

    def test_live_run_rejects_unbound_required_inputs(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["draft"] = False
        for w in d["workers"]:
            w["model"] = "gemini-2.5-pro"
        # Input value is None
        with self.assertRaises(ValueError) as cm:
            WorkflowConfig.model_validate(d)
        self.assertIn("has no value bound for execution", str(cm.exception))

    def test_live_run_succeeds_when_fully_bound(self) -> None:
        d = copy.deepcopy(self.base_data)
        d["draft"] = False
        for w in d["workers"]:
            w["model"] = "gemini-2.5-pro"
        d["inputs"][0]["value"] = "Concrete evaluation brief"
        cfg = WorkflowConfig.model_validate(d)
        self.assertFalse(cfg.draft)
        self.assertEqual(cfg.inputs[0].value, "Concrete evaluation brief")


class TestOutputSections(unittest.TestCase):
    """Test CouncilOutputSections structured model and formatting."""

    def test_validate_required_detects_missing(self) -> None:
        sections = CouncilOutputSections(
            findings="Key finding",
            evidence_or_assumptions="",  # missing
            uncertainties="None",
            next_action="",  # missing
        )
        missing = sections.validate_required(
            ["findings", "evidence_or_assumptions", "uncertainties", "next_action"]
        )
        self.assertEqual(missing, ["evidence_or_assumptions", "next_action"])

    def test_validate_required_passes_when_all_present(self) -> None:
        sections = CouncilOutputSections(
            findings="Findings",
            evidence_or_assumptions="Evidence",
            uncertainties="None",
            next_action="Deploy",
        )
        missing = sections.validate_required(
            ["findings", "evidence_or_assumptions", "uncertainties", "next_action"]
        )
        self.assertEqual(missing, [])

    def test_to_markdown_formatting(self) -> None:
        sections = CouncilOutputSections(
            findings="Point 1",
            evidence_or_assumptions="Citation A",
            uncertainties="Unknown B",
            next_action="Step C",
        )
        md = sections.to_markdown()
        self.assertIn("## Findings\nPoint 1", md)
        self.assertIn("## Evidence or Assumptions\nCitation A", md)
        self.assertIn("## Uncertainties\nUnknown B", md)
        self.assertIn("## Next Action\nStep C", md)


class TestTurnContracts(unittest.TestCase):
    """Test TurnRequest, TurnResult, and normalized TurnStatus enums."""

    def test_turn_request_sync_and_validation(self) -> None:
        req = TurnRequest(
            attempt_id="att-100",
            run_id="run-200",
            stage_id="stage-300",
            worker_id="worker-400",
            account_ref="profile-default",
            model="gemini-2.5-flash",
            prompt="Analyze input data",
            conversation_handle="conv-handle-1",
        )
        self.assertEqual(req.conversation_id, "conv-handle-1")
        self.assertEqual(req.conversation_handle, "conv-handle-1")

    def test_turn_status_normalization(self) -> None:
        # Both hyphen and underscore variants must be normalized
        self.assertEqual(TurnStatus("success"), TurnStatus.SUCCESS)
        self.assertEqual(TurnStatus("needs-auth"), TurnStatus.NEEDS_AUTH)
        self.assertEqual(TurnStatus("needs_auth"), TurnStatus.NEEDS_AUTH)
        self.assertEqual(TurnStatus("rate-limited"), TurnStatus.RATE_LIMITED)
        self.assertEqual(TurnStatus("rate_limited"), TurnStatus.RATE_LIMITED)
        self.assertEqual(TurnStatus("permission-blocked"), TurnStatus.PERMISSION_BLOCKED)
        self.assertEqual(TurnStatus("malformed-output"), TurnStatus.MALFORMED_OUTPUT)
        self.assertEqual(TurnStatus("unknown-completion"), TurnStatus.UNKNOWN_COMPLETION)

    def test_turn_allowance_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            TurnAllowance(timeout_seconds=0)
        with self.assertRaises(ValidationError):
            TurnAllowance(timeout_seconds=-10)
        with self.assertRaises(ValidationError):
            TurnAllowance(call_budget=0)


class TestPersistenceModels(unittest.TestCase):
    """Test Account, ArtifactRef, Attempt, Run, and Event domain entities."""

    def test_account_model(self) -> None:
        acc = Account(
            profile_ref="prof-prod",
            label="Production Profile",
            provider=ProviderType.ANTIGRAVITY,
            auth_status=AccountAuthStatus.READY,
            concurrency_limit=2,
        )
        self.assertEqual(acc.concurrency_limit, 2)
        self.assertTrue(acc.enabled)
        with self.assertRaises(ValidationError):
            Account(profile_ref="bad", label="bad", concurrency_limit=0)

    def test_artifact_ref_sha256_validation(self) -> None:
        valid_hash = "a" * 64
        art = ArtifactRef(
            id="art-1",
            run_id="run-1",
            name="file.txt",
            path="cas/path",
            size_bytes=100,
            sha256=valid_hash,
        )
        self.assertEqual(art.sha256, valid_hash)

        # Invalid length
        with self.assertRaises(ValueError):
            ArtifactRef(
                id="art-1",
                run_id="run-1",
                name="file.txt",
                path="cas/path",
                size_bytes=100,
                sha256="abc",
            )
        # Non-hex characters
        with self.assertRaises(ValueError):
            ArtifactRef(
                id="art-1",
                run_id="run-1",
                name="file.txt",
                path="cas/path",
                size_bytes=100,
                sha256="g" * 64,
            )

    def test_event_sequence_monotonicity(self) -> None:
        ev = CouncilEvent(
            sequence=1,
            run_id="run-1",
            type="run.started",
            payload={"action": "start"},
        )
        self.assertEqual(ev.sequence, 1)
        with self.assertRaises(ValidationError):
            CouncilEvent(sequence=0, run_id="run-1", type="test")

    def test_issue_record_and_idempotency(self) -> None:
        issue = IssueRecord(
            run_id="run-1",
            severity=IssueSeverity.WARNING,
            category="scheduler",
            message="Resource pressure",
        )
        self.assertEqual(issue.severity, IssueSeverity.WARNING)

        rec = IdempotencyRecord(
            key="idemp-key-1",
            action="start_run",
            request_hash="sha256-req",
            response_status=200,
            response_body='{"status":"ok"}',
        )
        self.assertEqual(rec.response_status, 200)


class TestRedactionAndSerialization(unittest.TestCase):
    """Test secret scrubbing and JSON export/import."""

    def test_redact_secrets(self) -> None:
        data = {
            "name": "worker",
            "api_key": "secret-123",
            "auth_token": "bearer-456",
            "nested": {
                "password": "pwd",
                "safe": "visible",
                "tokens_list": [{"token": "tok1"}, {"other": "clean"}],
            },
        }
        cleaned = redact_secrets(data)
        self.assertEqual(cleaned["api_key"], "[REDACTED]")
        self.assertEqual(cleaned["auth_token"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["password"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["safe"], "visible")
        self.assertEqual(cleaned["nested"]["tokens_list"][0]["token"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["tokens_list"][1]["other"], "clean")

    def test_workflow_json_roundtrip(self) -> None:
        path = BLUEPRINT_DIR / "examples" / "quick_council.json"
        original = load_workflow_from_file(path)
        json_str = export_workflow_to_json(original)
        reloaded = WorkflowConfig.model_validate(json.loads(json_str))
        self.assertEqual(original.name, reloaded.name)
        self.assertEqual(len(original.stages), len(reloaded.stages))


if __name__ == "__main__":
    unittest.main()
