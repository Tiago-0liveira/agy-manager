"""Comprehensive test suite for AntigravityProviderAdapter.

Covers:
- Capability inspection and ProviderCapabilities validation.
- Subprocess argv formatting adhering to Go flag parser rules (["--print", prompt] at the end).
- Three-tier non-billable auth probe (ProfileStore, .gemini directory, agy models probe).
- Model discovery: parsing tab-separated stdout and isolating stderr spinner frames.
- Output section extraction: direct JSON, markdown code fence, markdown headings, and fallback.
- Asynchronous NDJSON stream parsing: init, step_update deltas, result terminal event.
- Plain text delta streaming fallback.
- Usage metrics and conversation handle extraction.
- Turn error mapping (NEEDS_AUTH, RATE_LIMITED, PERMISSION_BLOCKED, MALFORMED_OUTPUT, TIMEOUT).
- Pre-cancellation, in-flight cancellation, and cancellation idempotency.
- Process group termination and child process reaping without zombie leaks.
- Startup crash reconciliation with OS PID and start time validation preventing PID reuse traps.
- Orphaned process cleanup without calling waitpid on non-children.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agym.council.models import (
    AccountAuthStatus,
    AttemptReconciliation,
    AttemptSnapshot,
    AttemptStatus,
    CouncilOutputSections,
    PermissionPolicy,
    TurnAllowance,
    TurnEvent,
    TurnRequest,
    TurnResult,
    TurnStatus,
)
from agym.council.providers.antigravity import (
    AntigravityProviderAdapter,
    build_turn_argv,
    extract_output_sections,
    get_process_start_time,
    is_pid_alive,
    sanitize_auth_error,
    terminate_child_process,
    terminate_pid_tree,
)
from agym.profiles import Profile, ProfileSettings, ProfileStore


def _create_mock_cli_script(path: Path, script_body: str) -> Path:
    """Create an executable Python script acting as a mock CLI."""
    content = f"#!{sys.executable}\nimport sys\n" + script_body
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


class TestAntigravityCapabilitiesAndArgv(unittest.IsolatedAsyncioTestCase):
    """Test capabilities declaration and CLI argv formatting."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(config_root=root / "config", data_root=root / "data")
        self.adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path="/bin/agy")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_capabilities_declaration(self) -> None:
        caps = await self.adapter.capabilities("any-ref")
        self.assertEqual(caps.provider_name, "antigravity")
        self.assertTrue(caps.supports_streaming)
        self.assertTrue(caps.supports_structured_output)
        self.assertTrue(caps.supports_resume)
        self.assertTrue(caps.supports_cancellation)
        self.assertFalse(caps.supports_tools)
        self.assertTrue(caps.supports_usage_metrics)
        self.assertTrue(caps.supports_model_discovery)
        self.assertFalse(caps.enforces_workspace_isolation)

        # Shorthand compatibility
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.resume_conversation)
        self.assertTrue(caps.cancellation)
        self.assertFalse(caps.tool_execution)

    def test_build_turn_argv_order_and_flags(self) -> None:
        # 1. Base case: prompt at end
        argv = build_turn_argv(
            agy_path=Path("/usr/local/bin/agy"),
            prompt="Analyze architectural options",
            model="gemini-2.5-pro",
            conversation_handle="conv-12345",
            dangerously_skip_permissions=True,
        )
        self.assertEqual(argv[0], "/usr/local/bin/agy")
        self.assertEqual(argv[1:3], ["--output-format", "stream-json"])
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-2.5-pro")
        self.assertIn("--conversation", argv)
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-12345")
        self.assertIn("--dangerously-skip-permissions", argv)

        # CRITICAL Go flag rule: --print must be the last flag, followed immediately by prompt
        self.assertEqual(argv[-2], "--print")
        self.assertEqual(argv[-1], "Analyze architectural options")

    def test_build_turn_argv_omitted_flags(self) -> None:
        argv = build_turn_argv(
            agy_path=Path("/bin/agy"),
            prompt="Hello World",
            model=None,
            conversation_handle=None,
            dangerously_skip_permissions=False,
        )
        self.assertEqual(argv, ["/bin/agy", "--output-format", "stream-json", "--print", "Hello World"])
        self.assertNotIn("--model", argv)
        self.assertNotIn("--conversation", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)


