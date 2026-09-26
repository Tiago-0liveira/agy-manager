"""Execution tracing tests using real local subprocesses, without model calls."""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from agym.orchestration.contracts import ModelInvocation, ModelResult, InvocationStatus
from agym.orchestration.persistence import FileRunStore
from agym.orchestration.recording import capture_attempt, current_attempt, read_trace
from agym.orchestration.runner import AntigravityRunner, AntigravitySession
from agym.orchestration.streaming import decode_response


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileRunStore(self.tmp.name)
        self.store.create_run("trace-run", "Trace a task")

    def test_concurrent_attempts_and_torn_tail_recovery(self):
        def execute(n):
            with capture_attempt(self.store, "trace-run", prompt=f"Prompt {n}", kind="worker") as cap:
                cap.write("stdout", f"output {n}".encode())
                cap.finish(ModelResult(invocation_id=f"inv-{n}"))
                return cap
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            captures = list(pool.map(execute, range(12)))
        path = self.store.run_dir("trace-run") / "trace.jsonl"
        events = list(read_trace(path))
        self.assertEqual(len({e["event_id"] for e in events}), len(events))
        self.assertEqual(sum(e["type"] == "attempt_finished" for e in events), 12)
        with path.open("ab") as f:
            f.write(b'{"partial":')
        self.assertEqual(list(read_trace(path)), events)
        captures[0].note("resumed")
        recovered = list(read_trace(path))
        self.assertEqual(recovered[-2]["type"], "trace_recovered")
        self.assertEqual(recovered[-1]["type"], "resumed")
        partial = path.parent / recovered[-2]["payload"]["partial_file"]
        self.assertEqual(partial.read_text(), '{"partial":')
        for cap in captures:
            if os.name == "posix":
                self.assertEqual((cap.directory / "prompt.txt").stat().st_mode & 0o777, 0o600)
                self.assertEqual((cap.directory / "stdout.log").stat().st_mode & 0o777, 0o600)
            else:
                self.assertTrue((cap.directory / "prompt.txt").is_file())
                self.assertTrue((cap.directory / "stdout.log").is_file())
        self.assertIsNone(current_attempt.get())

    def test_exception_is_saved_and_context_restored(self):
        with self.assertRaisesRegex(RuntimeError, "launch failed"):
            with capture_attempt(self.store, "trace-run", prompt="task", kind="worker") as cap:
                raise RuntimeError("launch failed")
        record = json.loads((cap.directory / "record.json").read_text())
        self.assertEqual(record["status"], "FAILED")
        self.assertEqual(record["error"], "launch failed")
        self.assertIsNone(current_attempt.get())

    def test_unfinished_attempt_remains_inspectable(self):
        cap = self.store.start_attempt("trace-run", prompt="Before crash", kind="worker")
        cap.write("stdout", b"partial progress")
        self.assertEqual(json.loads((cap.directory / "record.json").read_text())["status"], "RUNNING")
        self.assertEqual((cap.directory / "stdout.log").read_bytes(), b"partial progress")

    def test_stream_variants_and_failure_events(self):
        stream = '\n'.join(json.dumps(e) for e in [
            {"event": "init", "conversation_id": "conversation"},
            {"event": "tool_call", "name": "read_file", "path": "a.py"},
            {"event": "future_provider_event", "data": "retain me"},
            {"event": "result", "result": {"response": {"answer": "yes"}, "usage": {"tokens": 4}}},
        ])
        text, cid, usage = decode_response(stream)
        self.assertEqual(json.loads(text), {"answer": "yes"})
        self.assertEqual(cid, "conversation")
        self.assertEqual(usage, {"tokens": 4})
        for bad in ('{"event":"tool_call"}', '{"event":"error","error":"quota"}',
                    '{"event":"result","result":{"status":"ERROR","error":"bad"}}'):
            with self.assertRaises(ValueError):
                decode_response(bad)

    def test_decode_response_denied_actions_raises_informative_error(self):
        stream = '\n'.join([
            json.dumps({"event": "init", "conversation_id": "c1"}),
            json.dumps({"event": "step_update", "step_update": {
                "step_index": 2, "state": "ERROR", "step_type": "tool", "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "error": {"type": "TOOL_ERROR", "message": "permission check failed for command 'pwd': user denied permission"}
                }
            }}),
            "jetski: no output produced — a tool required the command permission that headless mode cannot prompt for",
            json.dumps({"event": "result", "result": {
                "conversation_id": "c1", "status": "SUCCESS", "response": "",
                "denied_actions": [{"action": "command", "display_name": "RunCommand"}]
            }})
        ])
        with self.assertRaises(ValueError) as ctx:
            decode_response(stream)
        err = str(ctx.exception)
        self.assertIn("Tool permission denied", err)
        self.assertIn("RunCommand", err)
        self.assertIn("permission check failed", err)

    def test_decode_response_empty_after_tool_error(self):
        stream = '\n'.join([
            json.dumps({"event": "init", "conversation_id": "c2"}),
            json.dumps({"event": "step_update", "step_update": {
                "step_index": 1, "state": "ERROR", "step_type": "tool",
                "tool_info": {"error": "Internal tool timeout"}
            }}),
            json.dumps({"event": "result", "result": {
                "conversation_id": "c2", "status": "SUCCESS", "response": ""
            }})
        ])
        with self.assertRaises(ValueError) as ctx:
            decode_response(stream)
        self.assertIn("Empty response after tool error", str(ctx.exception))
        self.assertIn("Internal tool timeout", str(ctx.exception))

    def test_decode_response_empty_response_standard(self):
        stream = '\n'.join([
            json.dumps({"event": "init", "conversation_id": "c3"}),
            json.dumps({"event": "result", "result": {
                "conversation_id": "c3", "status": "SUCCESS", "response": ""
            }})
        ])
        with self.assertRaises(ValueError) as ctx:
            decode_response(stream)
        self.assertEqual(str(ctx.exception), "Empty response in final result event")


