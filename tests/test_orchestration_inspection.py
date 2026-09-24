"""Observability and inspection tests for orchestration runs.

Verifies:
- Shared reader and normalized view data model
- Independent snapshot and attempt loading with corrupt artifact tolerance
- Separation of model outcome from protocol outcome
- Worker retries, failover, and coordinator correction linking
- Legacy run detection and explicit gap notes
- Byte cursor timeline reader, partial writes, and truncation recovery
- Path traversal protection and ANSI/control code escaping
- Full CLI formatters for `agym orchestrate inspect` and `agym orchestrate logs`
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agym.orchestration.contracts import ModelResult, InvocationStatus
from agym.orchestration.inspection import (
    AttemptInspectionView,
    CoordinatorDecisionView,
    RunInspectionView,
    TimelineEntryView,
    WorkerInspectionView,
    escape_control_codes,
    format_inspect_text,
    inspect_run,
    read_stream_chunk,
    read_timeline_entries,
    run_inspect_cli,
    run_logs_cli,
    stream_logs,
    validate_safe_path,
)
from agym.orchestration.persistence import (
    FileRunStore,
    RunNotFoundError,
    atomic_write_json,
    atomic_write_text,
)
from agym.orchestration.recording import append_trace, capture_attempt


class InspectionUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FileRunStore(self.root / "runs")

    def test_escape_control_codes(self) -> None:
        raw = "Hello \x1b[31;1mRed Alert\x1b[0m\rWorld!\tTab\nNewline\x00Null"
        sanitized = escape_control_codes(raw)
        # ANSI escape codes neutralized
        self.assertNotIn("\x1b", sanitized)
        self.assertIn("\\x1b[31;1mRed Alert\\x1b[0m", sanitized)
        # Carriage return escaped
        self.assertIn("\\rWorld!", sanitized)
        # Null byte escaped
        self.assertIn("\\x00Null", sanitized)
        # Normal newline and tab preserved
        self.assertIn("\tTab\nNewline", sanitized)

    def test_validate_safe_path(self) -> None:
        run_dir = self.root / "runs" / "run-1"
        run_dir.mkdir(parents=True, exist_ok=True)
        safe = validate_safe_path(run_dir, "attempts/att-1/prompt.txt")
        self.assertEqual(safe, run_dir / "attempts" / "att-1" / "prompt.txt")

        with self.assertRaises(ValueError) as ctx:
            validate_safe_path(run_dir, "../../etc/passwd")
        self.assertIn("escapes run root", str(ctx.exception))

    def test_byte_cursor_and_torn_tail_recovery(self) -> None:
        trace_file = self.root / "trace.jsonl"
        entry1 = {"schema_version": 1, "event_id": "e1", "type": "start", "payload": {}}
        entry2 = {"schema_version": 1, "event_id": "e2", "type": "step", "payload": {}}

        # Write complete entry1, and partial entry2 without newline
        trace_file.write_text(json.dumps(entry1) + "\n" + json.dumps(entry2)[:10], encoding="utf-8")

        entries, cursor, corrupt = read_timeline_entries(trace_file, cursor=0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event_id"], "e1")
        self.assertEqual(corrupt, [])
        self.assertEqual(cursor, len(json.dumps(entry1).encode("utf-8")) + 1)

        # Complete entry2 with newline
        with trace_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry2)[10:] + "\n")

        entries2, cursor2, corrupt2 = read_timeline_entries(trace_file, cursor=cursor)
        self.assertEqual(len(entries2), 1)
        self.assertEqual(entries2[0]["event_id"], "e2")
        self.assertEqual(corrupt2, [])
        self.assertGreater(cursor2, cursor)

        # File truncation / reset
        trace_file.write_text(json.dumps(entry1) + "\n", encoding="utf-8")
        entries3, cursor3, _ = read_timeline_entries(trace_file, cursor=cursor2)
        # Cursor should have reset to 0
        self.assertEqual(len(entries3), 1)
        self.assertEqual(entries3[0]["event_id"], "e1")

    def test_read_stream_chunk(self) -> None:
        stream_file = self.root / "stdout.log"
        stream_file.write_bytes(b"0123456789ABCDEF")
        chunk = read_stream_chunk(stream_file, offset=4, length=6)
        self.assertEqual(chunk, "456789")

    def test_current_run_inspection_complete(self) -> None:
        run_id = "run-complete"
        self.store.create_run(run_id, "Complete task")
        run_dir = self.store.run_dir(run_id)

        # Worker w-0 execution
        with capture_attempt(
            self.store,
            run_id,
            prompt="Worker 0 prompt",
            kind="worker",
            worker_id="w-0",
            invocation_id="inv-0",
            role="ARCHITECT",
            round_number=0,
            attempt_number=1,
            profile_name="prof-alpha",
        ) as cap:
            assert cap is not None
            cap.note("process_started", pid=12345, argv=["agy", "exec"])
            cap.write("stdout", b'{"event":"tool_call","name":"read_file"}\n')
            cap.write("stderr", b"internal trace\n")
            cap.finish(ModelResult(invocation_id="inv-0", response="Plan ready", usage={"tokens": 42}))

        # Coordinator decision
        append_trace(
            self.store,
            run_id,
            "orchestration",
            {
                "event_type": "ACTION_DECIDED",
                "round_number": 0,
                "payload": {"action_type": "DISPATCH_WORKERS", "workers": ["w-0"]},
            },
        )
        append_trace(
            self.store,
            run_id,
            "orchestration",
            {
                "event_type": "RUN_COMPLETED",
                "round_number": 0,
                "payload": {"summary": "Task fully accomplished."},
            },
        )

        view = inspect_run(run_dir)
        self.assertEqual(view.run_id, run_id)
        self.assertEqual(view.status, "COMPLETED")
        self.assertEqual(view.final_result, "Task fully accomplished.")
        self.assertEqual(len(view.workers), 1)

        w = view.workers[0]
        self.assertEqual(w.worker_id, "w-0")
        self.assertEqual(w.role, "ARCHITECT")
        self.assertEqual(w.final_status, "SUCCEEDED")
        self.assertEqual(w.attempt_count, 1)
        self.assertIn("prof-alpha", w.profiles)

        # Inspect attempt
        att = w.attempts[0]
        self.assertEqual(att.worker_id, "w-0")
        self.assertEqual(att.model_response, "Plan ready")
        self.assertEqual(att.usage, {"tokens": 42})
        self.assertIn("stdout", att.available_streams)
        self.assertIn("stderr", att.available_streams)
        self.assertEqual(att.process_meta, {"pid": 12345, "argv": ["agy", "exec"], "cwd": None, "output_format": None})

        # Text output check
        text = format_inspect_text(view)
        self.assertIn("=== Run Inspection: run-complete ===", text)
        self.assertIn("w-0", text)
        self.assertIn("Task fully accomplished.", text)
        self.assertNotIn("FAILED", text)

    def test_legacy_run_inspection_with_gap_notes(self) -> None:
        run_id = "run-legacy"
        run_dir = self.store.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        # Legacy run has run.json and events.jsonl, but NO trace.jsonl and NO attempts/
        atomic_write_json(
            run_dir / "run.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "task": "Legacy run task",
                "status": "COMPLETED",
                "mode": "plan",
                "budget_usage": {"invocations": 1},
            },
        )
        atomic_write_text(
            run_dir / "events.jsonl",
            json.dumps({"event_id": "e1", "event_type": "RUN_STARTED", "payload": {"task": "Legacy run task"}}) + "\n"
            + json.dumps({"event_id": "e2", "event_type": "RUN_COMPLETED", "payload": {"summary": "Done"}}) + "\n"
        )
        # Invocations snapshot
        inv_dir = run_dir / "invocations"
        inv_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            inv_dir / "inv-legacy-1.json",
            {
                "invocation_id": "inv-legacy-1",
                "worker_id": "w-legacy",
                "role": "GENERAL",
                "status": "SUCCEEDED",
            },
        )

        view = inspect_run(run_dir)
        self.assertEqual(view.run_id, run_id)
        self.assertEqual(view.status, "COMPLETED")
        self.assertIn("attempt history unavailable", view.data_availability_notes)
        self.assertIn("raw streams unavailable", view.data_availability_notes)
        self.assertIn("usage unknown", view.data_availability_notes)
        self.assertEqual(len(view.workers), 1)
        self.assertEqual(view.workers[0].worker_id, "w-legacy")

        text = format_inspect_text(view)
        self.assertIn("attempt history unavailable", text)
        self.assertIn("raw streams unavailable", text)
        self.assertIn("usage unknown", text)

    def test_worker_retries_and_failover_display(self) -> None:
        run_id = "run-failover"
        self.store.create_run(run_id, "Failover test")
        run_dir = self.store.run_dir(run_id)

        # Attempt 1: FAILED
        with capture_attempt(
            self.store,
            run_id,
            prompt="Attempt 1 prompt",
            kind="worker",
            worker_id="w-retry",
            invocation_id="inv-shared",
            role="TESTING",
            attempt_number=1,
            profile_name="prof-bad",
        ) as cap1:
            assert cap1 is not None
            cap1.write("stderr", b"timeout error\n")
            cap1.finish(ModelResult(invocation_id="inv-shared", status=InvocationStatus.FAILED, error="Timed out"))

        # Attempt 2: SUCCEEDED on different profile
        with capture_attempt(
            self.store,
            run_id,
            prompt="Attempt 2 prompt",
            kind="worker",
            worker_id="w-retry",
            invocation_id="inv-shared",
            role="TESTING",
            attempt_number=2,
            profile_name="prof-good",
        ) as cap2:
            assert cap2 is not None
            cap2.write("stdout", b"All tests passed\n")
            cap2.finish(ModelResult(invocation_id="inv-shared", status=InvocationStatus.SUCCEEDED, response="Success"))

        append_trace(
            self.store,
            run_id,
            "orchestration",
            {
                "event_type": "RUN_COMPLETED",
                "payload": {"summary": "Done after failover"},
            },
        )

        view = inspect_run(run_dir)
        self.assertEqual(len(view.workers), 1)
        w = view.workers[0]
        self.assertEqual(w.worker_id, "w-retry")
        self.assertEqual(w.attempt_count, 2)
        self.assertEqual(w.final_status, "SUCCEEDED")
        self.assertIn("prof-bad", w.profiles)
        self.assertIn("prof-good", w.profiles)

        # Both attempts are retained
        self.assertEqual(len(w.attempts), 2)
        self.assertEqual(w.attempts[0].status, "FAILED")
        self.assertEqual(w.attempts[1].status, "SUCCEEDED")

        # Failed attempt is listed under failed_or_unfinished_attempts
        self.assertEqual(len(view.failed_or_unfinished_attempts), 1)
        self.assertEqual(view.failed_or_unfinished_attempts[0].attempt_id, cap1.attempt_id)

        text = format_inspect_text(view)
        self.assertIn("prof-bad, prof-good", text)
        self.assertIn("Attempt: " + cap1.attempt_id, text)
        self.assertIn("Timed out", text)

    def test_coordinator_model_success_with_protocol_rejection_and_correction(self) -> None:
        run_id = "run-coord-correction"
        self.store.create_run(run_id, "Coord protocol test")
        run_dir = self.store.run_dir(run_id)

        # Turn 0: Model succeeded, but output violated schema
        with capture_attempt(
            self.store,
            run_id,
            prompt="Decide action",
            kind="coordinator",
            worker_id="coordinator",
            round_number=0,
            correction_attempt=0,
            schema_name="action_schema",
        ) as cap0:
            assert cap0 is not None
            cap0.write("stdout", b'{"invalid":"json_missing_action"}\n')
            cap0.finish(ModelResult(invocation_id="coord-0", response='{"invalid":"json_missing_action"}'))
            cap0.note("protocol_rejected", schema_name="action_schema", round_number=0, error="Missing required property 'action'")

        # Turn 1: Corrected turn
        with capture_attempt(
            self.store,
            run_id,
            prompt="Decide action (correction)",
            kind="coordinator",
            worker_id="coordinator",
            round_number=0,
            correction_attempt=1,
            schema_name="action_schema",
        ) as cap1:
            assert cap1 is not None
            cap1.write("stdout", b'{"action":"FINALIZE"}\n')
            cap1.finish(ModelResult(invocation_id="coord-1", response='{"action":"FINALIZE"}'))
            cap1.note("protocol_accepted", schema_name="action_schema", round_number=0)

        append_trace(
            self.store,
            run_id,
            "orchestration",
            {
                "event_type": "ACTION_DECIDED",
                "round_number": 0,
                "payload": {"action_type": "FINALIZE"},
            },
        )
        append_trace(
            self.store,
            run_id,
            "orchestration",
            {
                "event_type": "RUN_COMPLETED",
                "payload": {"summary": "Finished"},
            },
        )

        view = inspect_run(run_dir)
        # Check attempts
        att0 = next(a for a in view.all_attempts if a.attempt_id == cap0.attempt_id)
        self.assertEqual(att0.model_status, "SUCCEEDED")
        self.assertEqual(att0.protocol_status, "REJECTED")
        self.assertIn("Missing required property 'action'", att0.protocol_error or "")

        att1 = next(a for a in view.all_attempts if a.attempt_id == cap1.attempt_id)
        self.assertEqual(att1.model_status, "SUCCEEDED")
        self.assertEqual(att1.protocol_status, "ACCEPTED")

        # Coordinator decisions view shows correction
        self.assertEqual(len(view.coordinator_decisions), 1)
        cd = view.coordinator_decisions[0]
        self.assertEqual(cd.round_number, 0)
        self.assertEqual(cd.action_type, "FINALIZE")
        self.assertEqual(len(cd.corrections), 1)
        self.assertIn("Missing required property 'action'", cd.corrections[0])

    def test_corrupt_artifacts_tolerance(self) -> None:
        run_id = "run-corrupt"
        run_dir = self.store.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        # Corrupt run.json
        (run_dir / "run.json").write_text("{corrupt json", encoding="utf-8")

        # Healthy attempt
        att_dir = run_dir / "attempts" / "attempt-ok"
        att_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            att_dir / "record.json",
            {
                "schema_version": 1,
                "attempt_id": "attempt-ok",
                "worker_id": "w-ok",
                "kind": "worker",
                "status": "SUCCEEDED",
            },
        )

        # Corrupt attempt record.json
        bad_att_dir = run_dir / "attempts" / "attempt-bad"
        bad_att_dir.mkdir(parents=True, exist_ok=True)
        (bad_att_dir / "record.json").write_text("not json", encoding="utf-8")

        # Trace with one corrupt line and one valid line
        (run_dir / "trace.jsonl").write_text(
            "not a json line\n" + json.dumps({"schema_version": 1, "event_id": "e1", "type": "heartbeat", "payload": {}}) + "\n",
            encoding="utf-8",
        )

        # Reader must NOT crash and must report corrupt artifacts with paths
        view = inspect_run(run_dir)
        self.assertEqual(len(view.workers), 1)
        self.assertEqual(view.workers[0].worker_id, "w-ok")

        corrupt_paths = [c["path"] for c in view.corrupt_artifacts]
        self.assertTrue(any("run.json" in p for p in corrupt_paths))
        self.assertTrue(any("attempt-bad" in p for p in corrupt_paths))
        self.assertTrue(any("trace.jsonl" in p for p in corrupt_paths))

    def test_unfinished_attempt_reconciled_with_terminal_run(self) -> None:
        run_id = "run-unfinished"
        self.store.create_run(run_id, "Unfinished test")
        run_dir = self.store.run_dir(run_id)

        # Attempt recorded as RUNNING
        with capture_attempt(
            self.store,
            run_id,
            prompt="Running task",
            kind="worker",
            worker_id="w-stale",
            invocation_id="inv-stale",
        ) as cap:
            assert cap is not None
            # Simulate sudden crash: do not call cap.finish()

        # Run is marked FAILED
        append_trace(
            self.store,
            run_id,
            "orchestration",
            {
                "event_type": "RUN_FAILED",
                "payload": {"error": "Coordinator died"},
            },
        )

        view = inspect_run(run_dir)
        att = view.all_attempts[0]
        # Reconciled status shows UNFINISHED because run is terminal
        self.assertIn("UNFINISHED", att.status)
        # Disk record.json was NOT mutated
        raw_on_disk = json.loads((cap.directory / "record.json").read_text())
        self.assertEqual(raw_on_disk["status"], "RUNNING")

    def test_unsupported_schema_version_preserved(self) -> None:
        run_id = "run-schema-99"
        self.store.create_run(run_id, "Schema test")
        run_dir = self.store.run_dir(run_id)

        # Entry with unsupported schema_version 99
        append_trace(
            self.store,
            run_id,
            "future_event",
            {"future_key": "future_val"},
        )
        # Modify line to schema_version 99
        trace_path = run_dir / "trace.jsonl"
        lines = trace_path.read_text().splitlines()
        e = json.loads(lines[-1])
        e["schema_version"] = 99
        trace_path.write_text(json.dumps(e) + "\n")

        view = inspect_run(run_dir)
        self.assertEqual(len(view.timeline), 1)
        self.assertTrue(view.timeline[0].unsupported_version)
        self.assertEqual(view.timeline[0].payload["future_key"], "future_val")

    def test_all_workers_failed_scenario_in_one_view(self) -> None:
        run_id = "run-all-failed"
        self.store.create_run(run_id, "Task all failed")
        run_dir = self.store.run_dir(run_id)

        for i in range(2):
            with capture_attempt(
                self.store,
                run_id,
                prompt=f"Worker {i} prompt",
                kind="worker",
                worker_id=f"w-{i}",
                invocation_id=f"inv-{i}",
                role="GENERAL",
                profile_name=f"prof-{i}",
            ) as cap:
                assert cap is not None
                cap.finish(ModelResult(invocation_id=f"inv-{i}", status=InvocationStatus.FAILED, error=f"Internal failure on node {i}", exit_code=1))

        append_trace(
            self.store,
            run_id,
            "orchestration",
            {
                "event_type": "RUN_FAILED",
                "payload": {"error": "No workers succeeded."},
            },
        )

        view = inspect_run(run_dir)
        self.assertEqual(view.status, "FAILED")
        self.assertEqual(len(view.workers), 2)
        for w in view.workers:
            self.assertEqual(w.final_status, "FAILED")
            self.assertIn("Internal failure", w.error or "")

        text = format_inspect_text(view)
        # Verify both worker errors appear right in terminal text without opening JSON
        self.assertIn("Internal failure on node 0", text)
        self.assertIn("Internal failure on node 1", text)
        self.assertIn("w-0", text)
        self.assertIn("w-1", text)


class CliObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FileRunStore(self.root / "runs")
        self.run_id = "run-cli-test"
        self.store.create_run(self.run_id, "CLI test task")

    def test_run_inspect_cli_default_and_flags(self) -> None:
        # Worker attempt
        with capture_attempt(
            self.store,
            self.run_id,
            prompt="CLI prompt text",
            kind="worker",
            worker_id="w-cli",
            invocation_id="inv-cli",
            role="DEVELOPER",
        ) as cap:
            assert cap is not None
            cap.write("stdout", b"Worker finished.\n")
            cap.finish(ModelResult(invocation_id="inv-cli", response="All done", usage={"tokens": 10}))

        append_trace(
            self.store,
            self.run_id,
            "orchestration",
            {"event_type": "RUN_COMPLETED", "payload": {"summary": "CLI run completed."}},
        )

        # 1. Default inspect
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = run_inspect_cli([self.run_id], self.store)
        self.assertEqual(code, 0)
        self.assertIn(f"=== Run Inspection: {self.run_id} ===", out.getvalue())
        self.assertIn("w-cli", out.getvalue())
        self.assertIn("CLI run completed.", out.getvalue())

        # 2. Inspect with --json
        out_json = io.StringIO()
        with mock.patch("sys.stdout", out_json):
            code_json = run_inspect_cli([self.run_id, "--json"], self.store)
        self.assertEqual(code_json, 0)
        parsed = json.loads(out_json.getvalue())
        self.assertEqual(parsed["run_id"], self.run_id)
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(len(parsed["workers"]), 1)

        # 3. Inspect with --worker
        out_w = io.StringIO()
        with mock.patch("sys.stdout", out_w):
            code_w = run_inspect_cli([self.run_id, "--worker", "w-cli"], self.store)
        self.assertEqual(code_w, 0)
        self.assertIn("=== Worker Details: w-cli ===", out_w.getvalue())

        # 4. Inspect with --attempt
        out_att = io.StringIO()
        with mock.patch("sys.stdout", out_att):
            code_att = run_inspect_cli([self.run_id, "--attempt", cap.attempt_id], self.store)
        self.assertEqual(code_att, 0)
        self.assertIn(f"=== Attempt Details: {cap.attempt_id} ===", out_att.getvalue())
        self.assertIn("CLI prompt text", out_att.getvalue())
        self.assertIn("All done", out_att.getvalue())

        # 5. Nonexistent run
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code_ghost = run_inspect_cli(["ghost-run"], self.store)
        self.assertEqual(code_ghost, 1)

        # 6. Nonexistent worker
        err_w = io.StringIO()
        with mock.patch("sys.stderr", err_w):
            code_bad_w = run_inspect_cli([self.run_id, "--worker", "w-ghost"], self.store)
        self.assertEqual(code_bad_w, 1)

    def test_run_logs_cli_milestones_and_stream(self) -> None:
        with capture_attempt(
            self.store,
            self.run_id,
            prompt="Log task",
            kind="worker",
            worker_id="w-log",
            invocation_id="inv-log",
        ) as cap:
            assert cap is not None
            cap.write("stdout", b'{"event":"tool_call","name":"search_web"}\n')
            cap.write("stderr", b"warn: rate limit\n")
            cap.finish(ModelResult(invocation_id="inv-log", response="Search complete"))

        append_trace(
            self.store,
            self.run_id,
            "orchestration",
            {"event_type": "RUN_COMPLETED", "payload": {}},
        )

        # Default logs: shows tool activity and milestones
        out = io.StringIO()
        code = stream_logs(self.store.run_dir(self.run_id), output_stream=out)
        self.assertEqual(code, 0)
        val = out.getvalue()
        self.assertIn("tool_call: search_web", val)
        self.assertIn("[run] Completed successfully", val)

        # Stream filter: stdout
        out_stdout = io.StringIO()
        code_s = stream_logs(self.store.run_dir(self.run_id), stream="stdout", output_stream=out_stdout)
        self.assertEqual(code_s, 0)
        self.assertIn('"event":"tool_call"', out_stdout.getvalue())

        # Stream filter: stderr
        out_stderr = io.StringIO()
        code_err = stream_logs(self.store.run_dir(self.run_id), stream="stderr", output_stream=out_stderr)
        self.assertEqual(code_err, 0)
        self.assertIn("warn: rate limit", out_stderr.getvalue())

        # JSON mode
        out_json = io.StringIO()
        code_j = stream_logs(self.store.run_dir(self.run_id), as_json=True, output_stream=out_json)
        self.assertEqual(code_j, 0)
        json_lines = [json.loads(line) for line in out_json.getvalue().splitlines() if line.strip()]
        self.assertTrue(len(json_lines) >= 2)

    def test_cli_help_and_path_traversal(self) -> None:
        # Help returns 0
        help_out = io.StringIO()
        with mock.patch("sys.stdout", help_out):
            self.assertEqual(run_inspect_cli(["--help"], self.store), 0)
            self.assertEqual(run_logs_cli(["-h"], self.store), 0)
        self.assertIn("usage: agym orchestrate inspect", help_out.getvalue())
        self.assertIn("usage: agym orchestrate logs", help_out.getvalue())

        # Path traversal returns 2
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(run_inspect_cli(["../../evil"], self.store), 2)
            self.assertEqual(run_logs_cli(["../etc/passwd"], self.store), 2)
        self.assertIn("invalid run ID", err.getvalue())


class EdgeCaseAndRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FileRunStore(self.root / "runs")
        self.run_id = "run-edge-cases"
        self.store.create_run(self.run_id, "Edge cases test task")
        self.run_dir = self.store.run_dir(self.run_id)

    def test_real_orchestration_events_and_follow_termination(self) -> None:
        # Emit real OrchestrationEvents (as generated by the deterministic engine)
        from agym.orchestration.contracts import EventType, OrchestrationEvent, RunId
        from agym.orchestration.recording import append_trace

        events = [
            OrchestrationEvent(event_id="e1", run_id=RunId(self.run_id), type=EventType.ROUND_STARTED, payload={"round_number": 1}),
            OrchestrationEvent(event_id="e2", run_id=RunId(self.run_id), type=EventType.ACTION_REQUESTED, payload={"action": {"kind": "RUN_WORKERS", "workers": ["w-1"]}}),
            OrchestrationEvent(event_id="e3", run_id=RunId(self.run_id), type=EventType.PROFILE_LEASED, payload={"profile_name": "prof-1", "worker_id": "w-1"}),
            OrchestrationEvent(event_id="e4", run_id=RunId(self.run_id), type=EventType.INVOCATION_STARTED, payload={"worker_id": "w-1", "role": "CODER"}),
            OrchestrationEvent(event_id="e5", run_id=RunId(self.run_id), type=EventType.INVOCATION_COMPLETED, payload={"worker_id": "w-1", "duration_seconds": 2.5}),
            OrchestrationEvent(event_id="e6", run_id=RunId(self.run_id), type=EventType.RUN_COMPLETED, payload={"summary": "Success!"}),
        ]
        for ev in events:
            append_trace(self.store, self.run_id, "orchestration", ev.to_dict(), event_id=ev.event_id)

        # In follow mode, stream_logs must terminate cleanly upon RUN_COMPLETED
        out = io.StringIO()
        code = stream_logs(self.run_dir, follow=True, output_stream=out)
        self.assertEqual(code, 0)
        logs = out.getvalue()
        self.assertIn("[round 1] Started", logs)
        self.assertIn("[coordinator] Decision: RUN_WORKERS", logs)
        self.assertIn("[lease] Acquired profile 'prof-1' for w-1", logs)
        self.assertIn("[worker w-1] Dispatched role: CODER", logs)
        self.assertIn("[worker w-1] Completed 2s", logs)
        self.assertIn("[run] Completed successfully", logs)

    def test_chunked_stream_and_split_utf8_buffering(self) -> None:
        with capture_attempt(
            self.store,
            self.run_id,
            prompt="Chunk test",
            kind="worker",
            worker_id="w-chunk",
            invocation_id="inv-chunk",
        ) as cap:
            assert cap is not None
            # Split a tool_call JSON event across 2 pipe chunks:
            part1 = b'{"event":"tool_call","name":"'
            part2 = b'read_file","input":{"path":"\xc3\xa9_test.py"}}\n'
            cap.write("stdout", part1)
            cap.write("stdout", part2)
            cap.finish(ModelResult(invocation_id="inv-chunk", response="Done"))

        # Milestone mode must buffer across chunks and successfully extract the tool_call
        out_m = io.StringIO()
        code_m = stream_logs(self.run_dir, output_stream=out_m)
        self.assertEqual(code_m, 0)
        self.assertIn("tool_call: read_file", out_m.getvalue())

        # Stream mode must not inject spurious newlines between chunks
        out_s = io.StringIO()
        code_s = stream_logs(self.run_dir, stream="stdout", output_stream=out_s)
        self.assertEqual(code_s, 0)
        raw_combined = out_s.getvalue()
        self.assertIn('{"event":"tool_call","name":"read_file"', raw_combined)
        # Verify UTF-8 accented character was decoded without replacement \ufffd
        self.assertNotIn("\ufffd", raw_combined)

    def test_worker_filter_isolation_and_dynamic_retries(self) -> None:
        # Worker 1 attempt 1 (fails)
        with capture_attempt(
            self.store,
            self.run_id,
            prompt="Worker 1 Attempt 1",
            kind="worker",
            worker_id="w-target",
            invocation_id="inv-target",
            attempt_number=1,
        ) as cap1:
            assert cap1 is not None
            cap1.write("stdout", b"w-target first attempt failed\n")
            cap1.finish(ModelResult(invocation_id="inv-target", status=InvocationStatus.FAILED, error="Crash 1"))

        # Worker 2 attempt (different worker)
        with capture_attempt(
            self.store,
            self.run_id,
            prompt="Worker 2",
            kind="worker",
            worker_id="w-other",
            invocation_id="inv-other",
        ) as cap2:
            assert cap2 is not None
            cap2.write("stdout", b"w-other should be excluded\n")
            cap2.finish(ModelResult(invocation_id="inv-other", response="w-other done"))

        # Worker 1 attempt 2 (retry, succeeds)
        with capture_attempt(
            self.store,
            self.run_id,
            prompt="Worker 1 Attempt 2",
            kind="worker",
            worker_id="w-target",
            invocation_id="inv-target",
            attempt_number=2,
        ) as cap3:
            assert cap3 is not None
            cap3.write("stdout", b"w-target retry succeeded\n")
            cap3.finish(ModelResult(invocation_id="inv-target", response="Recovered"))

        append_trace(
            self.store,
            self.run_id,
            "orchestration",
            {
                "type": "WORKER_FAILED",
                "payload": {"worker_id": "w-other", "error": "Other worker failed"},
            },
        )
        append_trace(
            self.store,
            self.run_id,
            "orchestration",
            {"type": "RUN_COMPLETED", "payload": {}},
        )

        # Stream logs filtering by w-target
        out = io.StringIO()
        code = stream_logs(self.run_dir, worker_id="w-target", output_stream=out)
        self.assertEqual(code, 0)
        logs = out.getvalue()
        # Must include both attempts of w-target
        self.assertIn(f"[{cap1.attempt_id}] Attempt started (worker worker=w-target)", logs)
        self.assertIn(f"[{cap3.attempt_id}] Attempt started (worker worker=w-target)", logs)
        # Must NOT include anything from w-other
        self.assertNotIn("w-other", logs)
        self.assertNotIn(cap2.attempt_id, logs)

    def test_atomic_trace_replacement_and_truncation_detection(self) -> None:
        trace_file = self.run_dir / "trace.jsonl"
        entry1 = {"schema_version": 1, "event_id": "e1", "type": "step", "payload": {}}
        trace_file.write_text(json.dumps(entry1) + "\n", encoding="utf-8")

        # Atomic replacement: new file, different inode
        new_tmp = self.run_dir / "trace.tmp"
        entry2 = {"schema_version": 1, "event_id": "e2", "type": "reset_event", "payload": {}}
        new_tmp.write_text(json.dumps(entry2) + "\n", encoding="utf-8")
        os.replace(new_tmp, trace_file)

        out = io.StringIO()
        code = stream_logs(self.run_dir, output_stream=out)
        self.assertEqual(code, 0)
        # Entry2 was read successfully
        self.assertIn("reset_event", out.getvalue())

    def test_exit_code_zero_preserved(self) -> None:
        with capture_attempt(
            self.store,
            self.run_id,
            prompt="Exit zero test",
            kind="worker",
            worker_id="w-zero",
            invocation_id="inv-zero",
        ) as cap:
            assert cap is not None
            cap.finish(ModelResult(invocation_id="inv-zero", response="Success", exit_code=0))

        view = inspect_run(self.run_dir)
        att = next(a for a in view.all_attempts if a.attempt_id == cap.attempt_id)
        self.assertEqual(att.exit_code, 0)

        text = format_inspect_text(view, attempt_id=cap.attempt_id)
        self.assertIn("exit_code: 0", text)

    def test_protocol_correction_without_round_number(self) -> None:
        with capture_attempt(
            self.store,
            self.run_id,
            prompt="Protocol note",
            kind="coordinator",
            round_number=2,
        ) as cap:
            assert cap is not None
            # Note protocol rejection without round_number in payload
            cap.note("protocol_rejected", schema_name="NextAction", error="Missing kind")
            cap.finish(ModelResult(invocation_id="inv-proto", status=InvocationStatus.FAILED))

        view = inspect_run(self.run_dir)
        cd = next((c for c in view.coordinator_decisions if c.round_number == 2), None)
        self.assertIsNotNone(cd)
        self.assertTrue(any("Schema rejected (NextAction)" in corr for corr in cd.corrections))


if __name__ == "__main__":
    unittest.main()

