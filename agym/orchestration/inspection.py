"""Orchestration run inspection and timeline observability.

Provides immutable views, robust error tolerance for corrupt or missing artifacts,
stream reading with byte cursors, and CLI formatters for `inspect` and `logs`.
Never acquires execution leases or launches coordinator/model processes.
"""

from __future__ import annotations

import argparse
import codecs
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterator, Sequence, TextIO

from agym.orchestration.contracts import (
    InvocationStatus,
    RunId,
    RunStatus,
)
from agym.orchestration.leases import is_pid_alive
from agym.orchestration.persistence import (
    FileRunStore,
    RunNotFoundError,
    get_default_runs_dir,
)
from agym.orchestration.ui import ROLE_DISPLAY_NAMES, format_duration

logger = logging.getLogger(__name__)

# Regular expression matching ANSI escape sequences
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\))"
)


# ============================================================================
# 1. Sanitization and Safety Helpers
# ============================================================================


def escape_control_codes(text: str) -> str:
    """Escapes ANSI sequences and unsafe control codes for terminal display.

    Preserves standard newlines (\\n) and tabs (\\t). Replaces carriage returns
    and control characters (< 32, 127) with safe visible representations so that
    captured text cannot corrupt terminal state or overwrite lines.
    """
    if not text:
        return ""

    def _replace_ansi(m: re.Match[str]) -> str:
        raw = m.group(0)
        return raw.replace("\x1b", "\\x1b")

    # Neutralize ANSI escape sequences
    sanitized = _ANSI_ESCAPE_RE.sub(_replace_ansi, text)

    out: list[str] = []
    for ch in sanitized:
        code = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif ch == "\r":
            out.append("\\r")
        elif code < 32 or code == 127:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def validate_safe_path(base_dir: Path, target_path: Path | str) -> Path:
    """Validate that target_path is located inside base_dir, preventing path traversal."""
    base_resolved = base_dir.resolve()
    target = Path(target_path)
    if not target.is_absolute():
        target_resolved = (base_resolved / target).resolve()
    else:
        target_resolved = target.resolve()
    try:
        target_resolved.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"Path '{target_path}' escapes run root '{base_dir}'")
    return target_resolved


def is_process_alive(pid: int | None) -> bool:
    """Check if process with given PID is alive on current system."""
    return is_pid_alive(pid)


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


# ============================================================================
# 2. Immutable Normalized Data Views
# ============================================================================


@dataclass(frozen=True)
class TimelineEntryView:
    """A single timeline or trace event."""

    schema_version: int
    event_id: str
    timestamp: str
    run_id: str
    attempt_id: str | None
    type: str
    payload: dict[str, Any]
    unsupported_version: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "type": self.type,
            "payload": self.payload,
        }
        if self.unsupported_version:
            d["unsupported_version"] = True
        return d


@dataclass(frozen=True)
class AttemptInspectionView:
    """Normalized view of one runner or coordinator execution attempt."""

    attempt_id: str
    run_id: str
    kind: str
    status: str
    model_status: str | None = None
    protocol_status: str | None = None
    protocol_error: str | None = None
    worker_id: str | None = None
    invocation_id: str | None = None
    role: str | None = None
    round_number: int | None = None
    attempt_number: int | None = None
    correction_attempt: int | None = None
    schema_name: str | None = None
    profile_name: str | None = None
    strategy: str | None = None
    workspace_mode: str | None = None
    timeout_seconds: float | None = None
    conversation_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    process_meta: dict[str, Any] | None = None
    prompt_file: str | None = None
    prompt_text: str | None = None
    stdout_file: str | None = None
    stderr_file: str | None = None
    model_response: str | None = None
    structured_data: Any | None = None
    available_streams: tuple[str, ...] = field(default_factory=tuple)
    stream_paths: dict[str, str] = field(default_factory=dict)
    schema_version: int = 1
    raw_record: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "model_status": self.model_status,
            "protocol_status": self.protocol_status,
            "protocol_error": self.protocol_error,
            "worker_id": self.worker_id,
            "invocation_id": self.invocation_id,
            "role": self.role,
            "round_number": self.round_number,
            "attempt_number": self.attempt_number,
            "correction_attempt": self.correction_attempt,
            "schema_name": self.schema_name,
            "profile_name": self.profile_name,
            "strategy": self.strategy,
            "workspace_mode": self.workspace_mode,
            "timeout_seconds": self.timeout_seconds,
            "conversation_id": self.conversation_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "error": self.error,
            "usage": self.usage,
            "process_meta": self.process_meta,
            "prompt_file": self.prompt_file,
            "prompt_text": self.prompt_text,
            "stdout_file": self.stdout_file,
            "stderr_file": self.stderr_file,
            "model_response": self.model_response,
            "structured_data": self.structured_data,
            "available_streams": list(self.available_streams),
            "stream_paths": self.stream_paths,
        }


@dataclass(frozen=True)
class WorkerInspectionView:
    """Normalized view of a logical worker and its attempts/retries."""

    worker_id: str
    role: str
    final_status: str
    attempt_count: int
    profiles: tuple[str, ...]
    duration_seconds: float | None
    error: str | None
    attempts: tuple[AttemptInspectionView, ...]
    invocation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "role": self.role,
            "final_status": self.final_status,
            "attempt_count": self.attempt_count,
            "profiles": list(self.profiles),
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "invocation_id": self.invocation_id,
            "attempts": [a.to_dict() for a in self.attempts],
        }


@dataclass(frozen=True)
class CoordinatorDecisionView:
    """Coordinator decision and rejections/corrections in a round."""

    round_number: int
    action_type: str
    action_id: str | None = None
    timestamp: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    rejections: tuple[str, ...] = field(default_factory=tuple)
    corrections: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_number": self.round_number,
            "action_type": self.action_type,
            "action_id": self.action_id,
            "timestamp": self.timestamp,
            "details": self.details,
            "rejections": list(self.rejections),
            "corrections": list(self.corrections),
        }


@dataclass(frozen=True)
class RunInspectionView:
    """Complete immutable inspection view of an orchestration run."""

    schema_version: int
    run_id: str
    run_dir: str
    task: str
    mode: str
    status: str
    started_at: str | None
    completed_at: str | None
    duration_seconds: float | None
    round_number: int
    budget_usage: dict[str, Any]
    last_activity: str | None
    workers: tuple[WorkerInspectionView, ...]
    coordinator_decisions: tuple[CoordinatorDecisionView, ...]
    failed_or_unfinished_attempts: tuple[AttemptInspectionView, ...]
    final_result: str | None
    data_availability_notes: tuple[str, ...]
    corrupt_artifacts: tuple[dict[str, str], ...]
    unsupported_schemas: tuple[dict[str, Any], ...]
    all_attempts: tuple[AttemptInspectionView, ...]
    timeline: tuple[TimelineEntryView, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "task": self.task,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "round_number": self.round_number,
            "budget_usage": self.budget_usage,
            "last_activity": self.last_activity,
            "workers": [w.to_dict() for w in self.workers],
            "coordinator_decisions": [c.to_dict() for c in self.coordinator_decisions],
            "failed_or_unfinished_attempts": [
                a.to_dict() for a in self.failed_or_unfinished_attempts
            ],
            "final_result": self.final_result,
            "data_availability_notes": list(self.data_availability_notes),
            "corrupt_artifacts": list(self.corrupt_artifacts),
            "unsupported_schemas": list(self.unsupported_schemas),
            "all_attempts": [a.to_dict() for a in self.all_attempts],
            "timeline": [t.to_dict() for t in self.timeline],
        }


# ============================================================================
# 3. Stream & Timeline Reading with Byte Cursors
# ============================================================================