class TestAntigravityAuthProbe(unittest.IsolatedAsyncioTestCase):
    """Test three-tier non-billable account verification probe."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(config_root=self.root / "config", data_root=self.root / "data")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_empty_account_ref_is_unavailable(self) -> None:
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path="/bin/agy")
        st = await adapter.check_account("")
        self.assertEqual(st.status, AccountAuthStatus.UNAVAILABLE)
        self.assertIn("empty", st.message.lower())

    async def test_missing_profile_is_unavailable(self) -> None:
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path="/bin/agy")
        st = await adapter.check_account("nonexistent_prof")
        self.assertEqual(st.status, AccountAuthStatus.UNAVAILABLE)
        self.assertIn("not found", st.message.lower())

    async def test_missing_cli_binary_is_unavailable(self) -> None:
        prof = self.store.create("test_prof")
        # Adapter with nonexistent binary
        adapter = AntigravityProviderAdapter(
            profile_store=self.store, agy_path=self.bin_dir / "nonexistent_agy"
        )
        st = await adapter.check_account(prof.name)
        self.assertEqual(st.status, AccountAuthStatus.UNAVAILABLE)
        self.assertIn("not found", st.message.lower())

    async def test_missing_gemini_dir_needs_login(self) -> None:
        prof = self.store.create("test_prof")
        # Existing dummy binary
        dummy_bin = _create_mock_cli_script(self.bin_dir / "agy", "sys.exit(0)\n")
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=dummy_bin)

        st = await adapter.check_account(prof.name)
        self.assertEqual(st.status, AccountAuthStatus.NEEDS_LOGIN)
        self.assertIn("agym setup", st.message)
        self.assertIn("credentials not found", st.message.lower())

    async def test_successful_probe_returns_ready_with_version(self) -> None:
        prof = self.store.create("test_prof")
        # Populate .gemini dir with credentials
        gemini_dir = prof.home / ".gemini"
        gemini_dir.mkdir(parents=True)
        (gemini_dir / "token.json").write_text("{}", encoding="utf-8")

        # Mock binary that exits 0 for 'models' and prints version for '--version'
        script = """
if len(sys.argv) > 1 and sys.argv[1] == "--version":
    print("antigravity 1.2.7")
    sys.exit(0)
