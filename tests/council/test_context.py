"""Tests for Hierarchical Prompt Assembly & Scratch Workspace Staging (Features 22 & 23)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agym.council.artifacts import (
    ArtifactNotFoundError,
    ArtifactStore,
    PathTraversalError,
)
from agym.council.context import (
    DISSENT_MANDATE,
    assemble_prompt,
    clean_attempt_workspace,
    parse_council_sections,
    resolve_attempt_workspace_path,
    setup_attempt_workspace,
)
from agym.council.models import (
    ArtifactRef,
    CouncilOutputSections,
    ExecutionMode,
    StageConfig,
    StageContextPolicy,
    StageKind,
    WorkerConfig,
    WorkflowConfig,
    WorkflowInput,
)


class TestContextPromptAssembly(unittest.TestCase):
    """Test hierarchical prompt composition order and content rules (Feature 22)."""

    def setUp(self) -> None:
        self.worker = WorkerConfig(
            id="worker-analyst",
            name="Analyst Agent",
            account_ref="prof-1",
            model="gemini-2.5-pro",
            role="Data Analyst",
            instructions="Analyze data accurately without extrapolation.",
            task="Evaluate the Q3 revenue metrics.",
        )
        self.stage = StageConfig(
            id="stage-analysis",
            kind=StageKind.INDEPENDENT,
            workers=["worker-analyst"],
            instruction="Review raw metrics and summarize key trends.",
            required_sections=["findings", "evidence_or_assumptions", "uncertainties", "next_action"],
        )
        self.workflow = WorkflowConfig(
            name="Revenue Review",
            goal="Identify revenue growth vectors",
            inputs=[
                WorkflowInput(id="q3_data", description="Q3 spreadsheet", value="art_123"),
            ],
            workers=[self.worker],
            stages=[self.stage],
            final_stage="stage-analysis",
        )

    def test_strict_composition_order(self) -> None:
        """Prompt must follow strict hierarchical order: Rules -> Goal -> Stage -> Worker -> Evidence."""
        prompt = assemble_prompt(
            goal=self.workflow.goal,
            stage=self.stage,
            worker=self.worker,
            workflow=self.workflow,
        )

        pos_rules = prompt.find("# Application Execution & Output Rules")
        pos_goal = prompt.find("# User Goal & Workflow Constraints")
        pos_stage = prompt.find(f"# Stage Contract: {self.stage.id}")
        pos_worker = prompt.find(f"# Worker Assignment: {self.worker.name}")
        pos_evidence = prompt.find("# Released Evidence & Prior-Stage Inputs")

        self.assertNotEqual(pos_rules, -1)
        self.assertNotEqual(pos_goal, -1)
        self.assertNotEqual(pos_stage, -1)
        self.assertNotEqual(pos_worker, -1)
        self.assertNotEqual(pos_evidence, -1)

        self.assertLess(pos_rules, pos_goal, "Rules must precede User Goal")
        self.assertLess(pos_goal, pos_stage, "Goal must precede Stage Contract")
        self.assertLess(pos_stage, pos_worker, "Stage Contract must precede Worker Assignment")
        self.assertLess(pos_worker, pos_evidence, "Worker Assignment must precede Evidence")

    def test_required_sections_and_execution_mode_in_prompt(self) -> None:
        """Rules section must explicitly enumerate required sections and execution mode."""
        prompt = assemble_prompt(
            goal=self.workflow.goal,
            stage=self.stage,
            worker=self.worker,
            workflow=self.workflow,
            execution_mode=ExecutionMode.SUPPLIED_EVIDENCE,
        )

        self.assertIn("Execution Mode: supplied_evidence", prompt)
        for s in self.stage.required_sections:
            self.assertIn(f"`{s}`", prompt)

    def test_dissent_mandate_injection_for_synthesis(self) -> None:
        """Synthesize stage must strictly inject the non-invention of consensus mandate."""
        synth_stage = StageConfig(
            id="stage-synth",
            kind=StageKind.SYNTHESIZE,
            workers=["worker-analyst"],
            instruction="Synthesize perspectives",
        )
        prompt = assemble_prompt(
            goal=self.workflow.goal,
            stage=synth_stage,
            worker=self.worker,
            workflow=self.workflow,
        )

        self.assertIn("IMPORTANT DISSENT MANDATE:", prompt)
        self.assertIn(DISSENT_MANDATE, prompt)

    def test_dissent_mandate_injection_when_dissent_flag_set(self) -> None:
        """When has_dissent is True, mandate must be injected even in non-synthesize stages."""
        prompt = assemble_prompt(
            goal=self.workflow.goal,
            stage=self.stage,
            worker=self.worker,
            workflow=self.workflow,
            has_dissent=True,
        )
        self.assertIn(DISSENT_MANDATE, prompt)

    def test_no_dissent_mandate_in_independent_stage_without_dissent(self) -> None:
        """Normal independent stage without dissent should not inject the mandate."""
        prompt = assemble_prompt(
            goal=self.workflow.goal,
            stage=self.stage,
            worker=self.worker,
            workflow=self.workflow,
            has_dissent=False,
        )
        self.assertNotIn(DISSENT_MANDATE, prompt)

    def test_released_evidence_delimiters(self) -> None:
        """Released evidence must use clear delimiters."""
        art = ArtifactRef(
            id="a" * 64,
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w-1",
            name="notes.txt",
            path="/tmp/notes.txt",
            size_bytes=10,
            sha256="a" * 64,
            released=True,
        )
        art_dict = art.model_dump()
        art_dict["metadata"] = {"content": "Revenue grew by 15%."}

        prompt = assemble_prompt(
            goal="Goal",
            stage=self.stage,
            worker=self.worker,
            released_artifacts=[art_dict],
        )

        self.assertIn("=== Released Artifact: notes.txt", prompt)
        self.assertIn("Revenue grew by 15%.", prompt)
        self.assertIn("=== End of Artifact: notes.txt ===", prompt)


class TestScratchWorkspaceStaging(unittest.TestCase):
    """Test scratch workspace directory isolation and physical barriers (Feature 23)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.artifact_store = ArtifactStore(self.root / "cas")

        # Create a sample artifact in CAS
        res = self.artifact_store.store(b"Prior stage evidence content")
        self.content_hash = res.content_hash

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_attempt_workspace_path(self) -> None:
        """Workspace path must follow the specified directory layout."""
        path = resolve_attempt_workspace_path(
            run_id="run-100",
            worker_id="worker-w1",
            attempt_id="att-1",
            base_dir=self.root,
        )
        expected = self.root / "council" / "runs" / "run-100" / "workers" / "worker-w1" / "attempts" / "att-1" / "workspace"
        self.assertEqual(path, expected)

    def test_setup_attempt_workspace_success(self) -> None:
        """Released artifact is successfully staged into the scratch workspace."""
        art = ArtifactRef(
            id=self.content_hash,
            run_id="run-100",
            stage_id="stage-prior",
            worker_id="worker-peer",
            name="findings.txt",
            path=str(self.artifact_store.get_artifact_path(self.content_hash)),
            size_bytes=28,
            sha256=self.content_hash,
            released=True,
        )

        ws_dir = setup_attempt_workspace(
            run_id="run-100",
            stage_id="stage-current",
            worker_id="worker-target",
            attempt_id="att-1",
            released_artifacts=[art],
            artifact_store=self.artifact_store,
            base_dir=self.root,
            allowed_input_stages=["stage-prior"],
        )

        staged_file = ws_dir / "findings.txt"
        self.assertTrue(staged_file.is_file())
        self.assertEqual(staged_file.read_bytes(), b"Prior stage evidence content")

    def test_unreleased_artifact_staging_raises_error(self) -> None:
        """Attempting to stage an unreleased artifact (released=False) must be blocked."""
        unreleased_art = ArtifactRef(
            id=self.content_hash,
            run_id="run-100",
            stage_id="stage-prior",
            worker_id="worker-peer",
            name="draft.txt",
            path="/path/draft.txt",
            size_bytes=28,
            sha256=self.content_hash,
            released=False,  # UNRELEASED!
        )

        with self.assertRaises(ValueError) as ctx:
            setup_attempt_workspace(
                run_id="run-100",
                stage_id="stage-current",
                worker_id="worker-target",
                attempt_id="att-1",
                released_artifacts=[unreleased_art],
                artifact_store=self.artifact_store,
                base_dir=self.root,
            )
        self.assertIn("unreleased artifact", str(ctx.exception).lower())

    def test_same_stage_peer_artifact_staging_raises_error(self) -> None:
        """Staging an artifact from the current stage must raise an error (no premature leakage)."""
        art = ArtifactRef(
            id=self.content_hash,
            run_id="run-100",
            stage_id="stage-current",  # SAME STAGE!
            worker_id="worker-early-finisher",
            name="early_result.txt",
            path="/path/early.txt",
            size_bytes=28,
            sha256=self.content_hash,
            released=True,
        )

        with self.assertRaises(ValueError) as ctx:
            setup_attempt_workspace(
                run_id="run-100",
                stage_id="stage-current",
                worker_id="worker-target",
                attempt_id="att-1",
                released_artifacts=[art],
                artifact_store=self.artifact_store,
                base_dir=self.root,
            )
        self.assertIn("current stage", str(ctx.exception).lower())

    def test_disallowed_input_stage_staging_raises_error(self) -> None:
        """Artifact from an undeclared input stage must raise an error."""
        art = ArtifactRef(
            id=self.content_hash,
            run_id="run-100",
            stage_id="stage-other",
            worker_id="worker-other",
            name="other.txt",
            path="/path/other.txt",
            size_bytes=28,
            sha256=self.content_hash,
            released=True,
        )

        with self.assertRaises(ValueError) as ctx:
            setup_attempt_workspace(
                run_id="run-100",
                stage_id="stage-current",
                worker_id="worker-target",
                attempt_id="att-1",
                released_artifacts=[art],
                artifact_store=self.artifact_store,
                base_dir=self.root,
                allowed_input_stages=["stage-permitted"],
            )
        self.assertIn("allowed input_stages", str(ctx.exception))

    def test_path_traversal_in_artifact_name_raises_error(self) -> None:
        """Artifact name with path traversal syntax must be blocked."""
        malicious_art = ArtifactRef(
            id=self.content_hash,
            run_id="run-100",
            stage_id="stage-prior",
            worker_id="worker-peer",
            name="../../etc/passwd",  # Traversal attempt!
            path="/path/bad.txt",
            size_bytes=28,
            sha256=self.content_hash,
            released=True,
        )

        with self.assertRaises(PathTraversalError):
            setup_attempt_workspace(
                run_id="run-100",
                stage_id="stage-current",
                worker_id="worker-target",
                attempt_id="att-1",
                released_artifacts=[malicious_art],
                artifact_store=self.artifact_store,
                base_dir=self.root,
            )


