"""Server-Sent Events (SSE) streaming with sequence IDs and Last-Event-ID reconnection."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncIterator

from agym.council.models import CouncilEvent
from agym.council.storage import get_connection, list_events_for_run


class EventBroadcaster:
    """In-memory pub/sub broadcaster for live Council events by run_id."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Register a new listener queue for a run's live events."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._subscribers[run_id].add(q)
        return q

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Unregister a listener queue."""
        async with self._lock:
            if run_id in self._subscribers:
                self._subscribers[run_id].discard(queue)
                if not self._subscribers[run_id]:
                    del self._subscribers[run_id]

    async def broadcast(self, run_id: str, event_data: dict[str, Any]) -> None:
        """Broadcast an event to all active listeners for a run."""
        async with self._lock:
            listeners = list(self._subscribers.get(run_id, []))
        for q in listeners:
            try:
                q.put_nowait(event_data)
            except Exception:
                pass


# Global singleton broadcaster
broadcaster = EventBroadcaster()


def _on_storage_event(ev: CouncilEvent) -> None:
    payload = ev.model_dump(mode="json")
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(broadcaster.broadcast(ev.run_id, payload))
    except RuntimeError:
        pass


from agym.council.storage import register_event_listener

register_event_listener(_on_storage_event)


def format_sse_message(
    event_id: str | int,
    event_type: str,
    data: dict[str, Any] | str,
) -> str:
    """Format an SSE message chunk according to W3C EventSource standard."""
    data_str = json.dumps(data) if isinstance(data, dict) else str(data)
    # Newlines within data must be prefixed with 'data: '
    data_lines = data_str.split("\n")
    data_payload = "\n".join(f"data: {line}" for line in data_lines)
    return f"id: {event_id}\nevent: {event_type}\n{data_payload}\n\n"


async def sse_event_stream(
    run_id: str,
    db_path: Path | str | None = None,
    last_sequence: int = 0,
    heartbeat_interval: float = 15.0,
    live: bool = True,
) -> AsyncIterator[str]:
    """Asynchronous generator yielding formatted SSE frames for a run.

    Replays historical events past last_sequence, then yields live events
    from the broadcaster and periodic keepalive comments if live is True.
    """
    current_seq = last_sequence

    # 1. Historical Replay from SQLite
    conn = get_connection(db_path)
    try:
        past_events = list_events_for_run(conn, run_id=run_id, after_sequence=current_seq, limit=1000)
        for row in past_events:
            seq = int(row["sequence"])
            ev_type = str(row["event_type"])
            payload_raw = row["payload_json"]
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except Exception:
                payload = {"raw": payload_raw}

            payload_with_meta = {
                "sequence": seq,
                "event_id": str(row["event_id"]),
                "run_id": str(row["run_id"]),
                "stage_id": row["stage_id"],
                "worker_id": row["worker_id"],
                "attempt_id": row["attempt_id"],
                "type": ev_type,
                "timestamp": str(row["timestamp"]),
                "payload": payload,
            }
            yield format_sse_message(event_id=seq, event_type=ev_type, data=payload_with_meta)
            if seq > current_seq:
                current_seq = seq
    finally:
        conn.close()

    if not live:
        return

    # 2. Live Tail Subscription
    queue = await broadcaster.subscribe(run_id)
    try:
        while True:
            try:
                # Wait for next live event or heartbeat timeout
                event_dict = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                seq = int(event_dict.get("sequence", current_seq + 1))
                if seq <= current_seq:
                    continue  # Deduplicate already replayed events
                current_seq = seq
                ev_type = str(event_dict.get("type", "event"))
                yield format_sse_message(event_id=seq, event_type=ev_type, data=event_dict)
            except asyncio.TimeoutError:
                # Heartbeat comment to keep HTTP connection alive through proxies
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        await broadcaster.unsubscribe(run_id, queue)
