from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agym.profiles import _default_data_root, _profile_lock

from .errors import IntegrationError, RUN_NOT_FOUND, INVALID_REQUEST
from .protocol import MAX_EVENT_BYTES, MAX_PAGE_EVENTS


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else _default_data_root() / "integration"
        self.runs_dir = self.root / "runs"
        self.events_dir = self.root / "events"
        self.leases_dir = self.root / "leases"
        self.lock_path = self.root / "locks" / "store.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        with _profile_lock(self.lock_path):
            yield

    def _path(self, folder: Path, identifier: str, suffix: str = ".json") -> Path:
        if not isinstance(identifier, str) or not identifier or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in identifier):
            raise IntegrationError(INVALID_REQUEST, "Invalid identifier")
        return folder / (identifier + suffix)

    @staticmethod
    def _atomic(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.parent.chmod(0o700)
        fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("invalid record")
            return data
        except FileNotFoundError:
            return None

    def get_instance_id(self) -> str:
        with self.locked():
            path = self.root / "instance.json"
            record = self._read(path)
            if record is None:
                record = {"instance_id": str(uuid.uuid4())}
                self._atomic(path, record)
            return record["instance_id"]

    def create_run(self, run: dict[str, Any]) -> None:
        with self.locked():
            path = self._path(self.runs_dir, run["run_id"])
            if path.exists():
                raise IntegrationError(INVALID_REQUEST, "Run already exists")
            self._atomic(path, run)

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._read(self._path(self.runs_dir, run_id))
        if run is None:
            raise IntegrationError(RUN_NOT_FOUND, "Run not found")
        return run

    def update_run(self, run_id: str, **changes: Any) -> dict[str, Any]:
        with self.locked():
            run = self.get_run(run_id)
            run.update(changes)
            self._atomic(self._path(self.runs_dir, run_id), run)
            return run

    def list_runs(self, **filters: str | None) -> list[dict[str, Any]]:
        result = []
        for path in self.runs_dir.glob("*.json"):
            run = self._read(path)
            if run is not None and all(value is None or run.get(key) == value or (key == "workspace_key" and run.get("workspace", {}).get("key") == value) for key, value in filters.items()):
                result.append(run)
        return sorted(result, key=lambda item: item["created_at"], reverse=True)

    def find_by_request(self, client_id: str, request_id: str) -> dict[str, Any] | None:
        return next(iter(self.list_runs(client_id=client_id, request_id=request_id)), None)

    def find_active_by_workspace(self, workspace_key: str, cwd: str | None = None) -> dict[str, Any] | None:
        for run in self.list_runs():
            if run["status"] not in {"succeeded", "failed", "stopped"} and (run.get("workspace_key") == workspace_key or (cwd and run.get("workspace_cwd") == cwd)):
                return run
        return None

    def append_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        with self.locked():
            run = self.get_run(run_id)
            event = dict(event, run_id=run_id, seq=run["last_seq"] + 1)
            encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(encoded) > MAX_EVENT_BYTES:
                raise IntegrationError(INVALID_REQUEST, "Event exceeds size limit")
            path = self._path(self.events_dir, run_id, ".ndjson")
            path.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                path.parent.chmod(0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            run["last_seq"] = event["seq"]
            self._atomic(self._path(self.runs_dir, run_id), run)
            return event

    def get_events(self, run_id: str, after: int = 0, limit: int = MAX_PAGE_EVENTS) -> list[dict[str, Any]]:
        self.get_run(run_id)
        if after < 0 or not 1 <= limit <= MAX_PAGE_EVENTS:
            raise IntegrationError(INVALID_REQUEST, "Invalid event cursor or limit")
        path = self._path(self.events_dir, run_id, ".ndjson")
        try:
            with path.open(encoding="utf-8") as handle:
                result = []
                for line in handle:
                    event = json.loads(line)
                    if event["seq"] > after:
                        result.append(event)
                        if len(result) == limit:
                            break
                return result
        except FileNotFoundError:
            return []

    def save_lease(self, lease: dict[str, Any]) -> None:
        with self.locked():
            self._atomic(self._path(self.leases_dir, lease["lease_id"]), lease)

    def get_lease(self, lease_id: str) -> dict[str, Any]:
        lease = self._read(self._path(self.leases_dir, lease_id))
        if lease is None:
            raise IntegrationError(RUN_NOT_FOUND, "Lease not found")
        return lease

    def active_lease(self, profile_id: str) -> dict[str, Any] | None:
        for path in self.leases_dir.glob("*.json"):
            lease = self._read(path)
            if lease and lease["profile_id"] == profile_id and lease["state"] == "active":
                return lease
        return None