elif len(sys.argv) > 1 and sys.argv[1] == "models":
    print("gemini-2.5-pro\\tGemini 2.5 Pro")
    sys.exit(0)
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        st = await adapter.check_account(prof.name)
        self.assertEqual(st.status, AccountAuthStatus.READY)
        self.assertEqual(st.cli_version, "antigravity 1.2.7")
        self.assertIn("authenticated and ready", st.message)

    async def test_failed_probe_returns_needs_login_sanitized(self) -> None:
        prof = self.store.create("test_prof")
        gemini_dir = prof.home / ".gemini"
        gemini_dir.mkdir(parents=True)
        (gemini_dir / "token.json").write_text("{}", encoding="utf-8")

        # Mock binary exiting with code 1 and auth error
        script = """
if len(sys.argv) > 1 and sys.argv[1] == "models":
    sys.stderr.write("Fetching available models...\\nError: Please sign in to view available models.\\n")
    sys.exit(1)
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        st = await adapter.check_account(prof.name)
        self.assertEqual(st.status, AccountAuthStatus.NEEDS_LOGIN)
        self.assertIn("sign in", st.message.lower())

    def test_sanitize_auth_error_redacts_secrets(self) -> None:
        raw = "Error: Bearer ya29.a0AfH6_SECRET_TOKEN_XYZ invalid. code=oauth_secret_123"
        sanitized = sanitize_auth_error(raw, "prof-1")
        self.assertNotIn("ya29.a0AfH6_SECRET_TOKEN_XYZ", sanitized)
        self.assertNotIn("oauth_secret_123", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    async def test_check_account_timeout_returns_unknown(self) -> None:
        prof = self.store.create("test_timeout_prof")
        gemini_dir = prof.home / ".gemini"
        gemini_dir.mkdir(parents=True)
        (gemini_dir / "token.json").write_text("{}", encoding="utf-8")

        dummy_bin = _create_mock_cli_script(self.bin_dir / "agy", "sys.exit(0)\n")
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=dummy_bin)

        import unittest.mock

        async def _mock_wait_for(fut, timeout):
            # Await future if it's a coroutine to prevent unawaited warning
            if asyncio.iscoroutine(fut):
                fut.close()
            raise asyncio.TimeoutError()

        with unittest.mock.patch("asyncio.wait_for", side_effect=_mock_wait_for):
            st = await adapter.check_account(prof.name)
            self.assertEqual(st.status, AccountAuthStatus.UNKNOWN)
            self.assertIn("timed out", st.message.lower())


class TestAntigravityModelDiscovery(unittest.IsolatedAsyncioTestCase):
    """Test model catalog discovery and stdout/stderr separation."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(config_root=self.root / "config", data_root=self.root / "data")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.prof = self.store.create("prof1")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_discover_models_parses_tsv_and_enriches(self) -> None:
        script = """
import sys
# Write spinner frames to stderr
sys.stderr.write("\\r\\033[K⠋ Fetching available models...\\n")
# Write clean tab-separated models to stdout
sys.stdout.write("gemini-2.5-pro\\tGemini 2.5 Pro (Thinking)\\n")
sys.stdout.write("claude-3-5-sonnet\\tClaude 3.5 Sonnet\\n")
sys.stdout.write("custom-fast\\tCustom Fast Model\\n")
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        models = await adapter.discover_models("prof1")
        self.assertEqual(len(models), 3)

        m0 = models[0]
        self.assertEqual(m0.id, "gemini-2.5-pro")
        self.assertEqual(m0.display_name, "Gemini 2.5 Pro (Thinking)")
        self.assertEqual(m0.context_window, 1000000)
        self.assertIn("thinking", m0.capabilities)
        self.assertIn("streaming", m0.capabilities)

        m1 = models[1]
        self.assertEqual(m1.id, "claude-3-5-sonnet")
        self.assertEqual(m1.context_window, 200000)

        m2 = models[2]
        self.assertEqual(m2.id, "custom-fast")
        self.assertEqual(m2.context_window, 128000)

    async def test_discover_models_fails_returns_empty_list(self) -> None:
        script = """
import sys
sys.stderr.write("Error: flags provided but not defined: -output-format\\n")
sys.exit(1)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        models = await adapter.discover_models("prof1")
        self.assertEqual(models, [])

    async def test_discover_models_invalid_account_returns_empty_list(self) -> None:
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path="/bin/agy")
        self.assertEqual(await adapter.discover_models(""), [])
        self.assertEqual(await adapter.discover_models("nonexistent"), [])

    async def test_discover_models_whitespace_and_missing_name(self) -> None:
        script = """
import sys
sys.stdout.write("only-id\\n")
sys.stdout.write("multi-tab\\tDisplay Name\\tExtra Tab Ignored\\n")
sys.stdout.write("   \\n")
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        models = await adapter.discover_models("prof1")
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0].id, "only-id")
        self.assertEqual(models[0].display_name, "only-id")
        self.assertEqual(models[1].id, "multi-tab")
        self.assertEqual(models[1].display_name, "Display Name\tExtra Tab Ignored")


class TestAntigravityOutputSectionExtraction(unittest.TestCase):
    """Test multi-strategy output section extractor."""

    def test_strategy_1_direct_json(self) -> None:
        text = """
{
  "findings": "Primary findings here.",
  "evidence_or_assumptions": "Benchmark citations.",
  "uncertainties": "Minor latency variance.",
  "next_action": "Approve stage."
}
"""
        sections, data = extract_output_sections(text)
        self.assertIsNotNone(sections)
        self.assertIsNotNone(data)
        assert sections is not None
        self.assertEqual(sections.findings, "Primary findings here.")
        self.assertEqual(sections.evidence_or_assumptions, "Benchmark citations.")
        self.assertEqual(sections.uncertainties, "Minor latency variance.")
        self.assertEqual(sections.next_action, "Approve stage.")
        self.assertEqual(sections.validate_required(["findings", "next_action"]), [])

    def test_strategy_2_markdown_fenced_code_block(self) -> None:
        text = """
Here is my analysis:

```json
{
  "findings": "Fenced findings content.",
  "evidence_or_assumptions": "Fenced evidence.",
  "uncertainties": "None.",
  "next_action": "Deploy immediately."
}
```

