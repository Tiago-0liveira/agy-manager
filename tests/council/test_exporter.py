"""Tests for run dossier exporter, secret redaction, and traversal resistance."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from agym.council.api.app import create_app
from agym.council.api.exporter import (
    export_run_dossier_bytes,
    export_run_to_file,
    sanitize_workflow_for_export,
    scrub_sensitive_strings,
)
from agym.council.api.security import get_or_create_session_token
from agym.council.artifacts import ArtifactStore
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.models import (
    ArtifactRef,
    RunStatus,
    StageConfig,
    StageKind,
    WorkerConfig,
    WorkflowConfig,
    WorkflowInput,
)
from agym.council.storage import (
    create_run,
    get_connection,
    init_db,
    record_event,
    record_issue,
    store_artifact_record,
)


class TestExporter(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        self.db_path = self.root_path / "test_council.db"
        self.store_dir = self.root_path / "artifacts"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        init_db(self.db_path)

        self.conn = get_connection(self.db_path)
        self.artifact_store = ArtifactStore(self.store_dir)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # 1. String Scrubbing & Secret Redaction
    # -----------------------------------------------------------------------

    def test_scrub_sensitive_strings_tokens(self) -> None:
        """scrub_sensitive_strings must redact Bearer tokens, token params, and auth codes."""
        text = "Authorization: Bearer ya29.a0AfH6SMCxyz1234567890abcdef and token=secret_value_12345678"
        scrubbed = scrub_sensitive_strings(text)
        self.assertNotIn("ya29.a0AfH6SMCxyz1234567890abcdef", scrubbed)
        self.assertNotIn("secret_value_12345678", scrubbed)
        self.assertIn("Bearer [REDACTED_TOKEN]", scrubbed)
        self.assertIn("token=[REDACTED_TOKEN]", scrubbed)

        auth_code_text = "Redirected with auth_code=4/0AX4XfWiabcdef1234567890"
        scrubbed_auth = scrub_sensitive_strings(auth_code_text)
        self.assertNotIn("4/0AX4XfWiabcdef1234567890", scrubbed_auth)
        self.assertIn("auth_code=[REDACTED_AUTH_CODE]", scrubbed_auth)

    def test_scrub_sensitive_strings_paths(self) -> None:
        """scrub_sensitive_strings must mask absolute user home paths."""
        linux_path = "Log saved to /home/johndoe/project/council/run.log"
        scrubbed_linux = scrub_sensitive_strings(linux_path)
        self.assertEqual(scrubbed_linux, "Log saved to /home/[USER]/project/council/run.log")

        windows_path = r"Config located at C:\Users\Alice\AppData\Roaming\agym"
        scrubbed_win = scrub_sensitive_strings(windows_path)
        self.assertEqual(scrubbed_win, r"Config located at C:\Users\[USER]\AppData\Roaming\agym")

    # -----------------------------------------------------------------------
    # 2. Workflow Sanitization for Export
    # -----------------------------------------------------------------------

    def test_sanitize_workflow_for_export(self) -> None:
        """sanitize_workflow_for_export produces portable workflow template with placeholders."""
        config = {
            "name": "Exported Workflow",
            "goal": "Test goal",
            "draft": False,
            "workers": [
                {"id": "w1", "name": "Worker 1", "account_ref": "profile-personal", "model": "gemini-1.5-pro"},
                {"id": "w2", "name": "Worker 2", "account_ref": "profile-work", "model": "claude-3-opus"},
            ],
            "stages": [
                {"id": "s1", "kind": "independent", "workers": ["w1", "w2"]},
            ],
        }

        portable = sanitize_workflow_for_export(config)
        self.assertTrue(portable["draft"])
        self.assertEqual(portable["workers"][0]["account_ref"], "<assign-local-profile-1>")
        self.assertEqual(portable["workers"][0]["model"], "<choose-discovered-model>")
        self.assertEqual(portable["workers"][1]["account_ref"], "<assign-local-profile-2>")
        self.assertEqual(portable["workers"][1]["model"], "<choose-discovered-model>")
        # Stage structure is preserved
        self.assertEqual(len(portable["stages"]), 1)
        self.assertEqual(portable["stages"][0]["id"], "s1")

    # -----------------------------------------------------------------------
    # 3. Full Dossier ZIP Export
    # -----------------------------------------------------------------------

    def _create_sample_run(self) -> str:
        run_cfg = WorkflowConfig(
            name="Exporter Test Run",
            goal="Test full dossier export with sensitive content",
            inputs=[WorkflowInput(id="brief", description="Test brief", required=True)],
            workers=[
                WorkerConfig(
                    id="worker_1",
                    name="Test Worker",
                    account_ref="real-account-alice",
                    model="model-test",
                    role="analyst",
                    instructions="Do work",
                    task="Analyze text",
                )
            ],
            stages=[
                StageConfig(
                    id="stage_1",
                    kind=StageKind.INDEPENDENT,
                    workers=["worker_1"],
                    instruction="Analyze input",
                )
            ],
            final_stage="stage_1",
        )
        run_id = create_run(self.conn, run_cfg, stages=run_cfg.stages, workers=run_cfg.workers)

        # Store an artifact with sensitive text
        sensitive_text = "Result with token=abc123456789012345 in /home/alice/secret"
        res = self.artifact_store.store(sensitive_text.encode("utf-8"))
        content_hash = res.content_hash
        artifact = ArtifactRef(
            id="art-100",
            run_id=run_id,
            stage_id="stage_1",
            worker_id="worker_1",
            attempt_id=None,
            name="analysis.txt",
            path=str(self.artifact_store.get_artifact_path(content_hash).relative_to(self.store_dir)),
            size_bytes=len(sensitive_text.encode("utf-8")),
            sha256=content_hash,
            released=True,
            mime_type="text/plain",
        )
        store_artifact_record(self.conn, artifact)

        # Record an issue
        record_issue(
            self.conn,
            run_id=run_id,
            stage_id="stage_1",
            worker_id="worker_1",
            attempt_id="att-1",
            severity="warning",
            category="quota",
            message="Approaching quota limit for real-account-alice",
        )

        # Record an event
        record_event(
            self.conn,
            run_id=run_id,
            event_type="stage.completed",
            stage_id="stage_1",
            payload={"note": "Stage 1 finished successfully"},
        )

        return run_id

    def test_export_run_dossier_bytes(self) -> None:
        """export_run_dossier_bytes generates a valid ZIP containing all dossier elements."""
        run_id = self._create_sample_run()

        zip_bytes = export_run_dossier_bytes(
            self.conn,
            run_id=run_id,
            redact=True,
            artifact_store_root=self.store_dir,
        )
        self.assertGreater(len(zip_bytes), 0)

        # Inspect ZIP structure
        with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
            namelist = zf.namelist()
            self.assertIn("run_summary.json", namelist)
            self.assertIn("workflow_template.json", namelist)
            self.assertIn("issues.json", namelist)
            self.assertIn("events.json", namelist)
            self.assertIn("artifacts/art-100_analysis.txt", namelist)

            # Check run_summary.json content
            summary = json.loads(zf.read("run_summary.json").decode("utf-8"))
            self.assertEqual(summary["run_id"], run_id)
            self.assertEqual(summary["name"], "Exporter Test Run")
            # Account ref in summary was redacted
            self.assertEqual(summary["workers"][0]["account_ref"], "[LOCAL_PROFILE]")

            # Check workflow_template.json
            template = json.loads(zf.read("workflow_template.json").decode("utf-8"))
            self.assertTrue(template["draft"])
            self.assertEqual(template["workers"][0]["account_ref"], "<assign-local-profile-1>")

            # Check artifact text was scrubbed
            art_content = zf.read("artifacts/art-100_analysis.txt").decode("utf-8")
            self.assertNotIn("abc123456789012345", art_content)
            self.assertNotIn("/home/alice", art_content)
            self.assertIn("token=[REDACTED_TOKEN]", art_content)
            self.assertIn("/home/[USER]", art_content)

    def test_export_run_to_file(self) -> None:
        """export_run_to_file writes the zip dossier directly to a filesystem path."""
        run_id = self._create_sample_run()
        out_file = self.root_path / "exports" / f"run_{run_id}.zip"

        dest = export_run_to_file(
            self.conn,
            run_id=run_id,
            output_path=out_file,
            redact=True,
            artifact_store_root=self.store_dir,
        )
        self.assertTrue(dest.is_file())
        self.assertEqual(dest, out_file)
        self.assertTrue(zipfile.is_zipfile(dest))

    def test_export_nonexistent_run_raises(self) -> None:
        """export_run_dossier_bytes raises ValueError for unknown run_id."""
        with self.assertRaises(ValueError):
            export_run_dossier_bytes(self.conn, run_id="nonexistent-run-id")

    def test_export_api_endpoint(self) -> None:
        """POST /api/runs/{run_id}/export returns a downloadable ZIP attachment."""
        run_id = self._create_sample_run()
        provider = FakeProviderAdapter()
        app = create_app(db_path=self.db_path, provider_adapter=provider)
        client = TestClient(app)
        session_token = get_or_create_session_token()
        headers = {"X-Council-Session": session_token}

        resp = client.post(f"/api/runs/{run_id}/export", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/zip")
        self.assertIn(f"attachment; filename=\"council_run_{run_id}.zip\"", resp.headers.get("content-disposition", ""))

        # Verify response body is valid zip
        zf = zipfile.ZipFile(io.BytesIO(resp.content), mode="r")
        self.assertIn("run_summary.json", zf.namelist())

        # Unknown run returns 404
        resp_404 = client.post("/api/runs/nonexistent-run-xyz/export", headers=headers)
        self.assertEqual(resp_404.status_code, 404)


if __name__ == "__main__":
    unittest.main()
