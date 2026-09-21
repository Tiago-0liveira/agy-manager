"""Tests for bundled workflow presets and presets helper functions."""

from __future__ import annotations

import unittest
from pathlib import Path

from agym.council.models import WorkflowConfig
from agym.council.presets import get_presets_dir, list_presets, load_preset_raw, load_preset_workflow


class TestPresets(unittest.TestCase):
    def test_presets_directory_exists(self) -> None:
        """The bundled presets directory must exist and contain JSON files."""
        pdir = get_presets_dir()
        self.assertTrue(pdir.is_dir(), f"Presets directory does not exist: {pdir}")
        json_files = list(pdir.glob("*.json"))
        self.assertGreaterEqual(len(json_files), 3)

    def test_list_presets(self) -> None:
        """list_presets returns metadata for all bundled presets."""
        presets = list_presets()
        self.assertGreaterEqual(len(presets), 3)
        preset_ids = {p["id"] for p in presets}
        expected_ids = {"quick_council", "research_and_design", "review_and_revise"}
        self.assertTrue(expected_ids.issubset(preset_ids), f"Missing expected presets in: {preset_ids}")

        for p in presets:
            self.assertIn("id", p)
            self.assertIn("name", p)
            self.assertIn("goal", p)
            self.assertGreaterEqual(p["workers_count"], 1)
            self.assertGreaterEqual(p["stages_count"], 1)
            self.assertEqual(p["schema_version"], 1)

    def test_load_preset_raw(self) -> None:
        """load_preset_raw loads raw JSON dict with and without .json extension."""
        raw1 = load_preset_raw("quick_council")
        raw2 = load_preset_raw("quick_council.json")
        self.assertIsNotNone(raw1)
        self.assertEqual(raw1, raw2)
        self.assertEqual(raw1["name"], "Quick council")
        self.assertEqual(raw1["schema_version"], 1)

        # Nonexistent returns None
        self.assertIsNone(load_preset_raw("nonexistent_preset_xyz"))

    def test_bundled_presets_validate_schema(self) -> None:
        """All bundled presets must successfully validate as WorkflowConfig instances."""
        preset_names = ["quick_council", "research_and_design", "review_and_revise"]
        for name in preset_names:
            wf = load_preset_workflow(name)
            self.assertIsNotNone(wf, f"Failed to load/validate preset: {name}")
            self.assertIsInstance(wf, WorkflowConfig)
            self.assertEqual(wf.schema_version, 1)
            self.assertTrue(len(wf.workers) > 0)
            self.assertTrue(len(wf.stages) > 0)
            # WorkflowConfig model validation already validates the graph (acyclic DAG, unique workers/stages)
            self.assertGreaterEqual(len(wf.workers), 1)
            self.assertGreaterEqual(len(wf.stages), 1)

    def test_quick_council_structure(self) -> None:
        """quick_council preset has coordinator, analyst, critic and 3 stages."""
        wf = load_preset_workflow("quick_council")
        self.assertIsNotNone(wf)
        worker_ids = {w.id for w in wf.workers}
        self.assertEqual(worker_ids, {"coordinator", "analyst", "critic"})
        stage_ids = [s.id for s in wf.stages]
        self.assertEqual(stage_ids, ["independent", "critique", "answer"])

    def test_review_and_revise_structure(self) -> None:
        """review_and_revise preset has author, reviewer roles and 5 stages."""
        wf = load_preset_workflow("review_and_revise")
        self.assertIsNotNone(wf)
        worker_ids = {w.id for w in wf.workers}
        self.assertEqual(worker_ids, {"author", "reviewer"})
        stage_ids = [s.id for s in wf.stages]
        self.assertEqual(stage_ids, ["draft", "review", "revision", "check", "delivery"])

    def test_research_and_design_structure(self) -> None:
        """research_and_design preset has lead, falsifier, designer_a, designer_b roles and 6 stages."""
        wf = load_preset_workflow("research_and_design")
        self.assertIsNotNone(wf)
        worker_ids = {w.id for w in wf.workers}
        self.assertEqual(worker_ids, {"lead", "falsifier", "designer_a", "designer_b"})
        stage_ids = [s.id for s in wf.stages]
        self.assertEqual(stage_ids, ["diagnoses", "requirements", "concepts", "cross_review", "revisions", "dossier"])


if __name__ == "__main__":
    unittest.main()