Hope this helps!
"""
        sections, data = extract_output_sections(text)
        self.assertIsNotNone(sections)
        assert sections is not None
        self.assertEqual(sections.findings, "Fenced findings content.")
        self.assertEqual(sections.next_action, "Deploy immediately.")

    def test_strategy_3_markdown_headings(self) -> None:
        text = """
## Findings
We discovered a viable solution using cache layers.

## Evidence or Assumptions
Workload logs show 90% read queries.

## Uncertainties
Cache invalidation under partition.

## Next Action
Proceed to implementation slice.
"""
        sections, data = extract_output_sections(text)
        self.assertIsNotNone(sections)
        assert sections is not None
        self.assertIn("viable solution", sections.findings)
        self.assertIn("Workload logs", sections.evidence_or_assumptions)
        self.assertIn("Cache invalidation", sections.uncertainties)
        self.assertIn("Proceed to implementation", sections.next_action)

    def test_strategy_4_fallback_unstructured_text(self) -> None:
        text = "This is a simple plain text response without JSON or headers."
        sections, data = extract_output_sections(text)
        self.assertIsNotNone(sections)
        assert sections is not None
        self.assertEqual(sections.findings, text)
        self.assertEqual(sections.evidence_or_assumptions, "")
        # Missing required sections validation
        missing = sections.validate_required(["findings", "evidence_or_assumptions"])
        self.assertEqual(missing, ["evidence_or_assumptions"])


class TestAntigravityTurnExecution(unittest.IsolatedAsyncioTestCase):
    """Test real subprocess execution, streaming parser, and error mapping."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(config_root=self.root / "config", data_root=self.root / "data")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.prof = self.store.create("prof1")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_run_turn_ndjson_streaming_success(self) -> None:
        # Mock CLI script emitting typed NDJSON events
        script = """
import sys, json, time

# 1. init event
init_event = {"type": "init", "conversation_id": "conv-ndjson-999"}
sys.stdout.write(json.dumps(init_event) + "\\n")
sys.stdout.flush()

# 2. step_update deltas
for chunk in ["{\\n  \\"findings\\": \\"Step 1 OK\\",\\n", "  \\"evidence_or_assumptions\\": \\"Verified\\",\\n", "  \\"uncertainties\\": \\"None\\",\\n", "  \\"next_action\\": \\"Proceed\\"\\n}"]:
    step_event = {"type": "step_update", "delta": chunk}
    sys.stdout.write(json.dumps(step_event) + "\\n")
    sys.stdout.flush()

# 3. terminal result event
res_event = {
    "type": "result",
    "result": {
        "status": "SUCCESS",
        "conversation_id": "conv-ndjson-999",
        "usage": {"input_tokens": 150, "output_tokens": 80, "total_tokens": 230}
    }
}
sys.stdout.write(json.dumps(res_event) + "\\n")
sys.stdout.flush()
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        req = TurnRequest(
            attempt_id="att-ndjson-1",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w-ndjson",
            account_ref="prof1",
            model="gemini-2.5-pro",
            prompt="Plan deployment",
        )

        events: list[TurnEvent | TurnResult] = []
        async for item in adapter.run_turn(req):
            events.append(item)

        # First event is started
        self.assertIsInstance(events[0], TurnEvent)
        self.assertEqual(events[0].event_type, "started")
        self.assertEqual(events[0].sequence, 1)

        # Deltas emitted
        deltas = [e for e in events if isinstance(e, TurnEvent) and e.event_type == "delta"]
        self.assertGreaterEqual(len(deltas), 4)

        # Terminal event is TurnResult
        res = events[-1]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.SUCCESS)
        self.assertEqual(res.conversation_handle, "conv-ndjson-999")
        self.assertIsNotNone(res.parsed_sections)
        assert res.parsed_sections is not None
        self.assertEqual(res.parsed_sections.findings, "Step 1 OK")
        self.assertEqual(res.parsed_sections.next_action, "Proceed")
        self.assertEqual(res.usage.get("total_tokens"), 230)
        self.assertEqual(res.usage.get("prompt_tokens"), 150)
        self.assertEqual(res.usage.get("completion_tokens"), 80)

    async def test_run_turn_plain_text_deltas(self) -> None:
        # Mock CLI script emitting plain text (fallback)
        script = """