class TestParseCouncilSections(unittest.TestCase):
    """Test output section parsing across multiple strategies (Feature 26)."""

    def test_direct_json_parse(self) -> None:
        data = {
            "findings": "All systems operational.",
            "evidence_or_assumptions": "Telemetry logs clean.",
            "uncertainties": "Minor latency fluctuation.",
            "next_action": "Deploy v2.0.",
        }
        sections, parsed_dict = parse_council_sections(str(data).replace("'", '"'))
        self.assertIsNotNone(sections)
        self.assertEqual(sections.findings, "All systems operational.")
        self.assertEqual(sections.next_action, "Deploy v2.0.")

    def test_fenced_code_block_json(self) -> None:
        text = """Here is my review:
```json
{
    "findings": "Bug found in line 42.",
    "evidence_or_assumptions": "Index out of bounds exception.",
    "uncertainties": "None.",
    "next_action": "Patch the index check."
}
```
Thanks!"""
        sections, parsed_dict = parse_council_sections(text)
        self.assertIsNotNone(sections)
        self.assertEqual(sections.findings, "Bug found in line 42.")
        self.assertEqual(sections.next_action, "Patch the index check.")

    def test_markdown_headings(self) -> None:
        text = """## Findings
Found three optimizations.

## Evidence or Assumptions
Benchmarks show 20% speedup.

## Uncertainties
Hardware variance may alter gains.

## Next Action
Benchmark on ARM64."""
        sections, parsed_dict = parse_council_sections(text)
        self.assertIsNotNone(sections)
        self.assertEqual(sections.findings, "Found three optimizations.")
        self.assertEqual(sections.next_action, "Benchmark on ARM64.")

    def test_numbered_markdown_headings(self) -> None:
        text = """## 1. Findings
Found two critical memory leaks in worker cache.

## 2. Evidence or Assumptions
Valgrind reports 4MB leaked per run.

## 3. Uncertainties
Impact on FreeBSD not yet verified.

## 4. Next Action
Patch the allocator free path."""
        sections, parsed_dict = parse_council_sections(text)
        self.assertIsNotNone(sections)
        self.assertEqual(sections.findings, "Found two critical memory leaks in worker cache.")
        self.assertEqual(sections.evidence_or_assumptions, "Valgrind reports 4MB leaked per run.")
        self.assertEqual(sections.uncertainties, "Impact on FreeBSD not yet verified.")
        self.assertEqual(sections.next_action, "Patch the allocator free path.")

    def test_bold_markdown_labels(self) -> None:
        text = """**Findings:**
Algorithm complexity is O(N^2).

**Evidence or Assumptions:**
Profiling on 10k items took 8 seconds.

**Uncertainties:**
Input is typically under 100 items.

**Next Action:**
Replace nested loops with hash map."""
        sections, parsed_dict = parse_council_sections(text)
        self.assertIsNotNone(sections)
        self.assertEqual(sections.findings, "Algorithm complexity is O(N^2).")
        self.assertEqual(sections.evidence_or_assumptions, "Profiling on 10k items took 8 seconds.")
        self.assertEqual(sections.uncertainties, "Input is typically under 100 items.")
        self.assertEqual(sections.next_action, "Replace nested loops with hash map.")

    def test_empty_string(self) -> None:
        sections, parsed_dict = parse_council_sections("")
        self.assertIsNone(sections)
        self.assertIsNone(parsed_dict)


if __name__ == "__main__":
    unittest.main()
