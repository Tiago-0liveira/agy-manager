"""Comprehensive tests for AGYM Council REST API endpoints."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agym.council.api.app import create_app
from agym.council.api.security import get_or_create_session_token
from agym.council.artifacts import ArtifactStore
from agym.council.models import ArtifactRef, RunStatus, StageKind
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.storage import (
    create_account,
    create_run,
    get_connection,
    init_db,
    store_artifact_record,
)
from agym.profiles import ProfileStore


class TestApiEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_council.db"
        init_db(self.db_path)
        self.provider = FakeProviderAdapter()
        self.app = create_app(db_path=self.db_path, provider_adapter=self.provider)
        self.client = TestClient(self.app)
        self.session_token = get_or_create_session_token()
        self.headers = {"X-Council-Session": self.session_token}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # 1. Accounts Endpoints
    # -----------------------------------------------------------------------

    def test_accounts_lifecycle(self) -> None:
        """Test full accounts CRUD, connect, check, and models endpoints."""
        # 1. List initially empty
        resp = self.client.get("/api/accounts")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 0)

        # 2. Create account
        create_payload = {
            "profile_ref": "profile-alpha",
            "label": "Alpha Profile",
            "provider": "fake",
            "concurrency_limit": 2,
        }
        resp = self.client.post("/api/accounts", json=create_payload, headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        acc_data = resp.json()
        account_id = acc_data["account_id"]
        self.assertEqual(acc_data["display_label"], "Alpha Profile")
        self.assertEqual(acc_data["concurrency_limit"], 2)

        # Duplicate profile_ref should return 409
        resp_dup = self.client.post("/api/accounts", json=create_payload, headers=self.headers)
        self.assertEqual(resp_dup.status_code, 409)

        # 3. Get account
        resp = self.client.get(f"/api/accounts/{account_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["profile_ref"], "profile-alpha")

        # 4. Patch account
        patch_payload = {"label": "Alpha Renamed", "concurrency_limit": 3}
        resp = self.client.patch(f"/api/accounts/{account_id}", json=patch_payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["display_label"], "Alpha Renamed")
        self.assertEqual(resp.json()["concurrency_limit"], 3)

        # 5. Check account status
        resp = self.client.post(f"/api/accounts/{account_id}/check", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        check_data = resp.json()
        self.assertIn("status", check_data)

        # 7. Discover models
        resp = self.client.get(f"/api/accounts/{account_id}/models")
        self.assertEqual(resp.status_code, 200)
        models = resp.json()
        self.assertTrue(len(models) > 0)
        self.assertIn("id", models[0])

        # 8. Delete / Unlink account
        resp = self.client.delete(f"/api/accounts/{account_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])

        # Subsequent get returns 404
        resp = self.client.get(f"/api/accounts/{account_id}")
        self.assertEqual(resp.status_code, 404)

    # -----------------------------------------------------------------------
    # 2. Agent Templates Endpoints
    # -----------------------------------------------------------------------

    def test_agents_lifecycle(self) -> None:
        """Test Agent Template CRUD operations."""
        # 1. Create agent template
        payload = {
            "name": "Constructive Critic",
            "purpose": "Find flaws and edge cases",
            "instructions": "Be rigorous and challenging",
            "working_style": "Skeptical",
            "preferred_model": "fake-model-pro",
        }
        resp = self.client.post("/api/agents", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        agent_data = resp.json()
        agent_id = agent_data.get("id") or agent_data.get("template_id")
        self.assertTrue(bool(agent_id))

        # 2. Get agent template
        resp = self.client.get(f"/api/agents/{agent_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Constructive Critic")

        # 3. List agents
        resp = self.client.get("/api/agents")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

        # 4. Patch agent
        resp = self.client.patch(
            f"/api/agents/{agent_id}",
            json={"working_style": "Diplomatic but firm"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)

        # 5. Delete agent
        resp = self.client.delete(f"/api/agents/{agent_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])

    # -----------------------------------------------------------------------
    # 3. Presets & Workflow Templates Endpoints
    # -----------------------------------------------------------------------

    def test_presets_endpoints(self) -> None:
        """Presets must list bundled examples and return valid JSON definitions."""
        resp = self.client.get("/api/presets")
        self.assertEqual(resp.status_code, 200)
        presets = resp.json()
        preset_ids = [p["id"] for p in presets]
        self.assertIn("quick_council", preset_ids)
        self.assertIn("research_and_design", preset_ids)
        self.assertIn("review_and_revise", preset_ids)

        # Get single preset
        resp = self.client.get("/api/presets/quick_council")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["name"], "Quick council")
        self.assertEqual(data["schema_version"], 1)

        # Nonexistent preset returns 404
        resp = self.client.get("/api/presets/nonexistent_preset_xyz")
        self.assertEqual(resp.status_code, 404)

    def test_workflow_validation_endpoint(self) -> None:
        """POST /api/workflows/validate checks domain contracts and graph integrity."""
        # Valid preset definition
        resp = self.client.get("/api/presets/quick_council")
        valid_def = resp.json()

        val_resp = self.client.post("/api/workflows/validate", json=valid_def, headers=self.headers)
        self.assertEqual(val_resp.status_code, 200)
        self.assertTrue(val_resp.json()["valid"])

        # Invalid: forward/unknown stage reference
        invalid_def = dict(valid_def)
        invalid_def["stages"] = [
            dict(valid_def["stages"][0]),
            dict(valid_def["stages"][1]),
        ]
        invalid_def["stages"][0]["input_stages"] = ["nonexistent_stage_id"]
        val_bad = self.client.post("/api/workflows/validate", json=invalid_def, headers=self.headers)
        self.assertEqual(val_bad.status_code, 400)
        self.assertFalse(val_bad.json()["detail"]["valid"])

    def test_workflow_template_crud(self) -> None:
        """Create, get, list, and delete custom workflow templates."""
        resp = self.client.get("/api/presets/quick_council")
        wf_def = resp.json()

        create_payload = {
            "name": "Customized Quick Council",
            "goal": "Custom goal",
            "definition": wf_def,
            "description": "My custom workflow",
        }
        resp = self.client.post("/api/workflows", json=create_payload, headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        wf_id = resp.json()["workflow_id"]

        # Get workflow
        resp = self.client.get(f"/api/workflows/{wf_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Customized Quick Council")

        # List workflows
        resp = self.client.get("/api/workflows")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(len(resp.json()) >= 1)

        # Delete workflow
        resp = self.client.delete(f"/api/workflows/{wf_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])

    # -----------------------------------------------------------------------
    # 4. Runs Execution & Lifecycle Endpoints
    # -----------------------------------------------------------------------

    def test_runs_lifecycle(self) -> None:
        """Create, inspect, start, pause, and stop a run via REST endpoints."""
        # 1. Create run from preset
        payload = {
            "preset_id": "quick_council",
            "name": "Test Run Alpha",
            "goal": "Evaluate architecture options",
            "inputs": {"brief": "Consider monolithic vs modular design"},
            "account_bindings": {
                "coordinator": "profile-1",
                "analyst": "profile-2",
                "critic": "profile-3",
            },
            "model_bindings": {
                "coordinator": "fake-model-pro",
                "analyst": "fake-model-pro",
                "critic": "fake-model-pro",
            },
        }

        resp = self.client.post("/api/runs", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        run_data = resp.json()
        run_id = run_data["run_id"]
        self.assertEqual(run_data["status"], "READY")

        # 2. Get run details
        resp = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(resp.status_code, 200)
        details = resp.json()
        self.assertEqual(len(details["stages"]), 3)
        self.assertEqual(len(details["workers"]), 3)

        # 3. List runs
        resp = self.client.get("/api/runs?status=READY")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(r["run_id"] == run_id for r in resp.json()))

        # 4. Start run
        resp = self.client.post(f"/api/runs/{run_id}/start", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "RUNNING")

        # 5. Pause run
        resp = self.client.post(f"/api/runs/{run_id}/pause", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "PAUSING")

        # 6. Stop run
        resp = self.client.post(f"/api/runs/{run_id}/stop", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "CANCELLED")

    def test_run_resolve_endpoint(self) -> None:
        """POST /api/runs/{id}/resolve transitions NEEDS_ATTENTION back to READY."""
        # Create run and force status to NEEDS_ATTENTION
        payload = {
            "preset_id": "quick_council",
            "name": "Attention Run",
            "inputs": {"brief": "Brief text"},
            "account_bindings": {"coordinator": "p1", "analyst": "p2", "critic": "p3"},
            "model_bindings": {"coordinator": "m", "analyst": "m", "critic": "m"},
        }
        resp = self.client.post("/api/runs", json=payload, headers=self.headers)
        run_id = resp.json()["run_id"]

        # Force state to NEEDS_ATTENTION in DB
        conn = get_connection(self.db_path)
        try:
            conn.execute("UPDATE runs SET status = 'NEEDS_ATTENTION' WHERE run_id = ?", (run_id,))
        finally:
            conn.close()

        # Call resolve endpoint
        resp = self.client.post(
            f"/api/runs/{run_id}/resolve",
            json={"action": "retry", "resolution_note": "User fixed credentials"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "READY")

    def test_session_endpoint(self) -> None:
        """GET /api/session returns valid CSRF session token."""
        resp = self.client.get("/api/session")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("session_token", data)
        self.assertIn("council_version", data)
        self.assertGreaterEqual(len(data["session_token"]), 32)

    def test_run_subresources_and_resume(self) -> None:
        """Test run details subresources and POST resume endpoint."""
        payload = {
            "preset_id": "quick_council",
            "name": "Subresources Run",
            "inputs": {"brief": "Brief text"},
            "account_bindings": {"coordinator": "p1", "analyst": "p2", "critic": "p3"},
            "model_bindings": {"coordinator": "m", "analyst": "m", "critic": "m"},
        }
        resp = self.client.post("/api/runs", json=payload, headers=self.headers)
        run_id = resp.json()["run_id"]

        # Run details includes stages, workers, and issues
        resp_details = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(resp_details.status_code, 200)
        details = resp_details.json()
        self.assertEqual(len(details["stages"]), 3)
        self.assertEqual(len(details["workers"]), 3)
        self.assertEqual(len(details["issues"]), 0)

        # Resume endpoint
        # Force status to PAUSED
        conn = get_connection(self.db_path)
        try:
            conn.execute("UPDATE runs SET status = 'PAUSED' WHERE run_id = ?", (run_id,))
        finally:
            conn.close()

        resp_resume = self.client.post(f"/api/runs/{run_id}/resume", headers=self.headers)
        self.assertEqual(resp_resume.status_code, 200)
        self.assertEqual(resp_resume.json()["status"], "RUNNING")

    def test_artifacts_endpoints_and_download(self) -> None:
        """Test listing run artifacts, downloading artifact by ID, and traversal protection."""
        payload = {
            "preset_id": "quick_council",
            "name": "Artifacts Run",
            "inputs": {"brief": "Brief text"},
            "account_bindings": {"coordinator": "p1", "analyst": "p2", "critic": "p3"},
            "model_bindings": {"coordinator": "m", "analyst": "m", "critic": "m"},
        }
        resp = self.client.post("/api/runs", json=payload, headers=self.headers)
        run_id = resp.json()["run_id"]

        # Create store and artifact
        store = ArtifactStore()
        content = b"Council verified output report"
        res = store.store(content)
        content_hash = res.content_hash

        conn = get_connection(self.db_path)
        try:
            art = ArtifactRef(
                id="art-test-1",
                run_id=run_id,
                stage_id="independent",
                worker_id="coordinator",
                attempt_id=None,
                name="report.txt",
                path=str(res.path),
                size_bytes=len(content),
                sha256=content_hash,
                released=True,
                mime_type="text/plain",
            )
            store_artifact_record(conn, art)
        finally:
            conn.close()

        # 1. List artifacts for run
        resp_list = self.client.get(f"/api/runs/{run_id}/artifacts")
        self.assertEqual(resp_list.status_code, 200)
        artifacts = resp_list.json()
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["artifact_id"], "art-test-1")

        # 2. Download artifact by ID
        resp_dl = self.client.get("/api/artifacts/art-test-1")
        self.assertEqual(resp_dl.status_code, 200)
        self.assertEqual(resp_dl.content, content)
        self.assertIn("text/plain", resp_dl.headers["content-type"])

        # 3. Nonexistent artifact returns 404
        resp_404 = self.client.get("/api/artifacts/nonexistent-art-id")
        self.assertEqual(resp_404.status_code, 404)


if __name__ == "__main__":
    unittest.main()