class StreamBuffer:
    """Incremental UTF-8 and NDJSON decoder holding partial records across reads."""

    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.line_buffer: str = ""

    def decode_chunk(self, raw_bytes: bytes, final: bool = False) -> str:
        """Decode raw bytes incrementally into UTF-8 text without forcing final flush."""
        return self.decoder.decode(raw_bytes, final=final)

    def feed_for_lines(self, raw_bytes: bytes, final: bool = False) -> list[str]:
        """Feed bytes, decode incrementally, and return complete lines ending in newline."""
        text = self.decode_chunk(raw_bytes, final=final)
        self.line_buffer += text
        lines: list[str] = []
        while "\n" in self.line_buffer:
            line, self.line_buffer = self.line_buffer.split("\n", 1)
            lines.append(line)
        if final and self.line_buffer:
            lines.append(self.line_buffer)
            self.line_buffer = ""
        return lines

    def flush(self) -> list[str]:
        """Flush remaining buffered text upon stream/attempt termination."""
        lines: list[str] = []
        tail = self.decoder.decode(b"", final=True)
        self.line_buffer += tail
        if self.line_buffer:
            lines.append(self.line_buffer)
            self.line_buffer = ""
        return lines


def read_timeline_entries(
    path: Path,
    cursor: int = 0,
    max_bytes: int | None = None,
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]]]:
    """Read complete NDJSON trace entries from a byte cursor.

    Returns: (entries, new_cursor, corrupt_records)
    Advances the cursor only past complete records ending in newline.
    If the file size has shrunk below cursor, resets cursor to 0 and reads anew.
    """
    if not path.exists():
        return [], cursor, []

    try:
        file_size = path.stat().st_size
    except OSError:
        return [], cursor, []

    if file_size < cursor:
        cursor = 0

    entries: list[dict[str, Any]] = []
    corrupt: list[dict[str, str]] = []

    try:
        with path.open("rb") as handle:
            handle.seek(cursor)
            chunk = handle.read(max_bytes) if max_bytes is not None else handle.read()
            if not chunk:
                return entries, cursor, corrupt

            last_newline = chunk.rfind(b"\n")
            if last_newline < 0:
                # No complete record encountered
                return entries, cursor, corrupt

            complete_bytes = chunk[: last_newline + 1]
            new_cursor = cursor + len(complete_bytes)

            lines = complete_bytes.split(b"\n")
            for line in lines[:-1]:
                if not line.strip():
                    continue
                try:
                    decoded = line.decode("utf-8")
                    entry = json.loads(decoded)
                    schema_ver = entry.get("schema_version")
                    if schema_ver is not None and schema_ver != 1:
                        entry["_unsupported_schema"] = True
                    entries.append(entry)
                except Exception as exc:
                    corrupt.append(
                        {
                            "line": line.decode("utf-8", errors="replace"),
                            "error": str(exc),
                        }
                    )

            return entries, new_cursor, corrupt
    except OSError as exc:
        corrupt.append({"path": str(path), "error": str(exc)})
        return entries, cursor, corrupt


def read_stream_raw_chunk(stream_path: Path, offset: int, length: int) -> bytes:
    """Read raw byte range from a stream log."""
    if not stream_path.exists() or length <= 0:
        return b""
    try:
        with stream_path.open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)
    except OSError:
        return b""


def read_stream_chunk(stream_path: Path, offset: int, length: int) -> str:
    """Read byte range from a stream log and decode as UTF-8."""
    raw = read_stream_raw_chunk(stream_path, offset, length)
    if not raw:
        return ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(raw, final=True)


# ============================================================================
# 4. Comprehensive Run Inspection Builder
# ============================================================================


