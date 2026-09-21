"""Tests for API security, loopback enforcement, origin anti-CSRF, session tokens, and idempotency."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agym.council.api.app import create_app
from agym.council.api.security import get_or_create_session_token
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.storage import init_db


class TestApiSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_council.db"
        init_db(self.db_path)
        self.provider = FakeProviderAdapter()
        self.app = create_app(db_path=self.db_path, provider_adapter=self.provider)
        self.client = TestClient(self.app)
        self.session_token = get_or_create_session_token()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_health_endpoint_accessible(self) -> None:
        """Health check must be accessible without authentication."""
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["loopback_only"])

    def test_session_handshake_endpoint(self) -> None:
        """Session handshake returns active session token for local frontend."""
        resp = self.client.get("/api/session")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("session_token", data)
        self.assertEqual(data["session_token"], self.session_token)

    def test_external_host_header_rejected(self) -> None:
        """DNS rebinding attack with external host header must be rejected with 403."""
        resp = self.client.get("/api/health", headers={"Host": "evil-attacker.com"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Forbidden", resp.json()["detail"])

    def test_loopback_host_headers_permitted(self) -> None:
        """Loopback host variations must be permitted."""
        for host in ("127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:8000", "[::1]", "[::1]:8000", "testserver"):
            resp = self.client.get("/api/health", headers={"Host": host})
            self.assertEqual(resp.status_code, 200, f"Host {host} should be permitted")

    def test_external_origin_header_rejected(self) -> None:
        """Cross-site requests from external origins must be rejected with 403."""
        resp = self.client.get("/api/health", headers={"Origin": "https://malicious-site.com"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Forbidden", resp.json()["detail"])

    def test_loopback_origin_headers_permitted(self) -> None:
        """Loopback origins (e.g. Vite dev server or local web UI) must be permitted."""
        for origin in ("http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:3000"):
            resp = self.client.get("/api/health", headers={"Origin": origin})
            self.assertEqual(resp.status_code, 200, f"Origin {origin} should be permitted")

    def test_mutations_require_session_token(self) -> None:
        """Mutating methods (POST/PATCH/DELETE) without X-Council-Session must return 401."""
        payload = {"profile_ref": "test-p1", "label": "Test"}

        # Missing token
        resp = self.client.post("/api/accounts", json=payload)
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Unauthorized", resp.json()["detail"])

        # Invalid token
        resp = self.client.post("/api/accounts", json=payload, headers={"X-Council-Session": "invalid-token-xyz"})
        self.assertEqual(resp.status_code, 401)

        # Valid token succeeds
        resp = self.client.post(
            "/api/accounts",
            json=payload,
            headers={"X-Council-Session": self.session_token},
        )
        self.assertEqual(resp.status_code, 201)

    def test_idempotency_key_replay(self) -> None:
        """Identical mutating requests with the same Idempotency-Key return cached response without re-executing."""
        payload = {"profile_ref": "idemp-p1", "label": "Idempotent Account"}
        headers = {
            "X-Council-Session": self.session_token,
            "Idempotency-Key": "idemp-key-001",
        }

        # First request: creates account
        resp1 = self.client.post("/api/accounts", json=payload, headers=headers)
        self.assertEqual(resp1.status_code, 201)
        data1 = resp1.json()
        account_id = data1["account_id"]

        # Second request with identical payload and key: returns cached response
        resp2 = self.client.post("/api/accounts", json=payload, headers=headers)
        self.assertEqual(resp2.status_code, 201)
        self.assertEqual(resp2.headers.get("X-Idempotent-Replay"), "true")
        data2 = resp2.json()
        self.assertEqual(data2["account_id"], account_id)

    def test_idempotency_key_collision_different_payload(self) -> None:
        """Using the same Idempotency-Key with a different payload must return 409 Conflict."""
        headers = {
            "X-Council-Session": self.session_token,
            "Idempotency-Key": "idemp-key-collision-test",
        }

        # First request
        resp1 = self.client.post(
            "/api/accounts",
            json={"profile_ref": "prof-a", "label": "Account A"},
            headers=headers,
        )
        self.assertEqual(resp1.status_code, 201)

        # Second request with differing payload
        resp2 = self.client.post(
            "/api/accounts",
            json={"profile_ref": "prof-b", "label": "Account B"},
            headers=headers,
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertIn("collision", resp2.json()["detail"])


if __name__ == "__main__":
    unittest.main()
