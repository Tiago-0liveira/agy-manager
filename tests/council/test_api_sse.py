"""Tests for Server-Sent Events (SSE) streaming, broadcaster, and sequence replay."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agym.council.api.app import create_app
from agym.council.api.events import EventBroadcaster, format_sse_message, sse_event_stream
from agym.council.api.security import get_or_create_session_token
from agym.council.models import RunStatus, StageConfig, StageKind, WorkerConfig, WorkflowConfig, WorkflowInput
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.storage import create_run, get_connection, init_db, record_event


class TestApiSse(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        self.db_path = self.root_path / "test_council.db"
        init_db(self.db_path)
        self.conn = get_connection(self.db_path)
        self.provider = FakeProviderAdapter()
        self.app = create_app(db_path=self.db_path, provider_adapter=self.provider)
        self.client = TestClient(self.app)
        self.session_token = get_or_create_session_token()
        self.headers = {"X-Council-Session": self.session_token}

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # 1. Message Formatting Tests
    # -----------------------------------------------------------------------

    def test_format_sse_message_single_line(self) -> None:
        """format_sse_message produces valid W3C SSE frame for single-line payload."""
        msg = format_sse_message(event_id=42, event_type="stage.started", data={"stage": "analysis"})
        self.assertTrue(msg.startswith("id: 42\nevent: stage.started\ndata: "))
        self.assertTrue(msg.endswith("\n\n"))
        self.assertIn('"stage": "analysis"', msg)

    def test_format_sse_message_multiline(self) -> None:
        """Multi-line strings must prefix every line with 'data: '."""
        multiline_data = "line 1\nline 2\nline 3"
        msg = format_sse_message(event_id=1, event_type="output.chunk", data=multiline_data)
        lines = msg.split("\n")
        self.assertEqual(lines[0], "id: 1")
        self.assertEqual(lines[1], "event: output.chunk")
        self.assertEqual(lines[2], "data: line 1")
        self.assertEqual(lines[3], "data: line 2")
        self.assertEqual(lines[4], "data: line 3")

    # -----------------------------------------------------------------------
    # 2. In-Memory Broadcaster Tests
    # -----------------------------------------------------------------------

    def test_broadcaster_pub_sub(self) -> None:
        """EventBroadcaster distributes events to subscribed queues of matching run_id."""
        async def _run_test() -> None:
            broadcaster = EventBroadcaster()
            q1 = await broadcaster.subscribe("run-alpha")
            q2 = await broadcaster.subscribe("run-alpha")
            q_other = await broadcaster.subscribe("run-beta")

            event_data = {"type": "worker.completed", "sequence": 5}
            await broadcaster.broadcast("run-alpha", event_data)

            self.assertEqual(q1.qsize(), 1)
            self.assertEqual(q2.qsize(), 1)
            self.assertEqual(q_other.qsize(), 0)

            item = await q1.get()
            self.assertEqual(item["sequence"], 5)

            await broadcaster.unsubscribe("run-alpha", q1)
            await broadcaster.unsubscribe("run-alpha", q2)
            await broadcaster.unsubscribe("run-beta", q_other)

        asyncio.run(_run_test())

    # -----------------------------------------------------------------------
    # 3. Stream Generator & SQLite Replay Tests
    # -----------------------------------------------------------------------

    def _create_test_run(self) -> str:
        wf = WorkflowConfig(
            name="SSE Test Run",
            goal="Test event streaming",
            inputs=[WorkflowInput(id="brief", description="brief", required=True)],
            workers=[
                WorkerConfig(
                    id="w1",
                    name="Worker 1",
                    account_ref="p1",
                    model="m1",
                    role="analyst",
                    instructions="Do work",
                    task="Task",
                )
            ],
            stages=[
                StageConfig(
                    id="s1",
                    kind=StageKind.INDEPENDENT,
                    workers=["w1"],
                    instruction="Analyze",
                )
            ],
            final_stage="s1",
        )
        return create_run(self.conn, wf, stages=wf.stages, workers=wf.workers)

    def test_sse_event_stream_historical_replay(self) -> None:
        """sse_event_stream yields prior SQLite events matching run_id after last_sequence."""
        run_id = self._create_test_run()

        # Insert 3 events
        ev1 = record_event(self.conn, run_id=run_id, event_type="run.started", payload={"status": "RUNNING"})
        ev2 = record_event(self.conn, run_id=run_id, event_type="stage.started", stage_id="s1", payload={"stage": "s1"})
        ev3 = record_event(self.conn, run_id=run_id, event_type="stage.completed", stage_id="s1", payload={"status": "COMPLETED"})

        async def _consume_replay(last_seq: int) -> list[str]:
            results: list[str] = []
            gen = sse_event_stream(run_id=run_id, db_path=self.db_path, last_sequence=last_seq, heartbeat_interval=0.1)
            # Fetch replayed events without waiting for live tail
            try:
                # The generator yields historical events synchronously before waiting on queue
                async for chunk in gen:
                    results.append(chunk)
                    if len(results) >= (3 - last_seq):
                        break
            except Exception:
                pass
            return results

        # 1. Replay all from 0
        all_events = asyncio.run(_consume_replay(0))
        self.assertEqual(len(all_events), 3)
        self.assertIn("id: 1", all_events[0])
        self.assertIn("event: run.started", all_events[0])
        self.assertIn("id: 2", all_events[1])
        self.assertIn("event: stage.started", all_events[1])
        self.assertIn("id: 3", all_events[2])
        self.assertIn("event: stage.completed", all_events[2])

        # 2. Replay after sequence 2 (only sequence 3 should yield)
        partial_events = asyncio.run(_consume_replay(2))
        self.assertEqual(len(partial_events), 1)
        self.assertIn("id: 3", partial_events[0])
        self.assertIn("event: stage.completed", partial_events[0])

    # -----------------------------------------------------------------------
    # 4. SSE HTTP Endpoint Tests
    # -----------------------------------------------------------------------

    def test_sse_endpoint_nonexistent_run_404(self) -> None:
        """GET /api/runs/{run_id}/events returns 404 for unknown run."""
        resp = self.client.get("/api/runs/nonexistent-run-12345/events")
        self.assertEqual(resp.status_code, 404)

    def test_sse_endpoint_headers_and_replay(self) -> None:
        """GET /api/runs/{run_id}/events returns text/event-stream with proper cache controls and historical replay."""
        run_id = self._create_test_run()
        record_event(self.conn, run_id=run_id, event_type="run.ready", payload={})
        record_event(self.conn, run_id=run_id, event_type="stage.started", stage_id="s1", payload={})

        # Fetch with live=false to consume replay and terminate cleanly
        resp = self.client.get(f"/api/runs/{run_id}/events?live=false")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers["content-type"])
        self.assertIn("no-cache", resp.headers["cache-control"])
        self.assertIn("id: 1", resp.text)
        self.assertIn("event: run.ready", resp.text)
        self.assertIn("id: 2", resp.text)
        self.assertIn("event: stage.started", resp.text)

        # Test reconnection replay with Last-Event-ID header
        resp_reconnect = self.client.get(
            f"/api/runs/{run_id}/events?live=false",
            headers={"Last-Event-ID": "1"},
        )
        self.assertEqual(resp_reconnect.status_code, 200)
        self.assertNotIn("id: 1", resp_reconnect.text)
        self.assertIn("id: 2", resp_reconnect.text)
        self.assertIn("event: stage.started", resp_reconnect.text)

        # Test reconnection replay with last_sequence query param
        resp_query = self.client.get(f"/api/runs/{run_id}/events?live=false&last_sequence=1")
        self.assertEqual(resp_query.status_code, 200)
        self.assertNotIn("id: 1", resp_query.text)
        self.assertIn("id: 2", resp_query.text)


if __name__ == "__main__":
    unittest.main()