import sys
sys.stdout.write("## Findings\\nPlain text findings.\\n\\n")
sys.stdout.write("## Evidence or Assumptions\\nAssumed true.\\n\\n")
sys.stdout.write("## Uncertainties\\nUnknowns.\\n\\n")
sys.stdout.write("## Next Action\\nNext step.\\n")
sys.stdout.flush()
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        req = TurnRequest(
            attempt_id="att-text-1",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w-text",
            account_ref="prof1",
            model="gemini-2.5-flash",
            prompt="Analyze logs",
        )

        events: list[TurnEvent | TurnResult] = []
        async for item in adapter.run_turn(req):
            events.append(item)

        res = events[-1]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.SUCCESS)
        assert res.parsed_sections is not None
        self.assertIn("Plain text findings", res.parsed_sections.findings)

    async def test_run_turn_missing_required_sections_malformed(self) -> None:
        script = """
import sys
sys.stdout.write("{\\"findings\\": \\"Only findings provided\\"}\\n")
sys.stdout.flush()
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        req = TurnRequest(
            attempt_id="att-malformed-1",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof1",
            model="gemini-2.5-flash",
            prompt="Analyze",
            required_sections=["findings", "evidence_or_assumptions", "uncertainties", "next_action"],
        )

        events = [e async for e in adapter.run_turn(req)]
        res = events[-1]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.MALFORMED_OUTPUT)
        self.assertIn("Missing required output sections", res.error_message or "")

    async def test_run_turn_empty_prompt_rejected(self) -> None:
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path="/bin/agy")
        req = TurnRequest(
            attempt_id="att-empty-1",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof1",
            model="gemini-2.5-flash",
            prompt="   ",
        )

        events = [e async for e in adapter.run_turn(req)]
        res = events[-1]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.MALFORMED_OUTPUT)
        self.assertEqual(res.error_code, "EMPTY_PROMPT")

    async def test_run_turn_exit_code_auth_error(self) -> None:
        script = """
import sys
sys.stderr.write("Error: Please sign in to view available models.\\n")
sys.exit(1)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        req = TurnRequest(
            attempt_id="att-err-auth",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof1",
            model="gemini-2.5-flash",
            prompt="Test",
        )

        events = [e async for e in adapter.run_turn(req)]
        res = events[-1]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.NEEDS_AUTH)

    async def test_run_turn_exit_code_rate_limit(self) -> None:
        script = """
import sys
sys.stderr.write("RESOURCE_EXHAUSTED: Rate limit exceeded for model gemini-2.5-pro\\n")
sys.exit(1)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        req = TurnRequest(
            attempt_id="att-err-rate",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof1",
            model="gemini-2.5-flash",
            prompt="Test",
        )

        events = [e async for e in adapter.run_turn(req)]
        res = events[-1]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.RATE_LIMITED)

    async def test_run_turn_timeout_terminates_child(self) -> None:
        # Script sleeps for 10 seconds
        script = """