def inspect_run(run_dir: Path | str, run_id: str | None = None) -> RunInspectionView:
    """Assemble an immutable RunInspectionView from filesystem artifacts alone.

    Loads artifacts independently; missing or corrupted artifacts are recorded
    without dropping healthy data. Does not acquire locks or spawn processes.
    """
    run_path = Path(run_dir).resolve()
    if not run_path.exists() or not run_path.is_dir():
        rid = run_id or run_path.name
        raise RunNotFoundError(f"Run directory not found: {run_path}")

    actual_run_id = str(run_id or run_path.name)
    corrupt_artifacts: list[dict[str, str]] = []
    unsupported_schemas: list[dict[str, Any]] = []
    notes: list[str] = []

    # 1. Load run.json
    run_data: dict[str, Any] = {}
    run_json = run_path / "run.json"
    if run_json.exists():
        try:
            run_data = json.loads(run_json.read_text(encoding="utf-8"))
            if run_data.get("schema_version") not in (None, 1):
                unsupported_schemas.append(
                    {"artifact": "run.json", "version": run_data.get("schema_version")}
                )
        except Exception as exc:
            corrupt_artifacts.append({"path": str(run_json), "error": str(exc)})

    # Fallback to task.txt if task not in run.json
    task = run_data.get("task")
    if not task:
        task_txt = run_path / "task.txt"
        if task_txt.exists():
            try:
                task = task_txt.read_text(encoding="utf-8").strip()
            except Exception as exc:
                corrupt_artifacts.append({"path": str(task_txt), "error": str(exc)})
    task = task or "unknown task"
    mode = run_data.get("mode") or "plan"
    run_status_raw = run_data.get("status") or "UNKNOWN"

    started_at = run_data.get("started_at")
    completed_at = run_data.get("completed_at")
    round_number = run_data.get("round_number", 0)
    budget_usage = run_data.get(
        "budget_usage",
        {"invocations": 0, "runtime_seconds": 0.0, "total_cost_usd": 0.0},
    )

    # 2. Assessment
    assessment_json = run_path / "assessment.json"
    if assessment_json.exists():
        try:
            json.loads(assessment_json.read_text(encoding="utf-8"))
        except Exception as exc:
            corrupt_artifacts.append({"path": str(assessment_json), "error": str(exc)})

    # 3. Final result
    final_result = run_data.get("final_result")
    final_json = run_path / "final.json"
    if final_json.exists():
        try:
            final_data = json.loads(final_json.read_text(encoding="utf-8"))
            if not final_result:
                final_result = final_data.get("summary") or final_data.get(
                    "final_result"
                )
        except Exception as exc:
            corrupt_artifacts.append({"path": str(final_json), "error": str(exc)})

    # 4. Coordinator info
    coordinator_json = run_path / "coordinator.json"
    if coordinator_json.exists():
        try:
            json.loads(coordinator_json.read_text(encoding="utf-8"))
        except Exception as exc:
            corrupt_artifacts.append(
                {"path": str(coordinator_json), "error": str(exc)}
            )

    # 5. Read attempts/ directory first to populate attempt metadata
    attempts_dir = run_path / "attempts"
    raw_attempt_records: dict[str, dict[str, Any]] = {}
    attempt_prompt_paths: dict[str, Path] = {}
    attempt_prompt_texts: dict[str, str | None] = {}
    attempt_stream_paths: dict[str, dict[str, str]] = {}
    attempt_avail_streams: dict[str, list[str]] = {}

    if attempts_dir.exists() and attempts_dir.is_dir():
        try:
            attempt_subdirs = sorted(
                [p for p in attempts_dir.iterdir() if p.is_dir()], key=lambda p: p.name
            )
        except OSError as exc:
            corrupt_artifacts.append({"path": str(attempts_dir), "error": str(exc)})
            attempt_subdirs = []

        for adir in attempt_subdirs:
            try:
                validate_safe_path(run_path, adir)
            except ValueError as val_err:
                corrupt_artifacts.append({"path": str(adir), "error": str(val_err)})
                continue

            record_file = adir / "record.json"
            if not record_file.exists():
                corrupt_artifacts.append(
                    {
                        "path": str(record_file),
                        "error": "Missing record.json in attempt directory",
                    }
                )
                continue

            try:
                record = json.loads(record_file.read_text(encoding="utf-8"))
            except Exception as exc:
                corrupt_artifacts.append(
                    {"path": str(record_file), "error": f"Invalid JSON: {exc}"}
                )
                continue

            aid = record.get("attempt_id") or adir.name
            raw_attempt_records[aid] = record

            # Check prompt file
            prompt_text: str | None = None
            prompt_file_name = record.get("prompt_file", "prompt.txt")
            prompt_path = adir / prompt_file_name
            if prompt_path.exists():
                try:
                    validate_safe_path(run_path, prompt_path)
                    prompt_text = prompt_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    attempt_prompt_paths[aid] = prompt_path
                    attempt_prompt_texts[aid] = prompt_text
                except Exception as exc:
                    corrupt_artifacts.append(
                        {"path": str(prompt_path), "error": str(exc)}
                    )

            # Check streams
            available_streams: list[str] = []
            stream_paths: dict[str, str] = {}
            for stream_name in ("stdout", "stderr"):
                sf_name = record.get(f"{stream_name}_file", f"{stream_name}.log")
                sf_path = adir / sf_name
                if sf_path.exists():
                    try:
                        validate_safe_path(run_path, sf_path)
                        size = sf_path.stat().st_size
                        if size > 0:
                            available_streams.append(stream_name)
                        stream_paths[stream_name] = str(sf_path)
                    except Exception as exc:
                        corrupt_artifacts.append(
                            {"path": str(sf_path), "error": str(exc)}
                        )
            attempt_stream_paths[aid] = stream_paths
            attempt_avail_streams[aid] = available_streams
    else:
        notes.append("attempt history unavailable")
        notes.append("raw streams unavailable")

    # 6. Read trace.jsonl or fallback to events.jsonl
    trace_path = run_path / "trace.jsonl"
    events_path = run_path / "events.jsonl"
    raw_timeline: list[dict[str, Any]] = []
    is_legacy = False

    if trace_path.exists() and trace_path.stat().st_size > 0:
        raw_timeline, _, trace_corrupt = read_timeline_entries(trace_path)
        for c in trace_corrupt:
            corrupt_artifacts.append(
                {"path": str(trace_path), "error": f"Corrupt line: {c.get('error')}"}
            )
    elif events_path.exists() and events_path.stat().st_size > 0:
        is_legacy = True
        try:
            with events_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        raw_timeline.append(
                            {
                                "schema_version": 1,
                                "event_id": ev.get("event_id"),
                                "timestamp": ev.get("timestamp"),
                                "run_id": ev.get("run_id", actual_run_id),
                                "attempt_id": None,
                                "type": "orchestration",
                                "payload": ev,
                            }
                        )
                    except Exception as line_exc:
                        corrupt_artifacts.append(
                            {
                                "path": str(events_path),
                                "error": f"Malformed line: {line_exc}",
                            }
                        )
        except Exception as exc:
            corrupt_artifacts.append({"path": str(events_path), "error": str(exc)})
    else:
        notes.append("no timeline trace available")

    # Reconcile terminal run status from timeline
    for entry in raw_timeline:
        etype = entry.get("type", "")
        payload = entry.get("payload", {})
        if etype in ("run_completed", "run_failed", "run_interrupted"):
            run_status_raw = etype.split("_")[1].upper()
            if not completed_at:
                completed_at = entry.get("timestamp")
            if not final_result:
                final_result = payload.get("summary") or payload.get("final_result")
        elif etype == "orchestration":
            o_type = payload.get("event_type") or payload.get("type") or ""
            o_inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            o_payload = o_inner if o_inner else payload
            if o_type in ("RUN_COMPLETED", "run_completed"):
                run_status_raw = "COMPLETED"
                if not completed_at:
                    completed_at = payload.get("timestamp") or entry.get("timestamp")
                if not final_result:
                    final_result = (
                        o_payload.get("summary")
                        or o_payload.get("final_result")
                        or payload.get("summary")
                    )
            elif o_type in ("RUN_FAILED", "run_failed"):
                run_status_raw = "FAILED"
                if not completed_at:
                    completed_at = payload.get("timestamp") or entry.get("timestamp")
            elif o_type in ("RUN_INTERRUPTED", "run_interrupted"):
                run_status_raw = "INTERRUPTED"
                if not completed_at:
                    completed_at = payload.get("timestamp") or entry.get("timestamp")

    is_run_terminal = run_status_raw in ("COMPLETED", "FAILED", "INTERRUPTED")

    # Extract process metadata, protocol statuses, and coordinator decisions from timeline
    attempt_process_meta: dict[str, dict[str, Any]] = {}
    attempt_protocol_status: dict[str, dict[str, Any]] = {}
    coordinator_decisions_by_round: dict[int, dict[str, Any]] = {}
    rejections_by_round: dict[int, list[str]] = {}
    corrections_by_round: dict[int, list[str]] = {}

    for entry in raw_timeline:
        aid = entry.get("attempt_id")
        etype = entry.get("type")
        payload = entry.get("payload", {})

        if etype in ("process_started", "process_attached") and aid:
            attempt_process_meta[aid] = {
                "pid": payload.get("pid"),
                "argv": payload.get("argv"),
                "cwd": payload.get("cwd"),
                "output_format": payload.get("output_format"),
            }
        elif etype == "protocol_accepted" and aid:
            attempt_protocol_status[aid] = {
                "status": "ACCEPTED",
                "schema": payload.get("schema_name"),
                "error": None,
            }
        elif etype == "protocol_rejected" and aid:
            attempt_protocol_status[aid] = {
                "status": "REJECTED",
                "schema": payload.get("schema_name"),
                "error": payload.get("error"),
            }
            rnd = payload.get("round_number")
            if rnd is None:
                rnd = raw_attempt_records.get(aid, {}).get("round_number", 0)
            if rnd is not None:
                corrections_by_round.setdefault(int(rnd), []).append(
                    f"Schema rejected ({payload.get('schema_name')}): {payload.get('error')}"
                )
        elif etype == "orchestration":
            o_type = payload.get("event_type") or payload.get("type") or ""
            o_inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            o_payload = o_inner if o_inner else payload
            rnd = o_payload.get("round_number") if o_payload.get("round_number") is not None else payload.get("round_number")
            if rnd is None and aid:
                rnd = raw_attempt_records.get(aid, {}).get("round_number")
            rnd = int(rnd) if rnd is not None else round_number

            if o_type in ("ROUND_STARTED", "round_started"):
                round_number = max(round_number, rnd)
            elif o_type in (
                "ACTION_REQUESTED",
                "action_requested",
                "ACTION_DECIDED",
                "action_decided",
            ):
                act_data = o_payload.get("action") if isinstance(o_payload.get("action"), dict) else {}
                action_kind = (
                    act_data.get("kind")
                    or act_data.get("action_type")
                    or o_payload.get("action_type")
                    or o_payload.get("action", "UNKNOWN")
                )
                action_id = act_data.get("action_id") or o_payload.get("action_id")
                details = act_data or o_payload
                coordinator_decisions_by_round[rnd] = {
                    "action_type": action_kind,
                    "action_id": action_id,
                    "timestamp": payload.get("timestamp") or entry.get("timestamp"),
                    "details": details,
                }
            elif o_type in ("ACTION_REJECTED", "action_rejected"):
                reason = (
                    o_payload.get("reason")
                    or o_payload.get("error")
                    or "Action rejected"
                )
                rejections_by_round.setdefault(rnd, []).append(reason)

    # 7. Build AttemptInspectionViews from raw attempt records
    attempts_by_id: dict[str, AttemptInspectionView] = {}
    for aid, record in raw_attempt_records.items():
        kind = record.get("kind") or "worker"
        raw_status = record.get("status") or "UNKNOWN"

        proc_meta = attempt_process_meta.get(aid)
        pid = proc_meta.get("pid") if proc_meta else None
        effective_status = raw_status

        if raw_status == "RUNNING":
            if is_run_terminal:
                effective_status = "UNFINISHED (run terminated)"
            elif pid is not None and not is_process_alive(pid):
                effective_status = "UNFINISHED (process dead)"

        # Separate model outcome from protocol outcome
        res = record.get("result", {})
        model_status = res.get("status") or (
            raw_status if raw_status != "RUNNING" else None
        )
        model_response = res.get("response")
        structured_data = res.get("structured_data")
        exit_code = res.get("exit_code") if res.get("exit_code") is not None else record.get("exit_code")
        error_msg = res.get("error") if res.get("error") is not None else record.get("error")
        if not error_msg and effective_status.startswith("UNFINISHED"):
            error_msg = f"Attempt unfinished: {effective_status}"
        usage = res.get("usage")

        prot_info = attempt_protocol_status.get(aid, {})
        protocol_status = prot_info.get("status")
        protocol_err = prot_info.get("error")

        att_started = record.get("started_at")
        att_completed = record.get("completed_at")
        att_duration: float | None = None
        st_dt = _parse_timestamp(att_started)
        et_dt = _parse_timestamp(att_completed)
        if st_dt and et_dt:
            att_duration = max(0.0, (et_dt - st_dt).total_seconds())
        elif st_dt and effective_status == "RUNNING":
            now_dt = datetime.now(timezone.utc)
            if st_dt.tzinfo is None:
                st_dt = st_dt.replace(tzinfo=timezone.utc)
            att_duration = max(0.0, (now_dt - st_dt).total_seconds())

        prompt_path = attempt_prompt_paths.get(aid)
        stream_paths = attempt_stream_paths.get(aid, {})
        available_streams = attempt_avail_streams.get(aid, [])

        attempt_view = AttemptInspectionView(
            schema_version=record.get("schema_version", 1),
            attempt_id=aid,
            run_id=actual_run_id,
            kind=kind,
            status=effective_status,
            model_status=model_status,
            protocol_status=protocol_status,
            protocol_error=protocol_err,
            worker_id=record.get("worker_id"),
            invocation_id=record.get("invocation_id"),
            role=record.get("role"),
            round_number=record.get("round_number"),
            attempt_number=record.get("attempt_number"),
            correction_attempt=record.get("correction_attempt"),
            schema_name=record.get("schema_name"),
            profile_name=record.get("profile_name"),
            strategy=record.get("strategy"),
            workspace_mode=record.get("workspace_mode"),
            timeout_seconds=record.get("timeout_seconds"),
            conversation_id=record.get("conversation_id"),
            started_at=att_started,
            completed_at=att_completed,
            duration_seconds=att_duration,
            exit_code=exit_code,
            error=error_msg,
            usage=usage,
            process_meta=proc_meta,
            prompt_file=str(prompt_path) if prompt_path else None,
            prompt_text=attempt_prompt_texts.get(aid),
            stdout_file=stream_paths.get("stdout"),
            stderr_file=stream_paths.get("stderr"),
            model_response=model_response,
            structured_data=structured_data,
            available_streams=tuple(available_streams),
            stream_paths=stream_paths,
            raw_record=record,
        )
        attempts_by_id[aid] = attempt_view
    else:
        # Attempts directory not present or empty
        notes.append("attempt history unavailable")
        notes.append("raw streams unavailable")

    # 7. Fallback to invocations/ snapshots if no attempt records or legacy run
    invocations_dir = run_path / "invocations"
    invocation_records: dict[str, dict[str, Any]] = {}
    if invocations_dir.exists() and invocations_dir.is_dir():
        for ifile in sorted(invocations_dir.glob("*.json")):
            try:
                validate_safe_path(run_path, ifile)
                inv_data = json.loads(ifile.read_text(encoding="utf-8"))
                iid = inv_data.get("invocation_id") or ifile.stem
                invocation_records[iid] = inv_data
            except Exception as exc:
                corrupt_artifacts.append({"path": str(ifile), "error": str(exc)})

    # If attempts are empty but invocation records exist, create surrogate attempt views
    if not attempts_by_id and invocation_records:
        for iid, inv in invocation_records.items():
            wid = inv.get("worker_id", "worker")
            surrogate_aid = f"legacy-{iid}"
            surrogate_att = AttemptInspectionView(
                schema_version=1,
                attempt_id=surrogate_aid,
                run_id=actual_run_id,
                kind="worker",
                status=inv.get("status", "UNKNOWN"),
                model_status=inv.get("status"),
                worker_id=wid,
                invocation_id=iid,
                role=inv.get("role"),
                started_at=inv.get("started_at"),
                completed_at=inv.get("completed_at"),
                duration_seconds=inv.get("duration_seconds"),
                error=inv.get("error"),
                exit_code=inv.get("exit_code"),
                usage=inv.get("usage"),
                raw_record=inv,
            )
            attempts_by_id[surrogate_aid] = surrogate_att

    all_attempts_list = list(attempts_by_id.values())

    # Check usage availability across all attempts & invocations
    has_usage = any(
        a.usage is not None
        for a in all_attempts_list
        if a.usage and any(v for v in a.usage.values())
    )
    if not has_usage:
        notes.append("usage unknown")

    # 8. Group worker attempts & build WorkerInspectionViews
    workers_map: dict[str, list[AttemptInspectionView]] = {}
    for att in all_attempts_list:
        if att.kind == "worker" and att.worker_id:
            workers_map.setdefault(att.worker_id, []).append(att)

    # Also check invocation records for any workers that had no attempt entries
    for iid, inv in invocation_records.items():
        wid = inv.get("worker_id")
        if wid and wid not in workers_map:
            # Create synthetic attempt view if needed
            surrogate_aid = f"legacy-{iid}"
            if surrogate_aid in attempts_by_id:
                workers_map.setdefault(wid, []).append(attempts_by_id[surrogate_aid])

    worker_views: list[WorkerInspectionView] = []
    for wid, atts in sorted(workers_map.items()):
        # Sort attempts by attempt_number or started_at
        sorted_atts = sorted(
            atts,
            key=lambda a: (
                a.attempt_number if a.attempt_number is not None else 0,
                a.started_at or "",
            ),
        )
        last_att = sorted_atts[-1]
        role = last_att.role or "general"
        final_st = last_att.status
        profiles = tuple(
            dict.fromkeys(
                a.profile_name
                for a in sorted_atts
                if a.profile_name and a.profile_name != "unknown"
            )
        )
        total_dur = sum((a.duration_seconds or 0.0) for a in sorted_atts)
        error = last_att.error
        inv_id = last_att.invocation_id

        # If last attempt didn't report error, look for failure error
        if not error and (final_st in ("FAILED", "INTERRUPTED") or final_st.startswith("UNFINISHED")):
            for a in reversed(sorted_atts):
                if a.error:
                    error = a.error
                    break
            if not error and final_st.startswith("UNFINISHED"):
                error = f"Worker unfinished: {final_st}"

        worker_views.append(
            WorkerInspectionView(
                worker_id=wid,
                role=role,
                final_status=final_st,
                attempt_count=len(sorted_atts),
                profiles=profiles,
                duration_seconds=total_dur if total_dur > 0 else None,
                error=error,
                attempts=tuple(sorted_atts),
                invocation_id=inv_id,
            )
        )

    # 9. Failed or unfinished attempts
    failed_or_unfinished: list[AttemptInspectionView] = []
    for att in all_attempts_list:
        if (
            "FAILED" in att.status
            or "INTERRUPTED" in att.status
            or "UNFINISHED" in att.status
            or att.protocol_status == "REJECTED"
            or (att.status == "RUNNING" and is_run_terminal)
        ):
            failed_or_unfinished.append(att)

    # 10. Coordinator decisions
    coord_views: list[CoordinatorDecisionView] = []
    all_rounds = sorted(
        set(coordinator_decisions_by_round.keys())
        | set(rejections_by_round.keys())
        | set(corrections_by_round.keys())
    )
    for rnd in all_rounds:
        dec = coordinator_decisions_by_round.get(rnd, {})
        action_type = dec.get("action_type", "UNKNOWN")
        action_id = dec.get("action_id")
        ts = dec.get("timestamp")
        details = dec.get("details", {})
        rejections = tuple(rejections_by_round.get(rnd, []))
        corrections = tuple(corrections_by_round.get(rnd, []))
        coord_views.append(
            CoordinatorDecisionView(
                round_number=rnd,
                action_type=action_type,
                action_id=action_id,
                timestamp=ts,
                details=details,
                rejections=rejections,
                corrections=corrections,
            )
        )

    # 11. Timeline views
    timeline_views = [
        TimelineEntryView(
            schema_version=e.get("schema_version", 1),
            event_id=str(e.get("event_id", "")),
            timestamp=str(e.get("timestamp", "")),
            run_id=actual_run_id,
            attempt_id=e.get("attempt_id"),
            type=str(e.get("type", "")),
            payload=e.get("payload", {}),
            unsupported_version=bool(e.get("_unsupported_schema", False)),
        )
        for e in raw_timeline
    ]

    # Calculate run elapsed time
    run_duration: float | None = None
    r_st = _parse_timestamp(started_at)
    r_et = _parse_timestamp(completed_at)
    if r_st and r_et:
        run_duration = max(0.0, (r_et - r_st).total_seconds())
    elif r_st:
        now_dt = datetime.now(timezone.utc)
        if r_st.tzinfo is None:
            r_st = r_st.replace(tzinfo=timezone.utc)
        run_duration = max(0.0, (now_dt - r_st).total_seconds())

    # Raw streams availability check
    has_streams = any(len(a.available_streams) > 0 for a in all_attempts_list)
    if not has_streams and "raw streams unavailable" not in notes:
        notes.append("raw streams unavailable")

    # Last recorded activity
    last_activity: str | None = None
    if raw_timeline:
        last_ev = raw_timeline[-1]
        last_activity = f"{last_ev.get('timestamp')} ({last_ev.get('type')})"
    elif all_attempts_list:
        last_att = all_attempts_list[-1]
        last_activity = f"{last_att.completed_at or last_att.started_at} ({last_att.kind} attempt)"

    return RunInspectionView(
        schema_version=1,
        run_id=actual_run_id,
        run_dir=str(run_path),
        task=task,
        mode=mode,
        status=run_status_raw,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=run_duration,
        round_number=round_number,
        budget_usage=budget_usage,
        last_activity=last_activity,
        workers=tuple(worker_views),
        coordinator_decisions=tuple(coord_views),
        failed_or_unfinished_attempts=tuple(failed_or_unfinished),
        final_result=final_result,
        data_availability_notes=tuple(dict.fromkeys(notes)),
        corrupt_artifacts=tuple(corrupt_artifacts),
        unsupported_schemas=tuple(unsupported_schemas),
        all_attempts=tuple(all_attempts_list),
        timeline=tuple(timeline_views),
    )


