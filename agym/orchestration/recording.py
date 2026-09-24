"""Versioned execution traces, independent of orchestration result snapshots.

One attempt is one call to a runner or coordinator session. Byte streams are
stored verbatim in private files; trace entries reference byte ranges. Readers
must treat captured model output as untrusted text, never terminal instructions.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator
import uuid

from agym.orchestration.contracts import ModelResult

current_attempt: ContextVar[AttemptCapture | None] = ContextVar("orchestration_attempt", default=None)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_trace(path: Path) -> Iterator[dict[str, Any]]:
    """Read complete entries, tolerating an incomplete final write after a crash.

    Malformed complete entries raise rather than silently hiding trace damage.
    File order is the ordering authority; timestamps are for presentation.
    """
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n"):
                return
            yield json.loads(line)


def append_trace(store: Any, run_id: str, event_type: str, payload: dict[str, Any],
                 attempt_id: str | None = None, event_id: str | None = None) -> None:
    """Serialize a timeline entry; preserve a torn tail before resuming appends."""
    with store._trace_lock:
        path = store.run_dir(run_id) / "trace.jsonl"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size:
                handle.seek(size - 1)
                if handle.read(1) != b"\n":
                    # Usually a single short entry; scan backward without loading the journal.
                    cursor = size
                    tail = b""
                    while cursor:
                        start = max(0, cursor - 65536)
                        handle.seek(start)
                        block = handle.read(cursor - start)
                        tail = block + tail
                        boundary = block.rfind(b"\n")
                        if boundary >= 0:
                            tail = tail[boundary + 1:]
                            cursor = start + boundary + 1
                            break
                        cursor = start
                    recovery_file = f"trace.partial-{uuid.uuid4().hex}.txt"
                    recovery_fd = os.open(path.parent / recovery_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(recovery_fd, "wb") as recovery_handle:
                        recovery_handle.write(tail)
                        recovery_handle.flush()
                        os.fsync(recovery_handle.fileno())
                    handle.seek(cursor)
                    handle.truncate()
                    recovery = dict(schema_version=1, event_id=uuid.uuid4().hex, timestamp=now(),
                                    run_id=str(run_id), attempt_id=None, type="trace_recovered",
                                    payload={"partial_file": recovery_file, "bytes": len(tail)})
                    handle.write((json.dumps(recovery) + "\n").encode())
            handle.seek(0, os.SEEK_END)
            entry = dict(schema_version=1, event_id=event_id or uuid.uuid4().hex, timestamp=now(),
                         run_id=str(run_id), attempt_id=attempt_id, type=event_type, payload=payload)
            handle.write((json.dumps(entry, ensure_ascii=True) + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())


class AttemptCapture:
    def __init__(self, store: Any, run_id: str, prompt: str, **metadata: Any) -> None:
        from agym.orchestration.persistence import atomic_write_json, atomic_write_text

        self.store = store
        self.run_id = str(run_id)
        self.attempt_id = f"attempt-{uuid.uuid4().hex}"
        self.directory = store.run_dir(run_id) / "attempts" / self.attempt_id
        self.directory.mkdir(parents=True, mode=0o700)
        self.directory.parent.chmod(0o700)
        self.directory.chmod(0o700)
        self._lock = threading.RLock()
        self.record = dict(metadata, schema_version=1, run_id=self.run_id,
                           attempt_id=self.attempt_id, status="RUNNING", started_at=now(),
                           prompt_file="prompt.txt", stdout_file="stdout.log", stderr_file="stderr.log")
        atomic_write_text(self.directory / "prompt.txt", prompt)
        atomic_write_json(self.directory / "record.json", self.record)
        self.note("attempt_started", **metadata)

    def note(self, event_type: str, **payload: Any) -> None:
        append_trace(self.store, self.run_id, event_type, payload, self.attempt_id)

    def write(self, stream: str, data: bytes) -> None:
        if stream not in ("stdout", "stderr"):
            raise ValueError(f"Unsupported stream: {stream}")
        if not data:
            return
        with self._lock:
            path = self.directory / f"{stream}.log"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as handle:
                offset = handle.tell()
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self.note("output", stream=stream, offset=offset, length=len(data))

    def finish(self, result: ModelResult | None = None, *, error: BaseException | None = None) -> None:
        from agym.orchestration.persistence import atomic_write_json

        with self._lock:
            if self.record["status"] != "RUNNING":
                return
            if result is not None:
                self.record.update(status=result.status.value, result=result.to_dict())
            else:
                interrupted = isinstance(error, (asyncio.CancelledError, KeyboardInterrupt))
                self.record.update(status="INTERRUPTED" if interrupted else "FAILED",
                                   error=str(error) or type(error).__name__)
            self.record["completed_at"] = now()
            atomic_write_json(self.directory / "record.json", self.record)
            self.note("attempt_finished", status=self.record["status"],
                      error=result.error if result else self.record.get("error"),
                      exit_code=result.exit_code if result else None)


@contextmanager
def capture_attempt(store: Any, run_id: str, *, prompt: str, **metadata: Any) -> Iterator[AttemptCapture | None]:
    # Optional extension: custom/in-memory stores need not implement tracing.
    factory = getattr(store, "start_attempt", None)
    capture = factory(run_id, prompt=prompt, **metadata) if callable(factory) else None
    token = current_attempt.set(capture)
    try:
        yield capture
    except BaseException as exc:
        if capture is not None:
            capture.finish(error=exc)
        raise
    finally:
        current_attempt.reset(token)