import sys, time
time.sleep(10.0)
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        req = TurnRequest(
            attempt_id="att-timeout-1",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof1",
            model="gemini-2.5-flash",
            prompt="Test",
            allowance=TurnAllowance(timeout_seconds=1),
        )

        t0 = time.time()
        events = [e async for e in adapter.run_turn(req)]
        elapsed = time.time() - t0

        self.assertLess(elapsed, 4.0)
        res = events[-1]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.TIMEOUT)

    async def test_run_turn_profile_settings_dangerously_skip_permissions(self) -> None:
        prof_dsp = self.store.create(
            "prof_dsp", settings=ProfileSettings(dangerously_skip_permissions=True)
        )
        script = """
import sys, json
assert "--dangerously-skip-permissions" in sys.argv
res = {
    "type": "result",
    "result": {
        "status": "SUCCESS",
        "response": "{\\"findings\\": \\"OK\\", \\"evidence_or_assumptions\\": \\"OK\\", \\"uncertainties\\": \\"OK\\", \\"next_action\\": \\"OK\\"}"
    }
}
sys.stdout.write(json.dumps(res) + "\\n")
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)
        req = TurnRequest(
            attempt_id="att-dsp-test",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w-dsp",
            account_ref=prof_dsp.name,
            model="gemini-2.5-flash",
            prompt="Analyze permissions",
        )
        events = [e async for e in adapter.run_turn(req)]
        self.assertEqual(events[-1].status, TurnStatus.SUCCESS)


class TestAntigravityCancellationAndLifecycle(unittest.IsolatedAsyncioTestCase):
    """Test cancellation idempotency, process group signaling, and child reaping."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(config_root=self.root / "config", data_root=self.root / "data")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.prof = self.store.create("prof1")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_pre_cancellation(self) -> None:
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path="/bin/agy")
        await adapter.cancel("att-pre-cancel")

        req = TurnRequest(
            attempt_id="att-pre-cancel",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof1",
            model="gemini-2.5-pro",
            prompt="Analyze",
        )

        events = [e async for e in adapter.run_turn(req)]
        self.assertEqual(len(events), 1)
        res = events[0]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.CANCELLED)

    async def test_in_flight_cancellation_terminates_subprocess(self) -> None:
        # Script sleeps 10s
        script = """
import sys, time
sys.stdout.write("Starting...\\n")
sys.stdout.flush()
time.sleep(10.0)
sys.exit(0)
"""
        mock_bin = _create_mock_cli_script(self.bin_dir / "agy", script)
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path=mock_bin)

        req = TurnRequest(
            attempt_id="att-cancel-inflight",
            run_id="run-1",
            stage_id="stage-1",
            worker_id="w1",
            account_ref="prof1",
            model="gemini-2.5-pro",
            prompt="Analyze",
        )

        async def _run_and_collect() -> list[TurnEvent | TurnResult]:
            items = []
            async for item in adapter.run_turn(req):
                items.append(item)
            return items

        task = asyncio.create_task(_run_and_collect())

        # Wait until started
        while "att-cancel-inflight" not in adapter.active_processes:
            await asyncio.sleep(0.05)

        proc = adapter.active_processes["att-cancel-inflight"]
        pid = proc.pid
        self.assertTrue(is_pid_alive(pid))

        # Cancel attempt
        await adapter.cancel("att-cancel-inflight")
        items = await asyncio.wait_for(task, timeout=5.0)

        res = items[-1]
        self.assertIsInstance(res, TurnResult)
        self.assertEqual(res.status, TurnStatus.CANCELLED)

        # Verify child process was reaped and is not running
        self.assertFalse(is_pid_alive(pid))
        self.assertIsNotNone(proc.returncode)

    async def test_cancellation_idempotency(self) -> None:
        adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path="/bin/agy")
        # Calling cancel multiple times on non-existent or finished attempts does not raise
        await adapter.cancel("att-unknown")
        await adapter.cancel("att-unknown")
        await adapter.cancel("att-unknown")
        self.assertIn("att-unknown", adapter.cancelled_attempts)


