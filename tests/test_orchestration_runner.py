"""Tests for the AGYM orchestration runner and strategy subsystem.

Tests conform to agent rules:
- Zero real Gemini/Antigravity quota. All subprocess execution is mocked.
- Tests ModelRunner and ModelSession independence.
- Tests strategies isolation.
- Covers argv construction, profile env usage, strategy mapping, standard/high-effort/boost,
  structured JSON, invalid JSON, conversation ID capture, timeout, cancellation,
  nonzero exit, empty response, persistent session multi-turn, close, and FakeModelRunner.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agym.orchestration.contracts import (
    ConversationId,
    ExecutionStrategy,
    FailureClass,
    InvocationId,
    InvocationStatus,
    ModelInvocation,
    ModelResult,
    ModelRunner,
    ModelSession,
    RunId,
    WorkerRole,
    WorkspaceMode,
)
from agym.orchestration.runner import (
    AntigravityRunner,
    AntigravitySession,
    FakeModelRunner,
    FakeModelSession,
    classify_failure,
    is_auth_failure,
)
from agym.orchestration.strategies import (
    ExecutionSettings,
    UnsupportedStrategyError,
    get_execution_settings,
    get_strategy_args,
    is_strategy_supported,
    map_strategy,
)
from agym.profiles import Profile, ProfileSettings, ProfileStore


class TestStrategyMapping(unittest.TestCase):
    """Tests for agym.orchestration.strategies."""

    def test_standard_strategy_mapping(self) -> None:
        settings = get_execution_settings(ExecutionStrategy.STANDARD)
        self.assertTrue(settings.is_supported)
        self.assertEqual(settings.effort, "medium")
        self.assertEqual(settings.args, ("--effort", "medium"))
        self.assertIsNone(settings.unsupported_reason)
        self.assertEqual(get_strategy_args(ExecutionStrategy.STANDARD), ["--effort", "medium"])
        self.assertTrue(is_strategy_supported(ExecutionStrategy.STANDARD))

    def test_high_effort_strategy_mapping(self) -> None:
        settings = get_execution_settings(ExecutionStrategy.HIGH_EFFORT)
        self.assertTrue(settings.is_supported)
        self.assertEqual(settings.effort, "high")
        self.assertEqual(settings.args, ("--effort", "high"))
        self.assertIsNone(settings.unsupported_reason)
        self.assertEqual(get_strategy_args(ExecutionStrategy.HIGH_EFFORT), ["--effort", "high"])
        self.assertTrue(is_strategy_supported(ExecutionStrategy.HIGH_EFFORT))

    def test_boost_strategy_mapping_supported(self) -> None:
        settings = get_execution_settings(ExecutionStrategy.BOOST)
        self.assertTrue(settings.is_supported)
        self.assertEqual(settings.effort, "high")
        self.assertEqual(settings.args, ("--effort", "high"))
        self.assertIsNone(settings.unsupported_reason)
        self.assertEqual(get_strategy_args(ExecutionStrategy.BOOST), ["--effort", "high"])
        self.assertTrue(is_strategy_supported(ExecutionStrategy.BOOST))

    def test_case_insensitive_strategy_mapping(self) -> None:
        self.assertTrue(get_execution_settings("standard").is_supported)
        self.assertTrue(get_execution_settings("high_effort").is_supported)
        self.assertTrue(get_execution_settings("boost").is_supported)

    def test_unknown_strategy_handled_cleanly(self) -> None:
        settings = get_execution_settings("NONEXISTENT_STRATEGY")
        self.assertFalse(settings.is_supported)
        self.assertIn("Unknown", settings.unsupported_reason or "")

    def test_map_strategy_alias(self) -> None:
        self.assertEqual(
            map_strategy(ExecutionStrategy.STANDARD),
            get_execution_settings(ExecutionStrategy.STANDARD),
        )


class TestRunnerArgvAndEnvironment(unittest.TestCase):
    """Tests for argv construction and profile environment handling."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.config_root = Path(self.tmp_dir) / "config"
        self.data_root = Path(self.tmp_dir) / "data"
        self.profile_store = ProfileStore(self.config_root, self.data_root)
        self.runner = AntigravityRunner(
            agy_path="/usr/local/bin/agy",
            profile_store=self.profile_store,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_argv_standard_invocation(self) -> None:
        inv = ModelInvocation(
            invocation_id=InvocationId("i-1"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            strategy=ExecutionStrategy.STANDARD,
            prompt="Hello world",
        )
        argv = self.runner.build_argv(inv)
        self.assertEqual(Path(argv[0]), Path("/usr/local/bin/agy"))
        self.assertIn("--effort", argv)
        idx = argv.index("--effort")
        self.assertEqual(argv[idx + 1], "medium")
        self.assertIn("--output-format", argv)
        idx_out = argv.index("--output-format")
        self.assertEqual(argv[idx_out + 1], "text")
        self.assertIn("--print", argv)
        self.assertEqual(argv[argv.index("--print") + 1], "Hello world")

    def test_argv_high_effort_invocation(self) -> None:
        inv = ModelInvocation(
            invocation_id=InvocationId("i-2"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.ARCHITECTURE,
            strategy=ExecutionStrategy.HIGH_EFFORT,
            prompt="Deep thought",
        )
        argv = self.runner.build_argv(inv)
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_argv_structured_json_schema(self) -> None:
        schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
        inv = ModelInvocation(
            invocation_id=InvocationId("i-3"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.SYNTHESIZER,
            strategy=ExecutionStrategy.STANDARD,
            prompt="Synthesize",
            output_schema=schema,
        )
        argv = self.runner.build_argv(inv)
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertIn("--json-schema", argv)
        self.assertEqual(argv[argv.index("--json-schema") + 1], json.dumps(schema))

    def test_argv_conversation_id_flag(self) -> None:
        inv = ModelInvocation(
            invocation_id=InvocationId("i-4"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Follow up",
            conversation_id=ConversationId("conv-12345"),
        )
        argv = self.runner.build_argv(inv)
        self.assertIn("--conversation", argv)
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-12345")

    def test_argv_with_profile_settings(self) -> None:
        prof = self.profile_store.create(
            "test-prof",
            settings=ProfileSettings(
                model="gemini-2.5-pro",
                dangerously_skip_permissions=True,
            ),
        )
        inv = ModelInvocation(
            invocation_id=InvocationId("i-5"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.EXECUTOR,
            strategy=ExecutionStrategy.STANDARD,
            workspace_mode=WorkspaceMode.MUTATING,
            prompt="Implement code",
        )
        argv = self.runner.build_argv(inv, profile=prof)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-2.5-pro")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertNotIn("--sandbox", argv)
        self.assertIn("--effort", argv)
        self.assertIn("--print", argv)

    def test_runner_enforces_read_only_mode(self) -> None:
        """Regression test for W4-01: READ_ONLY workspace mode is enforced at runner boundary."""
        prof = self.profile_store.create(
            "ro-prof",
            settings=ProfileSettings(
                model="gemini-2.5-pro",
                dangerously_skip_permissions=True,
            ),
        )
        ro_inv = ModelInvocation(
            invocation_id=InvocationId("i-ro"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-ro"),
            role=WorkerRole.GENERAL,
            strategy=ExecutionStrategy.STANDARD,
            workspace_mode=WorkspaceMode.READ_ONLY,
            prompt="Read and inspect repo",
        )
        argv = self.runner.build_argv(ro_inv, profile=prof)
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", argv)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_argv_boost_strategy(self) -> None:
        inv = ModelInvocation(
            invocation_id=InvocationId("i-boost"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            strategy=ExecutionStrategy.BOOST,
            prompt="Boost me",
        )
        argv = self.runner.build_argv(inv)
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")


class TestOneShotInvocationExecution(unittest.IsolatedAsyncioTestCase):
    """Tests for ModelRunner one-shot invocation execution with mocked subprocesses."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.config_root = Path(self.tmp_dir) / "config"
        self.data_root = Path(self.tmp_dir) / "data"
        self.profile_store = ProfileStore(self.config_root, self.data_root)
        self.runner = AntigravityRunner(
            agy_path="/mock/agy",
            profile_store=self.profile_store,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_mock_process(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> AsyncMock:
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate.return_value = (
            stdout.encode("utf-8"),
            stderr.encode("utf-8"),
        )
        return proc

    @patch("asyncio.create_subprocess_exec")
    async def test_standard_invocation_success(self, mock_exec: AsyncMock) -> None:
        mock_proc = self._create_mock_process(stdout="Success result from model")
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-std"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            strategy=ExecutionStrategy.STANDARD,
            prompt="Explain recursion",
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.invocation_id, InvocationId("inv-std"))
        self.assertEqual(result.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(result.response, "Success result from model")
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.error)
        self.assertIsNotNone(result.started_at)
        self.assertIsNotNone(result.completed_at)

        # Check subprocess invocation
        mock_exec.assert_called_once()
        args, kwargs = mock_exec.call_args
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "medium")

    @patch("asyncio.create_subprocess_exec")
    async def test_high_effort_invocation(self, mock_exec: AsyncMock) -> None:
        mock_proc = self._create_mock_process(stdout="Deep answer")
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-high"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.ARCHITECTURE,
            strategy=ExecutionStrategy.HIGH_EFFORT,
            prompt="Complex architecture design",
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(result.response, "Deep answer")
        args, kwargs = mock_exec.call_args
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "high")

    @patch("agym.orchestration.runner.get_execution_settings")
    @patch("asyncio.create_subprocess_exec")
    async def test_unsupported_strategy_invocation_returns_clean_failure(
        self,
        mock_exec: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_settings.return_value = ExecutionSettings(
            strategy=ExecutionStrategy.STANDARD,
            is_supported=False,
            unsupported_reason="Custom unsupported reason",
        )
        inv = ModelInvocation(
            invocation_id=InvocationId("inv-unsupported"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            strategy=ExecutionStrategy.STANDARD,
            prompt="Run with unsupported",
        )
        result = await self.runner.run_async(inv)

        # Must fail cleanly without spawning subprocess
        mock_exec.assert_not_called()
        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("Unsupported execution strategy", result.error or "")
        self.assertIn("Custom unsupported reason", result.error or "")

    @patch("asyncio.create_subprocess_exec")
    async def test_structured_json_success(self, mock_exec: AsyncMock) -> None:
        payload = {"plan": "Refactor codebase", "steps": ["audit", "execute"]}
        mock_proc = self._create_mock_process(stdout=json.dumps(payload))
        mock_exec.return_value = mock_proc

        schema = {
            "type": "object",
            "properties": {
                "plan": {"type": "string"},
                "steps": {"type": "array"},
            },
            "required": ["plan"],
        }
        inv = ModelInvocation(
            invocation_id=InvocationId("inv-json"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.ARCHITECTURE,
            prompt="Output plan schema",
            output_schema=schema,
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(result.structured_data, payload)
        self.assertEqual(result.response, json.dumps(payload))
        self.assertEqual(result.exit_code, 0)

    @patch("asyncio.create_subprocess_exec")
    async def test_structured_json_wrapped_in_result_key(self, mock_exec: AsyncMock) -> None:
        payload = {"result": {"verdict": "APPROVE", "score": 95}}
        mock_proc = self._create_mock_process(stdout=json.dumps(payload))
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-wrapped"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.AUDITOR,
            prompt="Audit verdict",
            output_schema={"type": "object", "required": ["verdict"]},
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(result.structured_data, {"verdict": "APPROVE", "score": 95})

    @patch("asyncio.create_subprocess_exec")
    async def test_invalid_json_distinguished_from_process_failure(
        self,
        mock_exec: AsyncMock,
    ) -> None:
        # Exit code is 0 (process succeeded), but stdout is not valid JSON
        mock_proc = self._create_mock_process(
            stdout="Here is the result: {not valid json",
            returncode=0,
        )
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-bad-json"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Give me json",
            output_schema={"type": "object"},
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.response, "Here is the result: {not valid json")
        self.assertIn("Malformed JSON", result.error or "")
        self.assertIsNone(result.structured_data)

    @patch("asyncio.create_subprocess_exec")
    async def test_structured_payload_missing_required_property(
        self,
        mock_exec: AsyncMock,
    ) -> None:
        # Valid JSON, but missing required key
        mock_proc = self._create_mock_process(
            stdout=json.dumps({"unrelated": 123}),
            returncode=0,
        )
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-missing-prop"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.AUDITOR,
            prompt="Audit",
            output_schema={"type": "object", "required": ["verdict"]},
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("missing required field", result.error or "")
        self.assertIsNotNone(result.response)

    @patch("asyncio.create_subprocess_exec")
    async def test_empty_response_handling(self, mock_exec: AsyncMock) -> None:
        mock_proc = self._create_mock_process(stdout="   \n   ", stderr="Backend returned no response", returncode=0)
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-empty"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Say something",
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Empty output", result.error or "")
        self.assertIn("Backend returned no response", result.error or "")

    @patch("asyncio.create_subprocess_exec")
    async def test_nonzero_exit_reporting(self, mock_exec: AsyncMock) -> None:
        mock_proc = self._create_mock_process(
            stdout="",
            stderr="fatal: internal engine crash",
            returncode=2,
        )
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-nonzero"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Crashing command",
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("non-zero exit code 2", result.error or "")
        self.assertIn("internal engine crash", result.error or "")

    @patch("asyncio.create_subprocess_exec")
    async def test_nonzero_exit_preserves_stdout_and_error_separately(self, mock_exec: AsyncMock) -> None:
        """W4-15: Useful stdout and error are preserved separately on non-zero exit."""
        mock_proc = self._create_mock_process(
            stdout="Useful partial analysis before exit",
            stderr="fatal: process crashed with error",
            returncode=1,
        )
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-nonzero-stdout"),
            run_id=RunId("run-w415"),
            worker_id=InvocationId("w-partial"),
            role=WorkerRole.GENERAL,
            prompt="Analyze codebase",
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.exit_code, 1)
        # Stdout must be preserved in response
        self.assertEqual(result.response, "Useful partial analysis before exit")
        # Error must be preserved in error
        self.assertIn("non-zero exit code 1", result.error or "")
        self.assertIn("fatal: process crashed with error", result.error or "")
        # Response and error are distinct
        self.assertNotEqual(result.response, result.error)

    @patch("asyncio.create_subprocess_exec")
    async def test_auth_failure_detection(self, mock_exec: AsyncMock) -> None:
        mock_proc = self._create_mock_process(
            stdout="",
            stderr="Please log in: oauth2: token expired, re-authenticate with google",
            returncode=1,
        )
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-auth"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Do something",
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("Authentication failure", result.error or "")

    @patch("asyncio.create_subprocess_exec")
    async def test_conversation_id_capture(self, mock_exec: AsyncMock) -> None:
        mock_proc = self._create_mock_process(
            stdout="Response text\nConversation ID: conv-abc-12345\nDone",
        )
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-conv"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Begin session",
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(result.conversation_id, ConversationId("conv-abc-12345"))

    @patch("asyncio.create_subprocess_exec")
    async def test_timeout_handling(self, mock_exec: AsyncMock) -> None:
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr.read.return_value = b""
        # communicate hangs forever
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-timeout"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Sleep forever",
            timeout_seconds=0.1,
        )
        result = await self.runner.run_async(inv)

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("timed out", result.error or "")
        mock_proc.terminate.assert_called()

    @patch("asyncio.create_subprocess_exec")
    async def test_cancellation_handling(self, mock_exec: AsyncMock) -> None:
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr.read.return_value = b""
        mock_proc.communicate.side_effect = asyncio.CancelledError()
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-cancel"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Cancel me",
        )
        with self.assertRaises(asyncio.CancelledError):
            await self.runner.run_async(inv)

        mock_proc.terminate.assert_called()

    @patch("asyncio.create_subprocess_exec")
    async def test_profile_env_usage(self, mock_exec: AsyncMock) -> None:
        prof = self.profile_store.create("work-prof")
        mock_proc = self._create_mock_process(stdout="Profile response")
        mock_exec.return_value = mock_proc

        inv = ModelInvocation(
            invocation_id=InvocationId("inv-prof"),
            run_id=RunId("run-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Prompt with profile",
        )
        result = await self.runner.run_async(inv, profile_name="work-prof")

        self.assertEqual(result.status, InvocationStatus.SUCCEEDED)
        mock_exec.assert_called_once()
        kwargs = mock_exec.call_args[1]
        env = kwargs.get("env", {})
        self.assertEqual(env.get("AGYM_PROFILE"), "work-prof")
        self.assertEqual(env.get("GEMINI_FORCE_FILE_STORAGE"), "true")
        self.assertEqual(Path(env.get("HOME", "")).resolve(), prof.home.resolve())

    def test_synchronous_run_conforms_to_protocol(self) -> None:
        self.assertIsInstance(self.runner, ModelRunner)

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = self._create_mock_process(stdout="Sync result")
            mock_exec.return_value = mock_proc

            inv = ModelInvocation(
                invocation_id=InvocationId("inv-sync"),
                run_id=RunId("run-1"),
                worker_id=InvocationId("w-1"),
                role=WorkerRole.GENERAL,
                prompt="Sync prompt",
            )
            result = self.runner.run(inv)
            self.assertEqual(result.status, InvocationStatus.SUCCEEDED)
            self.assertEqual(result.response, "Sync result")


class TestProcessIsolationAndControl(unittest.TestCase):
    """Tests for runner process isolation, cancel_run, and cancel_invocation."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.profile_store = ProfileStore(Path(self.tmp_dir) / "config", Path(self.tmp_dir) / "data")
        self.runner = AntigravityRunner(profile_store=self.profile_store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cancel_run_terminates_only_specified_run(self) -> None:
        proc1 = MagicMock()
        proc1.returncode = None
        proc1.stderr.read.return_value = b""
        proc2 = MagicMock()
        proc2.returncode = None
        proc2.stderr.read.return_value = b""
        proc3 = MagicMock()
        proc3.returncode = None

        self.runner._register_process(RunId("run-A"), InvocationId("inv-1"), proc1)
        self.runner._register_process(RunId("run-A"), InvocationId("inv-2"), proc2)
        self.runner._register_process(RunId("run-B"), InvocationId("inv-3"), proc3)

        self.assertEqual(set(self.runner.active_runs()), {RunId("run-A"), RunId("run-B")})
        self.assertEqual(
            set(self.runner.active_invocations("run-A")),
            {InvocationId("inv-1"), InvocationId("inv-2")},
        )

        cancelled = self.runner.cancel_run("run-A")
        self.assertEqual(cancelled, 2)
        proc1.terminate.assert_called_once()
        proc2.terminate.assert_called_once()
        proc3.terminate.assert_not_called()

    def test_cancel_individual_invocation(self) -> None:
        proc1 = MagicMock()
        proc1.returncode = None
        proc1.stderr.read.return_value = b""
        proc2 = MagicMock()
        proc2.returncode = None
        proc2.stderr.read.return_value = b""

        self.runner._register_process(RunId("run-A"), InvocationId("inv-1"), proc1)
        self.runner._register_process(RunId("run-A"), InvocationId("inv-2"), proc2)

        self.assertTrue(self.runner.cancel_invocation("run-A", "inv-1"))
        proc1.terminate.assert_called_once()
        proc2.terminate.assert_not_called()
        self.assertFalse(self.runner.cancel_invocation("run-A", "nonexistent"))

    def test_no_global_process_registry(self) -> None:
        runner2 = AntigravityRunner(profile_store=self.profile_store)
        proc = MagicMock()
        proc.returncode = None

        self.runner._register_process(RunId("run-1"), InvocationId("i-1"), proc)
        self.assertEqual(len(self.runner.active_runs()), 1)
        self.assertEqual(len(runner2.active_runs()), 0)


class TestPersistentModelSession(unittest.TestCase):
    """Tests for AntigravitySession multi-turn execution and lifecycle."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.profile_store = ProfileStore(Path(self.tmp_dir) / "config", Path(self.tmp_dir) / "data")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("asyncio.create_subprocess_exec")
    def test_persistent_session_conforms_to_protocol(self, mock_exec: AsyncMock) -> None:
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr.read.return_value = b""
        mock_exec.return_value = mock_proc

        session = AntigravitySession(
            agy_path="/mock/agy",
            profile_store=self.profile_store,
        )
        self.assertIsInstance(session, ModelSession)
        session.close()

    @patch("asyncio.create_subprocess_exec")
    def test_persistent_session_multiple_messages_and_convo_id(
        self,
        mock_exec: AsyncMock,
    ) -> None:
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr.read.return_value = b""

        # Sequence of NDJSON stream events for two turns:
        # Turn 1: init event (conversation_id), then result event
        # Turn 2: result event
        lines = [
            b'{"event": "init", "conversation_id": "session-conv-42"}\n',
            b'{"event": "result", "response": "Answer turn 1"}\n',
            b'{"event": "result", "response": "Answer turn 2"}\n',
            b"",
        ]
        mock_proc.stdout.read.side_effect = lines
        mock_exec.return_value = mock_proc

        session = AntigravitySession(
            agy_path="/mock/agy",
            profile_store=self.profile_store,
        )

        res1 = session.ask("Question 1")
        self.assertEqual(res1.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(res1.response, "Answer turn 1")
        self.assertEqual(session.conversation_id, ConversationId("session-conv-42"))

        res2 = session.send("Question 2")
        self.assertEqual(res2.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(res2.response, "Answer turn 2")
        self.assertEqual(res2.conversation_id, ConversationId("session-conv-42"))

        # Verify stdin received both formatted NDJSON turns
        write_calls = mock_proc.stdin.write.call_args_list
        self.assertEqual(len(write_calls), 2)
        payload1 = json.loads(write_calls[0][0][0].decode("utf-8"))
        payload2 = json.loads(write_calls[1][0][0].decode("utf-8"))
        self.assertEqual(payload1, {"event": "user", "message": {"content": "Question 1"}})
        self.assertEqual(payload2, {"event": "user", "message": {"content": "Question 2"}})

        session.close()

    @patch("asyncio.create_subprocess_exec")
    def test_persistent_session_close_cleanly(self, mock_exec: AsyncMock) -> None:
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr.read.return_value = b""
        mock_exec.return_value = mock_proc

        session = AntigravitySession(
            agy_path="/mock/agy",
            profile_store=self.profile_store,
        )
        self.assertFalse(session._is_closed)
        session.close()
        self.assertTrue(session._is_closed)
        mock_proc.stdin.close.assert_called_once()

    @patch("asyncio.create_subprocess_exec")
    def test_persistent_session_context_manager(self, mock_exec: AsyncMock) -> None:
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stderr.read.return_value = b""
        mock_exec.return_value = mock_proc

        with AntigravitySession(agy_path="/mock/agy", profile_store=self.profile_store) as sess:
            self.assertFalse(sess._is_closed)
        self.assertTrue(sess._is_closed)

    @patch("asyncio.create_subprocess_exec")
    def test_session_timeout_or_crash_cannot_contaminate_next_turn(self, mock_exec: AsyncMock) -> None:
        """W4-09: Timeout or process crash invalidates session so next turn cannot consume delayed/contaminated output."""
        # Process 1: Turn 1 times out (hangs reading stdout)
        proc1 = AsyncMock()
        proc1.returncode = None
        proc1.stderr.read.return_value = b""
        async def slow_readline(*_args) -> bytes:
            await asyncio.sleep(5.0)
            return b'{"event": "result", "response": "Delayed Turn 1 output"}\n'
        proc1.stdout.read.side_effect = slow_readline

        # Process 2: Spawned on restart for Turn 2
        proc2 = AsyncMock()
        proc2.returncode = None
        proc2.stderr.read.return_value = b""
        proc2.stdout.read.side_effect = [
            b'{"event": "result", "response": "Turn 2 fresh output"}\n',
        ]

        mock_exec.side_effect = [proc1, proc2]

        session = AntigravitySession(
            agy_path="/mock/agy",
            profile_store=self.profile_store,
            timeout_seconds=0.1,
        )

        # Turn 1 should time out and fail
        res1 = session.send("Turn 1 prompt", timeout_seconds=0.1)
        self.assertEqual(res1.status, InvocationStatus.FAILED)
        self.assertIn("timed out", (res1.error or "").lower())
        self.assertTrue(session._is_invalid)

        # Turn 2 must NOT receive Turn 1's delayed output; it restarts and gets Turn 2 output
        res2 = session.send("Turn 2 prompt", timeout_seconds=2.0)
        self.assertEqual(res2.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(res2.response, "Turn 2 fresh output")
        self.assertNotEqual(res2.response, "Delayed Turn 1 output")

        # Part 2: Crash / EOF handling
        proc3 = AsyncMock()
        proc3.returncode = 1
        proc3.stderr.read.return_value = b""
        proc3.stdout.read.side_effect = [b""]  # Immediate EOF from crashed process
        proc4 = AsyncMock()
        proc4.returncode = None
        proc4.stderr.read.return_value = b""
        proc4.stdout.read.side_effect = [
            b'{"event": "result", "response": "Recovered after crash"}\n',
        ]
        mock_exec.side_effect = [proc3, proc4]

        # Simulate crash on turn 3
        session._is_invalid = True
        res3 = session.send("Turn 3 prompt")
        self.assertEqual(res3.status, InvocationStatus.FAILED)

        # Next turn recovers
        res4 = session.send("Turn 4 prompt")
        self.assertEqual(res4.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(res4.response, "Recovered after crash")

        session.close()


class TestFakeModelRunnerAndSession(unittest.TestCase):
    """Tests for FakeModelRunner and FakeModelSession."""

    def test_fake_runner_conforms_to_protocol(self) -> None:
        runner = FakeModelRunner()
        self.assertIsInstance(runner, ModelRunner)

    def test_fake_session_conforms_to_protocol(self) -> None:
        session = FakeModelSession()
        self.assertIsInstance(session, ModelSession)

    def test_fake_runner_scripted_sequence(self) -> None:
        # Scripted responses requirement:
        # invocation 1 -> success
        # invocation 2 -> timeout
        # invocation 3 -> malformed result
        fake_runner = FakeModelRunner(
            responses=[
                "Scripted success response",
                "timeout",
                "malformed",
            ]
        )

        inv1 = ModelInvocation(
            invocation_id=InvocationId("i-1"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Prompt 1",
        )
        res1 = fake_runner.run(inv1)
        self.assertEqual(res1.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(res1.response, "Scripted success response")

        inv2 = ModelInvocation(
            invocation_id=InvocationId("i-2"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Prompt 2",
        )
        res2 = fake_runner.run(inv2)
        self.assertEqual(res2.status, InvocationStatus.FAILED)
        self.assertIn("timed out", res2.error or "")

        inv3 = ModelInvocation(
            invocation_id=InvocationId("i-3"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Prompt 3",
        )
        res3 = fake_runner.run(inv3)
        self.assertEqual(res3.status, InvocationStatus.FAILED)
        self.assertIn("Malformed JSON", res3.error or "")

        # Verify invocations were tracked
        self.assertEqual(len(fake_runner.invocations), 3)
        self.assertEqual(fake_runner.invocations[0].prompt, "Prompt 1")

    def test_fake_runner_structured_dict_response(self) -> None:
        fake_runner = FakeModelRunner(responses=[{"status": "ok", "value": 42}])
        inv = ModelInvocation(
            invocation_id=InvocationId("i-dict"),
            run_id=RunId("r-1"),
            worker_id=InvocationId("w-1"),
            role=WorkerRole.GENERAL,
            prompt="Get dict",
        )
        res = fake_runner.run(inv)
        self.assertEqual(res.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(res.structured_data, {"status": "ok", "value": 42})

    def test_fake_runner_cancellation_tracking(self) -> None:
        fake_runner = FakeModelRunner()
        fake_runner.cancel_run("run-xyz")
        self.assertIn(RunId("run-xyz"), fake_runner.cancelled_runs)
        fake_runner.cancel_invocation("run-xyz", "inv-1")
        self.assertIn((RunId("run-xyz"), InvocationId("inv-1")), fake_runner.cancelled_invocations)

    def test_fake_session_scripted_multiple_messages(self) -> None:
        session = FakeModelSession(
            responses=[
                "Response 1",
                {"result": "answer 2"},
                "timeout",
            ],
            conversation_id="fake-session-99",
        )
        self.assertEqual(session.conversation_id, ConversationId("fake-session-99"))

        res1 = session.ask("Hello")
        self.assertEqual(res1.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(res1.response, "Response 1")

        res2 = session.send("Second question")
        self.assertEqual(res2.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(res2.structured_data, {"result": "answer 2"})

        res3 = session.ask("Third question")
        self.assertEqual(res3.status, InvocationStatus.FAILED)
        self.assertIn("timed out", res3.error or "")

        self.assertEqual(session.sent_prompts, ["Hello", "Second question", "Third question"])
        session.close()
        self.assertTrue(session._is_closed)


class TestFailureClassification(unittest.TestCase):
    """Tests for classify_failure mapping."""

    def test_classify_succeeded(self) -> None:
        res = ModelResult(invocation_id=InvocationId("i-1"), status=InvocationStatus.SUCCEEDED)
        self.assertEqual(classify_failure(res), FailureClass.RECOVERABLE)

    def test_classify_timeout(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.FAILED,
            error="Execution timed out after 300s",
        )
        self.assertEqual(classify_failure(res), FailureClass.RETRYABLE)

    def test_classify_nonzero(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.FAILED,
            exit_code=1,
            error="Process exited with non-zero exit code 1",
        )
        self.assertEqual(classify_failure(res), FailureClass.RETRYABLE)

    def test_classify_empty_output(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.FAILED,
            error="Empty output returned from model runner",
        )
        self.assertEqual(classify_failure(res), FailureClass.RETRYABLE)

    def test_classify_auth_failure(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.FAILED,
            error="Authentication failure: not authenticated",
        )
        self.assertEqual(classify_failure(res), FailureClass.UNRECOVERABLE)

    def test_classify_unsupported_strategy(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.FAILED,
            error="Unsupported execution strategy 'BOOST'",
        )
        self.assertEqual(classify_failure(res), FailureClass.UNRECOVERABLE)

    def test_classify_cancelled(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.CANCELLED,
            error="Execution was cancelled",
        )
        self.assertEqual(classify_failure(res), FailureClass.UNRECOVERABLE)

    def test_classify_malformed_json(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.FAILED,
            error="Malformed JSON in output: invalid character",
        )
        self.assertEqual(classify_failure(res), FailureClass.RECOVERABLE)

    def test_classify_permission_denied(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.FAILED,
            error="Tool permission denied for RunCommand: user denied permission to run command",
        )
        self.assertEqual(classify_failure(res), FailureClass.RETRYABLE)

    def test_classify_empty_response(self) -> None:
        res = ModelResult(
            invocation_id=InvocationId("i-1"),
            status=InvocationStatus.FAILED,
            error="Empty response in final result event",
        )
        self.assertEqual(classify_failure(res), FailureClass.RETRYABLE)


class TestIndependenceAndSafety(unittest.TestCase):
    """Tests ensuring implementation avoids council dependencies and uses zero quota."""

    def test_no_dependencies_on_agym_council(self) -> None:
        import inspect

        import agym.orchestration.runner as runner_mod
        import agym.orchestration.strategies as strat_mod

        runner_src = inspect.getsource(runner_mod)
        strat_src = inspect.getsource(strat_mod)

        self.assertNotIn("agym.council", runner_src)
        self.assertNotIn("agym/council", runner_src)
        self.assertNotIn("agym.council", strat_src)
        self.assertNotIn("agym/council", strat_src)


if __name__ == "__main__":
    unittest.main()