# ============================================================================
# 5. Presentation & Text Formatting (Inspect CLI)
# ============================================================================


def format_inspect_text(
    view: RunInspectionView,
    worker_id: str | None = None,
    attempt_id: str | None = None,
    show_timeline: bool = False,
) -> str:
    """Format RunInspectionView into the 5-part terminal display per specification."""
    lines: list[str] = []

    # If single attempt requested
    if attempt_id:
        att = next((a for a in view.all_attempts if a.attempt_id == attempt_id), None)
        if not att:
            return f"Error: attempt '{attempt_id}' not found in run '{view.run_id}'"
        return _format_attempt_detail(att)

    # If single worker requested
    if worker_id:
        w_view = next((w for w in view.workers if w.worker_id == worker_id), None)
        if not w_view:
            return f"Error: worker '{worker_id}' not found in run '{view.run_id}'"
        return _format_worker_detail(w_view)

    # 1. Overview header
    lines.append(f"=== Run Inspection: {view.run_id} ===")
    lines.append(f"Status:      {view.status}")
    lines.append(f"Task:        {escape_control_codes(view.task)}")
    lines.append(f"Mode:        {view.mode.upper()}")

    elapsed_str = (
        format_duration(view.duration_seconds)
        if view.duration_seconds is not None
        else "unknown"
    )
    lines.append(
        f"Elapsed:     {elapsed_str} (Started: {view.started_at or '-'}, Completed: {view.completed_at or '-'})"
    )

    inv_count = view.budget_usage.get("invocations", 0)
    rt_sec = view.budget_usage.get("runtime_seconds", 0.0)
    cost = view.budget_usage.get("total_cost_usd", 0.0)
    lines.append(
        f"Budget:      Invocations: {inv_count}, Runtime: {format_duration(rt_sec)}, Cost: ${cost:.4f}"
    )
    lines.append(f"Last Activity: {view.last_activity or 'None'}")
    lines.append("")

    # 2. Worker Table
    lines.append("--- Workers ---")
    if not view.workers:
        lines.append("  (No workers recorded)")
    else:
        hdr = f"{'Worker ID':<14} {'Role':<14} {'Status':<12} {'Attempts':<10} {'Profile(s)':<18} {'Duration':<10} {'Error'}"
        lines.append(hdr)
        lines.append("-" * min(80, len(hdr) + 15))
        for w in view.workers:
            role_disp = ROLE_DISPLAY_NAMES.get(w.role.upper(), w.role.lower())
            profs = ", ".join(w.profiles) if w.profiles else "-"
            dur = (
                format_duration(w.duration_seconds)
                if w.duration_seconds is not None
                else "-"
            )
            err = escape_control_codes(w.error or "-")
            if len(err) > 40:
                err = err[:37] + "..."
            lines.append(
                f"{w.worker_id:<14} {role_disp:<14} {w.final_status:<12} {w.attempt_count:<10} {profs:<18} {dur:<10} {err}"
            )
    lines.append("")

    # 3. Coordinator Decisions by Round
    lines.append("--- Coordinator Decisions ---")
    if not view.coordinator_decisions:
        lines.append("  (No coordinator decisions recorded)")
    else:
        for cd in view.coordinator_decisions:
            lines.append(f"Round {cd.round_number}:")
            if cd.rejections:
                lines.append("  Action Rejections:")
                for rej in cd.rejections:
                    lines.append(f"    - {escape_control_codes(rej)}")
            if cd.corrections:
                lines.append("  Protocol Corrections:")
                for corr in cd.corrections:
                    lines.append(f"    - {escape_control_codes(corr)}")
            lines.append(f"  Decision: {cd.action_type}")
            if cd.details:
                workers = cd.details.get("workers") or cd.details.get(
                    "worker_invocations"
                )
                if workers:
                    w_names = [str(w.get("worker_id", w)) if isinstance(w, dict) else str(w) for w in workers]
                    lines.append(f"    Workers: {', '.join(w_names)}")
    lines.append("")

    # 4. Failed / Unfinished Attempts
    lines.append("--- Failed / Unfinished Attempts ---")
    if not view.failed_or_unfinished_attempts:
        lines.append("  (None)")
    else:
        for fa in view.failed_or_unfinished_attempts:
            wid_str = f"worker: {fa.worker_id}, " if fa.worker_id else ""
            inv_str = f"invocation: {fa.invocation_id}" if fa.invocation_id else ""
            lines.append(f"Attempt: {fa.attempt_id} ({wid_str}{inv_str})")
            lines.append(f"  Status:  {fa.status}")
            if fa.model_status:
                lines.append(
                    f"  Model:   {fa.model_status} (exit_code: {fa.exit_code if fa.exit_code is not None else '-'})"
                )
            if fa.protocol_status:
                lines.append(
                    f"  Proto:   {fa.protocol_status}"
                    + (f": {fa.protocol_error}" if fa.protocol_error else "")
                )
            reason = fa.error or fa.protocol_error or (f"Attempt {fa.status}" if fa.status.startswith("UNFINISHED") else None)
            if reason:
                lines.append(f"  Reason:  {escape_control_codes(reason)}")
            if fa.prompt_file:
                lines.append(f"  Prompt:  {fa.prompt_file}")
            if fa.stdout_file:
                lines.append(f"  Stdout:  {fa.stdout_file}")
            if fa.stderr_file:
                lines.append(f"  Stderr:  {fa.stderr_file}")
    lines.append("")

    # 5. Stored Final Response & Data Availability Notes
    lines.append("--- Final Response ---")
    if view.final_result:
        lines.append(escape_control_codes(view.final_result.strip()))
    else:
        lines.append("  (No final result stored)")
    lines.append("")

    if view.data_availability_notes or view.corrupt_artifacts:
        lines.append("--- Data Availability & Diagnostics ---")
        if view.data_availability_notes:
            lines.append("Notes:")
            for note in view.data_availability_notes:
                lines.append(f"  - {note}")
        if view.corrupt_artifacts:
            lines.append("Corrupt Artifacts:")
            for c in view.corrupt_artifacts:
                lines.append(f"  - {c.get('path')}: {c.get('error')}")
        if view.unsupported_schemas:
            lines.append("Unsupported Schema Versions:")
            for u in view.unsupported_schemas:
                lines.append(f"  - {u.get('artifact')}: version {u.get('version')}")
        lines.append("")

    # Optional timeline
    if show_timeline:
        lines.append("--- Timeline ---")
        if not view.timeline:
            lines.append("  (No timeline events recorded)")
        else:
            for t in view.timeline:
                aid_str = f" [{t.attempt_id}]" if t.attempt_id else ""
                lines.append(
                    f"[{t.timestamp}] ({t.type}){aid_str} {escape_control_codes(str(t.payload))}"
                )
        lines.append("")

    return "\n".join(lines)


