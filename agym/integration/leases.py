from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .errors import IntegrationError, NO_CAPACITY
from .store import Store


def create(store: Store, profile_id: str, run_id: str) -> dict:
    with store.locked():
        if store.active_lease(profile_id):
            raise IntegrationError(NO_CAPACITY, "Profile is busy", retryable=True)
        now = datetime.now(timezone.utc).isoformat()
        lease = {"lease_id": "lease-" + uuid.uuid4().hex, "profile_id": profile_id,
                 "run_id": run_id, "owning_run_id": run_id, "created_at": now,
                 "acquired_at": now, "heartbeat_at": now, "state": "active"}
        store.save_lease(lease)
        return lease


def release(store: Store, lease_id: str) -> dict:
    with store.locked():
        lease = store.get_lease(lease_id)
        if lease["state"] != "released":
            lease["state"] = "released"
            lease["released_at"] = datetime.now(timezone.utc).isoformat()
            store.save_lease(lease)
        return lease
