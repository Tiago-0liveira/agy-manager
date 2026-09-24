from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import Store


def emit(store: Store, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return store.append_event(run_id, {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    })


def page(store: Store, run_id: str, after: int = 0, limit: int = 200) -> dict[str, Any]:
    events = store.get_events(run_id, after, limit)
    snapshot = store.get_run(run_id)
    cursor = events[-1]["seq"] if events else after
    return {"run_id": run_id, "events": events, "next_cursor": cursor,
            "has_more": snapshot["last_seq"] > cursor, "oldest_retained_seq": 1,
            "snapshot": {key: value for key, value in snapshot.items()
                         if key not in {"payload_hash", "worker_pid", "child_pid"}}}