def _format_worker_detail(w: WorkerInspectionView) -> str:
    lines: list[str] = [
        f"=== Worker Details: {w.worker_id} ===",
        f"Role:         {w.role}",
        f"Status:       {w.final_status}",
        f"Attempts:     {w.attempt_count}",
        f"Profiles:     {', '.join(w.profiles) if w.profiles else '-'}",
        f"Duration:     {format_duration(w.duration_seconds) if w.duration_seconds is not None else '-'}",
        f"Invocation:   {w.invocation_id or '-'}",
        f"Error:        {escape_control_codes(w.error or 'None')}",
        "",
        "--- Attempt History ---",
    ]
    for idx, att in enumerate(w.attempts, 1):
        lines.append(
            f"Attempt {idx} ({att.attempt_id}): {att.status} [Model: {att.model_status or '-'}]"
        )
        if att.profile_name:
            lines.append(f"  Profile:  {att.profile_name}")
        if att.duration_seconds is not None:
            lines.append(f"  Duration: {format_duration(att.duration_seconds)}")
        if att.error:
            lines.append(f"  Error:    {escape_control_codes(att.error)}")
        if att.prompt_file:
            lines.append(f"  Prompt:   {att.prompt_file}")
        if att.stdout_file:
            lines.append(f"  Stdout:   {att.stdout_file}")
        if att.stderr_file:
            lines.append(f"  Stderr:   {att.stderr_file}")
    return "\n".join(lines)


