from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import events, leases, profiles
from .errors import IntegrationError, IDEMPOTENCY_CONFLICT, INVALID_REQUEST, WORKSPACE_BUSY
from .store import Store

TERMINAL = {"succeeded", "failed", "stopped"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise IntegrationError(INVALID_REQUEST, "Request must be an object")
    required = ("request_id", "client", "client_id", "profile", "task")
    if any(not isinstance(raw.get(k), str) or not raw[k].strip() for k in required):
        raise IntegrationError(INVALID_REQUEST, "Missing or invalid run request field")
    workspace = raw.get("workspace")
    if not isinstance(workspace, dict) or any(not isinstance(workspace.get(k), str) or not workspace[k].strip() for k in ("key", "cwd")):
        raise IntegrationError(INVALID_REQUEST, "Workspace key and cwd are required")
    cwd = Path(workspace["cwd"])
    if not cwd.is_absolute() or not cwd.is_dir():
        raise IntegrationError(INVALID_REQUEST, "Workspace cwd must be an existing absolute directory")
    if raw.get("execution", "headless") != "headless" or raw.get("permission_policy", "profile-default") != "profile-default":
        raise IntegrationError(INVALID_REQUEST, "Unsupported execution or permission policy")
    if len(raw["task"]) > 100_000:
        raise IntegrationError(INVALID_REQUEST, "Task is too large")
    return {"request_id": raw["request_id"], "client": raw["client"], "client_id": raw["client_id"],
            "workspace": {"key": workspace["key"], "cwd": str(cwd.resolve()),
                          "repository_key": workspace.get("repository_key", "")},
            "profile": raw["profile"], "task": raw["task"],
            "execution": "headless", "permission_policy": "profile-default"}


def _hash(request: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _launch(store: Store, run_id: str) -> subprocess.Popen:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ)
    env["AGYM_DATA_HOME"] = str(store.root.parent)
    return subprocess.Popen([sys.executable, "-m", "agym.integration.worker", run_id],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, env=env, **kwargs)


def start(raw: Any, store: Store | None = None) -> dict[str, Any]:
    store = store or Store()
    request = validate_request(raw)
    digest = _hash(request)
    with store.locked():
        existing = store.find_by_request(request["client_id"], request["request_id"])
        if existing:
            if existing.get("payload_hash") != digest:
                raise IntegrationError(IDEMPOTENCY_CONFLICT, "Request ID already has different data")
            return existing
        workspace = request["workspace"]
        if store.find_active_by_workspace(workspace["key"], workspace["cwd"]):
            raise IntegrationError(WORKSPACE_BUSY, "Workspace has an active run", retryable=True)
        profile = profiles.select(request["profile"], store)
        run_id = "run-" + uuid.uuid4().hex
        stamp = now()
        run = {"run_id": run_id, "request_id": request["request_id"],
               "client": request["client"], "client_id": request["client_id"],
               "workspace": workspace, "workspace_key": workspace["key"],
               "workspace_cwd": workspace["cwd"], "repository_key": workspace["repository_key"],
               "requested_profile": request["profile"], "selected_profile": profile.name,
               "lease_id": "", "task": request["task"], "status": "starting",
               "session_id": None, "created_at": stamp, "started_at": None, "finished_at": None,
               "exit_code": None, "error": None, "last_seq": 0, "payload_hash": digest,
               "worker_pid": None, "child_pid": None}
        store.create_run(run)
        try:
            lease = leases.create(store, profile.name, run_id)
            run = store.update_run(run_id, lease_id=lease["lease_id"])
            proc = _launch(store, run_id)
            run = store.update_run(run_id, worker_pid=proc.pid)
        except Exception as exc:
            store.update_run(run_id, status="failed", finished_at=now(),
                             error={"code": "INTERNAL_ERROR", "message": "Run could not start", "retryable": True})
            if run.get("lease_id"):
                leases.release(store, run["lease_id"])
            raise IntegrationError("INTERNAL_ERROR", "Run could not start", retryable=True, run_id=run_id) from exc
        return run


def get(run_id: str, store: Store | None = None) -> dict[str, Any]:
    return (store or Store()).get_run(run_id)


def public(run: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in run.items()
            if key not in {"payload_hash", "worker_pid", "child_pid"}}


def list_runs(store: Store | None = None, **filters: str | None) -> list[dict[str, Any]]:
    return (store or Store()).list_runs(**filters)


def _terminate_child(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def stop(run_id: str, store: Store | None = None) -> dict[str, Any]:
    store = store or Store()
    with store.locked():
        run = store.get_run(run_id)
        if run["status"] in TERMINAL:
            return run
        if run["status"] != "stopping":
            run = store.update_run(run_id, status="stopping")
            events.emit(store, run_id, "status_changed", {"status": "stopping"})
        pid = run.get("child_pid")
    if pid:
        _terminate_child(pid)
    # The worker finalizes after observing the stop request. If it has not yet
    # launched a child, it will check the persisted state before doing so.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = store.get_run(run_id)
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.05)
    return store.get_run(run_id)