@unittest.skipUnless(os.name == "posix", "Executable fixture uses a POSIX shebang")
class SubprocessTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FileRunStore(self.root / "runs")
        self.store.create_run("run", "Trace subprocess")

    def script(self, body):
        path = self.root / "fake-agy"
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    def invocation(self, timeout=5):
        return ModelInvocation(invocation_id="inv", run_id="run", worker_id="worker",
                               role="GENERAL", prompt="Do work", stall_timeout_seconds=timeout)

    async def wait_for_output(self, capture):
        async def wait():
            path = capture.directory / "stdout.log"
            while not path.exists() or not path.stat().st_size:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=3)

    async def test_output_visible_before_completion_and_tool_events_retained(self):
        gate = self.root / "continue"
        script = self.script('''import json, sys, time
from pathlib import Path
print(json.dumps({"event":"tool_call", "name":"read_file", "path":"a.py"}), flush=True)
sys.stderr.buffer.write(b"diagnostic\\n"); sys.stderr.flush()
''' + f"while not Path({str(gate)!r}).exists(): time.sleep(0.01)\n" + '''print(json.dumps({"event":"result", "response":"Done", "usage":{"tokens":3}}), flush=True)
''')
        runner = AntigravityRunner(agy_path=script)
        with capture_attempt(self.store, "run", prompt="Do work", kind="worker") as cap:
            task = asyncio.create_task(runner.run_async(self.invocation()))
            try:
                await self.wait_for_output(cap)
                self.assertFalse(task.done())
                self.assertIn("tool_call", (cap.directory / "stdout.log").read_text())
            finally:
                gate.touch()
            result = await task
            cap.finish(result)
        self.assertEqual(result.response, "Done")
        self.assertEqual(result.usage, {"tokens": 3})
        self.assertEqual((cap.directory / "stderr.log").read_text(), "diagnostic\n")
        events = [e for e in read_trace(self.store.run_dir("run") / "trace.jsonl") if e["type"] == "output"]
        for event in events:
            p = event["payload"]
            data = (cap.directory / (p["stream"] + ".log")).read_bytes()
            self.assertEqual(len(data[p["offset"]:p["offset"] + p["length"]]), p["length"])

    async def test_stall_keeps_partial_stdout_and_stderr(self):
        script = self.script('''import sys,time
print('{"event":"tool_call","name":"slow_tool"}', flush=True)
print('waiting for backend', file=sys.stderr, flush=True)
time.sleep(30)
''')
        runner = AntigravityRunner(agy_path=script)
        with capture_attempt(self.store, "run", prompt="slow", kind="worker") as cap:
            result = await runner.run_async(self.invocation(timeout=0.3))
            cap.finish(result)
        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("STALLED", result.error)
        self.assertIn("slow_tool", (cap.directory / "stdout.log").read_text())
        self.assertIn("waiting", (cap.directory / "stderr.log").read_text())
        self.assertEqual(runner.active_runs(), [])

    async def test_cancellation_keeps_partial_output(self):
        script = self.script("import time\nprint('progress', flush=True)\ntime.sleep(30)\n")
        runner = AntigravityRunner(agy_path=script)
        cap = None
        async def execute():
            nonlocal cap
            with capture_attempt(self.store, "run", prompt="cancel", kind="worker") as cap:
                await runner.run_async(self.invocation())
        task = asyncio.create_task(execute())
        while cap is None:
            await asyncio.sleep(0)
        await self.wait_for_output(cap)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(json.loads((cap.directory / "record.json").read_text())["status"], "INTERRUPTED")
        self.assertIn("progress", (cap.directory / "stdout.log").read_text())
        self.assertEqual(runner.active_runs(), [])

    async def test_nonzero_exit_preserves_both_streams(self):
        script = self.script("import sys\nprint('partial response')\nprint('backend failure',file=sys.stderr)\nsys.exit(7)\n")
        with capture_attempt(self.store, "run", prompt="fail", kind="worker") as cap:
            result = await AntigravityRunner(agy_path=script).run_async(self.invocation())
            cap.finish(result)
        self.assertEqual(result.exit_code, 7)
        self.assertIn("partial response", (cap.directory / "stdout.log").read_text())
        self.assertIn("backend failure", (cap.directory / "stderr.log").read_text())

    async def test_coordinator_timeout_keeps_incomplete_json_line(self):
        script = self.script("import sys,time\nfor line in sys.stdin:\n    sys.stdout.write('{\"event\":'); sys.stdout.flush()\n    time.sleep(30)\n")
        session = await asyncio.to_thread(AntigravitySession, agy_path=script)
        try:
            with capture_attempt(self.store, "run", prompt="timeout", kind="coordinator") as cap:
                result = await asyncio.to_thread(session.send, "timeout", 0.3)
                cap.finish(result)
            self.assertEqual(result.status, InvocationStatus.FAILED)
            self.assertEqual((cap.directory / "stdout.log").read_bytes(), b'{"event":')
        finally:
            await asyncio.to_thread(session.close)

    async def test_structured_response_preserves_usage_and_conversation(self):
        script = self.script("print('{\"event\":\"result\",\"conversation_id\":\"conv-1\",\"usage\":{\"tokens\":4},\"response\":{\"answer\":\"yes\"}}')\n")
        invocation = self.invocation()
        invocation.output_schema = {"type": "object", "required": ["answer"]}
        with capture_attempt(self.store, "run", prompt="schema", kind="worker") as cap:
            result = await AntigravityRunner(agy_path=script).run_async(invocation)
            cap.finish(result)
        self.assertEqual(result.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(result.structured_data, {"answer": "yes"})
        self.assertEqual(result.usage, {"tokens": 4})
        self.assertEqual(result.conversation_id, "conv-1")

    async def test_persistent_session_drains_large_stderr_and_preserves_turns(self):
        script = self.script('''import sys,json
for line in sys.stdin:
    sys.stderr.write('x' * 200000); sys.stderr.flush()
    print(json.dumps({"event":"tool_call","name":"read_file"}), flush=True)
    print(json.dumps({"event":"result","response":"answer"}), flush=True)
''')
        session = await asyncio.to_thread(AntigravitySession, agy_path=script)
        captures = []
        try:
            for n in range(2):
                with capture_attempt(self.store, "run", prompt=f"turn {n}", kind="coordinator") as cap:
                    result = await asyncio.to_thread(session.send, f"turn {n}", 5)
                    cap.finish(result)
                    captures.append(cap)
                self.assertEqual(result.response, "answer")
        finally:
            await asyncio.to_thread(session.close)
        self.assertNotEqual(captures[0].attempt_id, captures[1].attempt_id)
        self.assertEqual(sum((c.directory / "stderr.log").stat().st_size for c in captures), 400000)
        for cap in captures:
            self.assertIn("tool_call", (cap.directory / "stdout.log").read_text())