def _format_attempt_detail(a: AttemptInspectionView) -> str:
    lines: list[str] = [
        f"=== Attempt Details: {a.attempt_id} ===",
        f"Run ID:          {a.run_id}",
        f"Kind:            {a.kind}",
        f"Worker ID:       {a.worker_id or '-'}",
        f"Invocation ID:   {a.invocation_id or '-'}",
        f"Role:            {a.role or '-'}",
        f"Round:           {a.round_number if a.round_number is not None else '-'}",
        f"Attempt Number:  {a.attempt_number if a.attempt_number is not None else '-'}",
        f"Profile:         {a.profile_name or '-'}",
        f"Status:          {a.status}",
        f"Model Outcome:   {a.model_status or '-'} (exit_code: {a.exit_code if a.exit_code is not None else '-'})",
    ]
    if a.error:
        lines.append(f"Error:           {escape_control_codes(a.error)}")
    if a.protocol_status:
        proto_err = f" ({a.protocol_error})" if a.protocol_error else ""
        lines.append(
            f"Protocol:        {a.protocol_status} [Schema: {a.schema_name or '-'}{proto_err}]"
        )

    if a.usage:
        tokens = [f"{k}={v}" for k, v in a.usage.items()]
        lines.append(f"Usage:           {', '.join(tokens)}")
    else:
        lines.append("Usage:           unknown")

    if a.process_meta:
        lines.append(
            f"Process Meta:    PID={a.process_meta.get('pid')}, Argv={a.process_meta.get('argv')}"
        )

    lines.append(f"Prompt File:     {a.prompt_file or '-'}")
    lines.append("Streams:")
    if a.stream_paths:
        for sname, spath in a.stream_paths.items():
            sz = (
                Path(spath).stat().st_size
                if Path(spath).exists()
                else "file missing"
            )
            lines.append(f"  - {sname}: {spath} ({sz} bytes)")
    else:
        lines.append("  - (No stream files recorded)")

    if a.prompt_text:
        lines.append("")
        lines.append("--- Prompt Content ---")
        lines.append(escape_control_codes(a.prompt_text.strip()))

    if a.model_response:
        lines.append("")
        lines.append("--- Model Response ---")
        lines.append(escape_control_codes(a.model_response.strip()))
    elif a.structured_data is not None:
        lines.append("")
        lines.append("--- Structured Data ---")
        lines.append(
            escape_control_codes(json.dumps(a.structured_data, indent=2))
        )

    return "\n".join(lines)


# ============================================================================
# 6. Logs Streaming and Milestones Formatting
# ============================================================================