class TestAntigravityCrashReconciliation(unittest.IsolatedAsyncioTestCase):
    """Test startup crash recovery, PID reuse safeguards, and orphan termination."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(config_root=self.root / "config", data_root=self.root / "data")
        self.adapter = AntigravityProviderAdapter(profile_store=self.store, agy_path="/bin/agy")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_reconcile_completed_and_cancelled(self) -> None:
        # Register completed and cancelled
        self.adapter.completed_attempts["att-c1"] = TurnResult(
            attempt_id="att-c1",
            status=TurnStatus.SUCCESS,
        )
        self.adapter.cancelled_attempts.add("att-can")

        snaps = [
            AttemptSnapshot(
                attempt_id="att-c1",
                run_id="r1",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                status=AttemptStatus.RUNNING,
            ),
            AttemptSnapshot(
                attempt_id="att-can",
                run_id="r1",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                status=AttemptStatus.RUNNING,
            ),
            AttemptSnapshot(
                attempt_id="att-none-pid",
                run_id="r1",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                pid=None,
                status=AttemptStatus.CLAIMED,
            ),
            AttemptSnapshot(
                attempt_id="att-dead-pid",
                run_id="r1",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                pid=9999999,
                status=AttemptStatus.RUNNING,
            ),
        ]

        reconciled = await self.adapter.reconcile(snaps)
        self.assertEqual(len(reconciled), 4)

        self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.SUCCEEDED)
        self.assertEqual(reconciled[1].reconciled_status, AttemptStatus.CANCELLED)
        self.assertEqual(reconciled[2].reconciled_status, AttemptStatus.UNKNOWN)
        self.assertEqual(reconciled[3].reconciled_status, AttemptStatus.UNKNOWN)

    async def test_reconcile_pid_reuse_safeguard(self) -> None:
        # Spawn a real long-running process (e.g. sleep)
        proc = await asyncio.create_subprocess_exec("sleep", "20")
        pid = proc.pid

        try:
            real_start_time = get_process_start_time(pid) or time.time()
            # Forged snapshot with mismatched start time (> 2.0s diff)
            fake_start_time = real_start_time - 500.0

            snap = AttemptSnapshot(
                attempt_id="att-reused-pid",
                run_id="r1",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                pid=pid,
                process_start_time=fake_start_time,
                status=AttemptStatus.RUNNING,
            )

            reconciled = await self.adapter.reconcile([snap])
            self.assertEqual(len(reconciled), 1)
            # Reconciled to UNKNOWN due to PID reuse
            self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.UNKNOWN)
            self.assertIn("PID reuse detected", reconciled[0].reason)

            # Crucial: the process must NOT have been killed because it belongs to someone else!
            self.assertTrue(is_pid_alive(pid))
        finally:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

    async def test_reconcile_orphaned_worker_process_terminated(self) -> None:
        # Spawn an orphaned worker process in a new session
        proc = await asyncio.create_subprocess_exec("sleep", "20", start_new_session=True)
        pid = proc.pid

        try:
            real_start_time = get_process_start_time(pid) or time.time()
            snap = AttemptSnapshot(
                attempt_id="att-orphan",
                run_id="r1",
                stage_id="s1",
                worker_id="w1",
                account_ref="p1",
                pid=pid,
                process_start_time=real_start_time,
                status=AttemptStatus.RUNNING,
            )

            reconciled = await self.adapter.reconcile([snap])
            self.assertEqual(len(reconciled), 1)
            self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.UNKNOWN)
            self.assertIn("Orphaned worker process", reconciled[0].reason)

            # Wait a brief moment to confirm SIGKILL terminated it
            await asyncio.sleep(0.1)
            self.assertFalse(is_pid_alive(pid))
        finally:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

    async def test_reconcile_completed_failed_attempt(self) -> None:
        self.adapter.completed_attempts["att-failed"] = TurnResult(
            attempt_id="att-failed",
            status=TurnStatus.RATE_LIMITED,
            error_message="Rate limit exceeded",
        )
        snap = AttemptSnapshot(
            attempt_id="att-failed",
            run_id="r1",
            stage_id="s1",
            worker_id="w1",
            account_ref="p1",
            status=AttemptStatus.RUNNING,
        )
        reconciled = await self.adapter.reconcile([snap])
        self.assertEqual(reconciled[0].reconciled_status, AttemptStatus.FAILED)
        assert reconciled[0].result is not None
        self.assertEqual(reconciled[0].result.status, TurnStatus.RATE_LIMITED)


class TestProcessInspectionUtilities(unittest.TestCase):
    """Test zero-dependency PID inspection and start time utilities."""

    def test_current_process_liveness_and_start_time(self) -> None:
        pid = os.getpid()
        self.assertTrue(is_pid_alive(pid))
        self.assertFalse(is_pid_alive(-1))
        self.assertFalse(is_pid_alive(None))

        st = get_process_start_time(pid)
        self.assertIsNotNone(st)
        assert st is not None
        self.assertGreater(st, 0)
        # Should be reasonable epoch timestamp (e.g. > year 2020)
        self.assertGreater(st, 1577836800.0)

    def test_proc_stat_field_parser_with_comm_whitespace(self) -> None:
        # Simulate Linux /proc stat line where comm contains spaces and parentheses
        sample_stat = "12345 (agy worker (sub)) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 987654 20 21"
        r_idx = sample_stat.rfind(")")
        fields = sample_stat[r_idx + 2 :].split()
        self.assertEqual(fields[0], "S")  # field 3 (state)
        self.assertEqual(int(fields[19]), 987654)  # field 22 (starttime)


if __name__ == "__main__":
    unittest.main()