def format_log_entry(
    entry: dict[str, Any],
    run_dir: Path,
    stream_filter: str | None = None,
    stream_buffers: dict[tuple[str, str], StreamBuffer] | None = None,
) -> list[str]:
    """Convert a timeline or trace entry into human-readable milestone log lines."""
    lines: list[str] = []
    ts = entry.get("timestamp", "")
    etype = entry.get("type", "")
    aid = entry.get("attempt_id")
    payload = entry.get("payload", {})

    prefix = f"[{ts}]" if ts else ""

    # If stream filtering is requested (stdout, stderr, or all), only stream output chunks are emitted.
    if stream_filter:
        if etype != "output":
            return []
        stream_name = payload.get("stream", "stdout")
        if stream_filter != "all" and stream_filter != stream_name:
            return []
        offset = payload.get("offset", 0)
        length = payload.get("length", 0)
        if aid and length > 0:
            stream_file = run_dir / "attempts" / aid / f"{stream_name}.log"
            try:
                validate_safe_path(run_dir, stream_file)
                raw_bytes = read_stream_raw_chunk(stream_file, offset, length)
            except Exception:
                raw_bytes = b""
            if raw_bytes:
                if stream_buffers is not None:
                    buf = stream_buffers.setdefault((str(aid), str(stream_name)), StreamBuffer())
                    decoded = buf.decode_chunk(raw_bytes, final=False)
                else:
                    decoded = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(raw_bytes, final=True)
                escaped = escape_control_codes(decoded)
                if escaped:
                    lines.append(escaped)
        return lines

    # Milestone mode: readable milestones, decisions, failures, and tool activity
    if etype == "orchestration":
        o_type = payload.get("type") or payload.get("event_type") or ""
        o_inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        o_payload = o_inner if o_inner else payload
        rnd = payload.get("round_number") if payload.get("round_number") is not None else o_payload.get("round_number", 0)

        if o_type in ("RUN_CREATED", "run_created", "RUN_STARTED", "run_started"):
            lines.append(
                f"{prefix} [run] Started: {escape_control_codes(str(o_payload.get('task', '')))}"
            )
        elif o_type in ("ROUND_STARTED", "round_started"):
            lines.append(
                f"{prefix} [round {rnd}] Started"
            )
        elif o_type in ("ROUND_COMPLETED", "round_completed"):
            lines.append(
                f"{prefix} [round {rnd}] Completed"
            )
        elif o_type in ("ACTION_REQUESTED", "action_requested", "ACTION_DECIDED", "action_decided"):
            act_data = o_payload.get("action") if isinstance(o_payload.get("action"), dict) else {}
            act_kind = (
                act_data.get("kind")
                or act_data.get("action_type")
                or o_payload.get("action_type")
                or o_payload.get("action", "action")
            )
            lines.append(
                f"{prefix} [coordinator] Decision: {act_kind} (round {rnd})"
            )
        elif o_type in ("ACTION_ACCEPTED", "action_accepted"):
            lines.append(
                f"{prefix} [coordinator] Action accepted"
            )
        elif o_type in ("ACTION_REJECTED", "action_rejected"):
            reason = o_payload.get("reason") or o_payload.get("error") or "Action rejected"
            lines.append(
                f"{prefix} [coordinator] ACTION REJECTED: {escape_control_codes(str(reason))}"
            )
        elif o_type in ("PROFILE_LEASED", "profile_leased", "PROFILE_LEASE_ACQUIRED", "profile_lease_acquired"):
            prof = o_payload.get("profile_name")
            wid = o_payload.get("worker_id")
            wid_str = f" for {wid}" if wid else ""
            lines.append(f"{prefix} [lease] Acquired profile '{prof}'{wid_str}")
        elif o_type in ("PROFILE_RELEASED", "profile_released", "PROFILE_LEASE_RELEASED", "profile_lease_released"):
            prof = o_payload.get("profile_name")
            lines.append(f"{prefix} [lease] Released profile '{prof}'")
        elif o_type in ("INVOCATION_STARTED", "invocation_started", "WORKER_DISPATCHED", "worker_dispatched"):
            wid = o_payload.get("worker_id")
            role = o_payload.get("role")
            lines.append(
                f"{prefix} [worker {wid}] Dispatched role: {role or 'general'}"
            )
        elif o_type in ("INVOCATION_COMPLETED", "invocation_completed", "WORKER_COMPLETED", "worker_completed"):
            wid = o_payload.get("worker_id")
            dur = (
                format_duration(o_payload.get("duration_seconds"))
                if o_payload.get("duration_seconds")
                else ""
            )
            lines.append(
                f"{prefix} [worker {wid}] Completed {dur}".strip()
            )
        elif o_type in ("INVOCATION_FAILED", "invocation_failed", "WORKER_FAILED", "worker_failed"):
            wid = o_payload.get("worker_id")
            err = o_payload.get("error") or o_payload.get("failure") or ""
            lines.append(
                f"{prefix} [worker {wid}] FAILED: {escape_control_codes(str(err))}"
            )
        elif o_type in ("RUN_COMPLETED", "run_completed"):
            lines.append(f"{prefix} [run] Completed successfully")
        elif o_type in ("RUN_FAILED", "run_failed"):
            err = o_payload.get("error") or o_payload.get("reason") or ""
            lines.append(
                f"{prefix} [run] FAILED: {escape_control_codes(str(err))}"
            )
        elif o_type in ("RUN_INTERRUPTED", "run_interrupted"):
            reason = o_payload.get("reason") or "Interrupted"
            lines.append(
                f"{prefix} [run] INTERRUPTED: {escape_control_codes(str(reason))}"
            )
        elif o_type:
            lines.append(
                f"{prefix} [orchestration] {o_type}: {escape_control_codes(str(o_payload))}"
            )
        return lines

    if etype in ("RUN_COMPLETED", "run_completed"):
        lines.append(f"{prefix} [run] Completed successfully")
    elif etype in ("RUN_FAILED", "run_failed"):
        err = payload.get("error") or payload.get("reason") or ""
        lines.append(f"{prefix} [run] FAILED: {escape_control_codes(str(err))}")
    elif etype in ("RUN_INTERRUPTED", "run_interrupted"):
        reason = payload.get("reason") or "Interrupted"
        lines.append(f"{prefix} [run] INTERRUPTED: {escape_control_codes(str(reason))}")
    elif etype == "attempt_started":
        kind = payload.get("kind", "worker")
        wid = f" worker={payload.get('worker_id')}" if payload.get("worker_id") else ""
        lines.append(
            f"{prefix} [{aid}] Attempt started ({kind}{wid})"
        )
    elif etype == "attempt_finished":
        st = payload.get("status", "UNKNOWN")
        err = f" error: {payload.get('error')}" if payload.get("error") else ""
        lines.append(f"{prefix} [{aid}] Attempt finished: {st}{err}")
    elif etype in ("process_started", "process_attached"):
        pid = payload.get("pid")
        action_name = "Started" if etype == "process_started" else "Attached"
        lines.append(f"{prefix} [{aid or 'process'}] {action_name} PID {pid}")
    elif etype == "protocol_accepted":
        schema = payload.get("schema_name", "")
        lines.append(f"{prefix} [{aid}] Protocol schema accepted: {schema}")
    elif etype == "protocol_rejected":
        schema = payload.get("schema_name", "")
        err = payload.get("error", "")
        lines.append(
            f"{prefix} [{aid}] Protocol schema REJECTED ({schema}): {escape_control_codes(str(err))}"
        )
    elif etype == "trace_recovered":
        part = payload.get("partial_file")
        lines.append(f"{prefix} [trace] Recovered torn tail to {part}")
    elif etype == "output":
        stream_name = payload.get("stream", "stdout")
        offset = payload.get("offset", 0)
        length = payload.get("length", 0)

        if aid and length > 0:
            stream_file = run_dir / "attempts" / aid / f"{stream_name}.log"
            try:
                validate_safe_path(run_dir, stream_file)
                raw_bytes = read_stream_raw_chunk(stream_file, offset, length)
            except Exception:
                raw_bytes = b""

            if raw_bytes:
                if stream_buffers is not None:
                    buf = stream_buffers.setdefault((str(aid), str(stream_name)), StreamBuffer())
                    ndjson_lines = buf.feed_for_lines(raw_bytes, final=False)
                else:
                    text = read_stream_chunk(stream_file, offset, length)
                    ndjson_lines = text.split("\n")

                for line in ndjson_lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                        pev = parsed.get("event")
                        if pev == "tool_call":
                            tname = parsed.get("name") or parsed.get("tool_name")
                            lines.append(
                                f"{prefix} [{aid}] tool_call: {tname}"
                            )
                        elif pev == "tool_result":
                            tname = parsed.get("name") or "tool"
                            lines.append(
                                f"{prefix} [{aid}] tool_result: {tname}"
                            )
                        elif pev == "step_update":
                            supdate = parsed.get("step_update", {})
                            stype = supdate.get("step_type", "step")
                            lines.append(
                                f"{prefix} [{aid}] step_update: {stype}"
                            )
                        elif pev and pev not in ("init", "delta"):
                            lines.append(
                                f"{prefix} [{aid}] provider_event: {pev}"
                            )
                    except json.JSONDecodeError:
                        pass
    else:
        # Unknown event type: generic description
        lines.append(
            f"{prefix} [event] {etype}: {escape_control_codes(str(payload))}"
        )

    return lines


def stream_logs(
    run_dir: Path | str,
    follow: bool = False,
    worker_id: str | None = None,
    attempt_id: str | None = None,
    stream: str | None = None,
    as_json: bool = False,
    output_stream: TextIO | None = None,
) -> int:
    """Stream or poll run logs. Follow mode stays alive until terminal run event.

    Handles file truncation and Ctrl+C cleanly without altering run state.
    """
    out = output_stream or sys.stdout
    run_path = Path(run_dir).resolve()
    if not run_path.exists():
        sys.stderr.write(f"agym: run directory not found: {run_path}\n")
        return 1

    trace_path = run_path / "trace.jsonl"
    events_path = run_path / "events.jsonl"

    use_trace = trace_path.exists()
    cursor = 0
    seen_terminal = False
    last_ino: int | None = None

    # Track worker-to-attempt lookup dynamically
    matching_attempt_ids: set[str] = set()
    attempt_to_worker: dict[str, str] = {}
    stream_buffers: dict[tuple[str, str], StreamBuffer] = {}

    def _sync_worker_attempts() -> None:
        if not worker_id:
            return
        attempts_dir = run_path / "attempts"
        if attempts_dir.exists() and attempts_dir.is_dir():
            try:
                for adir in attempts_dir.iterdir():
                    if not adir.is_dir():
                        continue
                    aid_name = adir.name
                    if aid_name in attempt_to_worker:
                        continue
                    rec_file = adir / "record.json"
                    if rec_file.exists():
                        try:
                            rec = json.loads(rec_file.read_text(encoding="utf-8"))
                            wid = rec.get("worker_id")
                            if wid:
                                attempt_to_worker[aid_name] = wid
                                if wid == worker_id:
                                    matching_attempt_ids.add(aid_name)
                        except Exception:
                            pass
            except OSError:
                pass

    _sync_worker_attempts()

    try:
        while True:
            target_path = trace_path if use_trace else events_path
            if not target_path.exists():
                if not follow:
                    out.write("No logs recorded for this run.\n")
                    out.flush()
                    return 0
                time.sleep(0.1)
                if trace_path.exists():
                    use_trace = True
                continue

            # Detect truncation or inode replacement
            try:
                st = target_path.stat()
                curr_size = st.st_size
                curr_ino = st.st_ino
                if last_ino is not None and curr_ino != last_ino:
                    out.write("[trace truncated/reset]\n")
                    out.flush()
                    cursor = 0
                    last_ino = curr_ino
                elif curr_size < cursor:
                    out.write("[trace truncated/reset]\n")
                    out.flush()
                    cursor = 0
                if last_ino is None:
                    last_ino = curr_ino
            except OSError:
                pass

            entries, cursor, _ = read_timeline_entries(target_path, cursor=cursor)

            for entry in entries:
                aid = entry.get("attempt_id")
                etype = entry.get("type")
                payload = entry.get("payload", {})

                # Update dynamic worker mapping from attempt_started
                if etype == "attempt_started":
                    entry_wid = payload.get("worker_id")
                    if entry_wid and aid:
                        attempt_to_worker[aid] = entry_wid
                        if entry_wid == worker_id:
                            matching_attempt_ids.add(aid)

                # Filter by attempt
                if attempt_id and aid != attempt_id:
                    continue

                # Filter by worker
                if worker_id:
                    if aid:
                        if aid not in matching_attempt_ids:
                            _sync_worker_attempts()
                        if aid not in matching_attempt_ids:
                            continue
                    else:
                        # Entry without attempt_id (e.g. orchestration event)
                        if etype == "orchestration":
                            o_inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                            o_payload = o_inner if o_inner else payload
                            ev_wid = o_payload.get("worker_id") or payload.get("worker_id")
                            if ev_wid and ev_wid != worker_id:
                                continue
                            workers = o_payload.get("workers") or o_payload.get("worker_invocations")
                            if workers:
                                w_names = [w.get("worker_id", w) if isinstance(w, dict) else str(w) for w in workers]
                                if worker_id not in w_names:
                                    continue

                # Check if terminal run event
                if etype in ("run_completed", "run_failed", "run_interrupted"):
                    seen_terminal = True
                elif etype == "orchestration":
                    o_type = payload.get("type") or payload.get("event_type") or ""
                    if o_type in (
                        "RUN_COMPLETED",
                        "run_completed",
                        "RUN_FAILED",
                        "run_failed",
                        "RUN_INTERRUPTED",
                        "run_interrupted",
                    ):
                        seen_terminal = True

                if as_json:
                    out.write(json.dumps(entry) + "\n")
                    out.flush()
                else:
                    lines = format_log_entry(
                        entry,
                        run_path,
                        stream_filter=stream,
                        stream_buffers=stream_buffers,
                    )
                    if stream:
                        for ln in lines:
                            if ln:
                                out.write(ln)
                    else:
                        for ln in lines:
                            if ln:
                                out.write(ln + "\n")
                    out.flush()

            if not follow or seen_terminal:
                break

            time.sleep(0.1)

        # Terminal flush of remaining stream buffers
        if stream:
            for buf in stream_buffers.values():
                tail = buf.decoder.decode(b"", final=True)
                if tail:
                    out.write(escape_control_codes(tail))
            out.flush()
        else:
            for (buf_aid, _), buf in stream_buffers.items():
                tail_lines = buf.flush()
                for line in tail_lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                        pev = parsed.get("event")
                        if pev == "tool_call":
                            tname = parsed.get("name") or parsed.get("tool_name")
                            out.write(f"[{buf_aid}] tool_call: {tname}\n")
                        elif pev == "tool_result":
                            tname = parsed.get("name") or "tool"
                            out.write(f"[{buf_aid}] tool_result: {tname}\n")
                    except json.JSONDecodeError:
                        pass
            out.flush()

    except KeyboardInterrupt:
        # Clean exit on Ctrl+C without affecting orchestration
        return 130

    return 0


# ============================================================================
# 7. CLI Subcommand Entrypoints
# ============================================================================


def run_inspect_cli(argv: list[str], run_store: Any) -> int:
    """CLI dispatcher for `agym orchestrate inspect <run-id>`."""
    parser = argparse.ArgumentParser(
        prog="agym orchestrate inspect",
        description="Inspect an orchestration run with failure analysis and logs.",
    )
    parser.add_argument("run_id", help="The orchestration run ID to inspect")
    parser.add_argument("--worker", dest="worker_id", default=None, help="Filter by worker ID")
    parser.add_argument("--attempt", dest="attempt_id", default=None, help="Detail a specific attempt ID")
    parser.add_argument("--timeline", action="store_true", help="Include full chronological event timeline")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output normalized view as JSON")

    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if ".." in ns.run_id or "/" in ns.run_id or "\\" in ns.run_id:
        sys.stderr.write(f"agym: invalid run ID: {ns.run_id}\n")
        return 2

    run_dir = run_store.run_dir(ns.run_id) if hasattr(run_store, "run_dir") else (get_default_runs_dir() / ns.run_id)
    if not run_dir.exists():
        sys.stderr.write(f"agym: run not found: {ns.run_id}\n")
        return 1

    try:
        view = inspect_run(run_dir, ns.run_id)
    except Exception as exc:
        sys.stderr.write(f"agym: failed to inspect run: {exc}\n")
        return 1

    if ns.attempt_id:
        if not any(a.attempt_id == ns.attempt_id for a in view.all_attempts):
            sys.stderr.write(f"agym: attempt not found: {ns.attempt_id}\n")
            return 1

    if ns.worker_id:
        if not any(w.worker_id == ns.worker_id for w in view.workers):
            sys.stderr.write(f"agym: worker not found: {ns.worker_id}\n")
            return 1

    if ns.as_json:
        if ns.attempt_id:
            att = next(a for a in view.all_attempts if a.attempt_id == ns.attempt_id)
            print(json.dumps(att.to_dict(), indent=2))
        elif ns.worker_id:
            w = next(w for w in view.workers if w.worker_id == ns.worker_id)
            print(json.dumps(w.to_dict(), indent=2))
        else:
            print(json.dumps(view.to_dict(), indent=2))
    else:
        text = format_inspect_text(
            view,
            worker_id=ns.worker_id,
            attempt_id=ns.attempt_id,
            show_timeline=ns.timeline,
        )
        print(text)

    return 0


def run_logs_cli(argv: list[str], run_store: Any) -> int:
    """CLI dispatcher for `agym orchestrate logs <run-id>`."""
    parser = argparse.ArgumentParser(
        prog="agym orchestrate logs",
        description="View or follow execution logs and milestones for an orchestration run.",
    )
    parser.add_argument("run_id", help="The orchestration run ID")
    parser.add_argument("-f", "--follow", action="store_true", help="Follow logs live")
    parser.add_argument("--worker", dest="worker_id", default=None, help="Filter by worker ID")
    parser.add_argument("--attempt", dest="attempt_id", default=None, help="Filter by attempt ID")
    parser.add_argument(
        "--stream",
        dest="stream",
        choices=["stdout", "stderr", "all"],
        default=None,
        help="Stream log output (stdout, stderr, or all)",
    )
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output full JSON event envelopes")

    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if ".." in ns.run_id or "/" in ns.run_id or "\\" in ns.run_id:
        sys.stderr.write(f"agym: invalid run ID: {ns.run_id}\n")
        return 2

    run_dir = run_store.run_dir(ns.run_id) if hasattr(run_store, "run_dir") else (get_default_runs_dir() / ns.run_id)
    if not run_dir.exists():
        sys.stderr.write(f"agym: run not found: {ns.run_id}\n")
        return 1

    return stream_logs(
        run_dir=run_dir,
        follow=ns.follow,
        worker_id=ns.worker_id,
        attempt_id=ns.attempt_id,
        stream=ns.stream,
        as_json=ns.as_json,
    )

