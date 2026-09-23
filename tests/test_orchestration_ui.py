"""Tests for orchestration Terminal UI and EventSink subsystem.

Covers:
- EventSink protocol adherence
- Architectural boundary invariants (zero imports of runner, scheduler, coordinator, persistence)
- Presentation state derivation purely from events
- TTY mode layout, columns, alignment, and formatting
- Non-TTY mode log lines and no-animation invariants
- Distinct failure representations (FAILED, CANCELLED, RETRYING, REJECTED, INTERRUPTED)
- Dry-run plan view rendering (all 7 required fields)
- Thread safety and concurrent event emission
- Resilience to malformed payloads and edge cases
"""

from __future__ import annotations

import ast
import io
import os
import threading
import unittest
from pathlib import Path

from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    ComplexityLevel,
    CoordinatorAction,
    EventSink,
    EventType,
    ExecutionStrategy,
    FailureClass,
    FleetView,
    InvocationId,
    InvocationStatus,
    OrchestrationBudget,
    OrchestrationEvent,
    ProfileLease,
    RunId,
    RunMode,
    RunStatus,
    TaskAssessment,
    TaskType,
    WorkerId,
    WorkerRequest,
    WorkerResult,
    WorkerRole,
    WorkspaceMode,
)
from agym.orchestration.ui import (
    ROLE_DISPLAY_NAMES,
    OrchestrationUI,
    PresentationState,
    TerminalEventSink,
    WavePresentation,
    WorkerPresentation,
    WorkerStatus,
    colorize,
    format_duration,
    render_dry_run,
)


class TestOrchestrationUIArchitecturalInvariants(unittest.TestCase):
    """Verifies strict isolation rules and protocol conformance."""

    def test_event_sink_protocol_compliance(self) -> None:
        """TerminalEventSink and OrchestrationUI must implement EventSink protocol."""
        sink = TerminalEventSink(is_tty=False)
        self.assertIsInstance(sink, EventSink)

        ui = OrchestrationUI(is_tty=False)
        self.assertIsInstance(ui, EventSink)
        self.assertIs(OrchestrationUI, TerminalEventSink)

        self.assertTrue(callable(getattr(sink, "emit", None)))
        self.assertTrue(callable(getattr(sink, "get_events", None)))

    def test_no_forbidden_module_imports(self) -> None:
        """ui.py must NEVER import runner, scheduler, coordinator, persistence, or council."""
        ui_path = Path(__file__).resolve().parent.parent / "agym" / "orchestration" / "ui.py"
        self.assertTrue(ui_path.exists(), f"File {ui_path} does not exist")

        tree = ast.parse(ui_path.read_text(encoding="utf-8"))

        forbidden_modules = {
            "agym.orchestration.runner",
            "agym.orchestration.scheduler",
            "agym.orchestration.coordinator",
            "agym.orchestration.persistence",
            "runner",
            "scheduler",
            "coordinator",
            "persistence",
            "agym.council",
            "council",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in forbidden_modules:
                        self.assertFalse(
                            alias.name == forbidden or alias.name.endswith(f".{forbidden}"),
                            f"Forbidden import found in ui.py: {alias.name}",
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for forbidden in forbidden_modules:
                    self.assertFalse(
                        module == forbidden or module.endswith(f".{forbidden}"),
                        f"Forbidden from-import found in ui.py: {module}",
                    )


class TestPresentationStateDerivation(unittest.TestCase):
    """Verifies that the UI presentation state is derived solely from events."""

    def setUp(self) -> None:
        self.sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="run-state-test")

    def test_run_created_event(self) -> None:
        event = OrchestrationEvent(
            event_id="e-1",
            run_id=RunId("run-state-test"),
            type=EventType.RUN_CREATED,
            payload={"task": "Implement payment flow", "mode": "PLAN"},
        )
        self.sink.emit(event)

        self.assertEqual(self.sink.state.run_id, "run-state-test")
        self.assertEqual(self.sink.state.task, "Implement payment flow")
        self.assertEqual(self.sink.state.mode, "PLAN")
        self.assertEqual(self.sink.state.run_status, "CREATED")

    def test_task_assessed_event_flat_payload(self) -> None:
        event = OrchestrationEvent(
            event_id="e-2",
            run_id=RunId("run-state-test"),
            type=EventType.TASK_ASSESSED,
            payload={"complexity": "LARGE", "task_type": "ARCHITECTURE"},
        )
        self.sink.emit(event)

        self.assertTrue(self.sink.state.assessment_done)
        self.assertEqual(self.sink.state.complexity, "LARGE")
        self.assertEqual(self.sink.state.task_type, "ARCHITECTURE")

    def test_task_assessed_event_object_payload(self) -> None:
        assessment = TaskAssessment(
            task_type=TaskType.DEBUGGING,
            complexity=ComplexityLevel.MEDIUM,
            confidence=0.9,
            mutation_required=False,
            repository_scope="src/",
            summary="Diagnose deadlock",
        )
        event = OrchestrationEvent(
            event_id="e-2b",
            run_id=RunId("run-state-test"),
            type=EventType.TASK_ASSESSED,
            payload={"assessment": assessment},
        )
        self.sink.emit(event)

        self.assertTrue(self.sink.state.assessment_done)
        self.assertEqual(self.sink.state.complexity, "MEDIUM")
        self.assertEqual(self.sink.state.task_type, "DEBUGGING")
        self.assertEqual(self.sink.state.task, "Diagnose deadlock")

    def test_fleet_view_updates(self) -> None:
        event = OrchestrationEvent(
            event_id="e-3",
            run_id=RunId("run-state-test"),
            type=EventType.ROUND_STARTED,
            payload={"round_number": 1, "fleet_view": {"available_profiles": 8}},
        )
        self.sink.emit(event)
        self.assertEqual(self.sink.state.fleet_available, 8)

    def test_round_and_wave_transitions(self) -> None:
        self.sink.emit(
            OrchestrationEvent(
                event_id="e-w1",
                run_id=RunId("run-state-test"),
                type=EventType.ROUND_STARTED,
                payload={"round_number": 1},
            )
        )
        self.assertEqual(self.sink.state.current_wave, 1)
        self.assertIn(1, self.sink.state.waves)
        self.assertEqual(self.sink.state.waves[1].status, "RUNNING")

        self.sink.emit(
            OrchestrationEvent(
                event_id="e-w1-done",
                run_id=RunId("run-state-test"),
                type=EventType.ROUND_COMPLETED,
                payload={"round_number": 1},
            )
        )
        self.assertEqual(self.sink.state.waves[1].status, "COMPLETED")

        self.sink.emit(
            OrchestrationEvent(
                event_id="e-w2",
                run_id=RunId("run-state-test"),
                type=EventType.ROUND_STARTED,
                payload={"round_number": 2},
            )
        )
        self.assertEqual(self.sink.state.current_wave, 2)
        self.assertIn(2, self.sink.state.waves)

    def test_worker_lifecycle_tracking(self) -> None:
        # 1. Action requested with workers
        self.sink.emit(
            OrchestrationEvent(
                event_id="e-act",
                run_id=RunId("run-state-test"),
                type=EventType.ACTION_REQUESTED,
                payload={
                    "workers": [
                        {"worker_id": "architect", "role": "ARCHITECTURE"},
                        {"worker_id": "testing", "role": "TESTING"},
                    ]
                },
            )
        )
        self.assertIn("architect", self.sink.state.workers)
        self.assertIn("testing", self.sink.state.workers)
        self.assertEqual(self.sink.state.workers["architect"].status, WorkerStatus.PENDING)

        # 2. Profile leased
        self.sink.emit(
            OrchestrationEvent(
                event_id="e-lease-1",
                run_id=RunId("run-state-test"),
                type=EventType.PROFILE_LEASED,
                payload={"worker_id": "architect", "profile_name": "AI3"},
            )
        )
        self.assertEqual(self.sink.state.workers["architect"].profile_name, "AI3")

        # 3. Invocation started
        self.sink.emit(
            OrchestrationEvent(
                event_id="e-start-1",
                run_id=RunId("run-state-test"),
                type=EventType.INVOCATION_STARTED,
                payload={"worker_id": "architect", "started_at": "2026-09-23T10:00:00Z"},
            )
        )
        self.assertEqual(self.sink.state.workers["architect"].status, WorkerStatus.RUNNING)

        # 4. Invocation completed
        self.sink.emit(
            OrchestrationEvent(
                event_id="e-comp-1",
                run_id=RunId("run-state-test"),
                type=EventType.INVOCATION_COMPLETED,
                payload={
                    "worker_id": "architect",
                    "completed_at": "2026-09-23T10:00:42Z",
                    "duration": 42.0,
                },
            )
        )
        self.assertEqual(self.sink.state.workers["architect"].status, WorkerStatus.SUCCEEDED)
        self.assertEqual(self.sink.state.workers["architect"].duration_seconds, 42.0)

    def test_get_events_isolated(self) -> None:
        e1 = OrchestrationEvent(event_id="1", run_id=RunId("run-A"), type=EventType.RUN_CREATED)
        e2 = OrchestrationEvent(event_id="2", run_id=RunId("run-B"), type=EventType.RUN_CREATED)
        e3 = OrchestrationEvent(event_id="3", run_id=RunId("run-A"), type=EventType.RUN_COMPLETED)

        self.sink.emit(e1)
        self.sink.emit(e2)
        self.sink.emit(e3)

        events_a = self.sink.get_events("run-A")
        events_b = self.sink.get_events("run-B")

        self.assertEqual(len(events_a), 2)
        self.assertEqual(events_a[0].event_id, "1")
        self.assertEqual(events_a[1].event_id, "3")

        self.assertEqual(len(events_b), 1)
        self.assertEqual(events_b[0].event_id, "2")


class TestTTYModeRendering(unittest.TestCase):
    """Verifies TTY visual rendering matches exact specification in wave2-C.md."""

    def test_wave2_mock_display_exact_match(self) -> None:
        """Tests the exact layout specified in wave2-C.md:

        AGYM Orchestrator · RUN-ID

        Assessment     ✓ LARGE
        Fleet          ✓ 8 available

        Wave 1
          architect    ✓ AI3   42s
          testing      ⠹ AI7
          alternative  ✓ AI2   31s
        """
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="RUN-ID")

        # Emit events to build the exact state
        sink.emit(
            OrchestrationEvent(
                event_id="e1",
                run_id=RunId("RUN-ID"),
                type=EventType.RUN_CREATED,
                payload={"task": "Orchestration demo"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e2",
                run_id=RunId("RUN-ID"),
                type=EventType.TASK_ASSESSED,
                payload={"complexity": "LARGE", "task_type": "ARCHITECTURE"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e3",
                run_id=RunId("RUN-ID"),
                type=EventType.ROUND_STARTED,
                payload={"round_number": 1, "available_profiles": 8},
            )
        )

        # Worker 1: architect - succeeded AI3 42s
        sink.emit(
            OrchestrationEvent(
                event_id="e4",
                run_id=RunId("RUN-ID"),
                type=EventType.PROFILE_LEASED,
                payload={"worker_id": "architect", "profile_name": "AI3"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e5",
                run_id=RunId("RUN-ID"),
                type=EventType.INVOCATION_STARTED,
                payload={"worker_id": "architect", "role": "ARCHITECTURE"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e6",
                run_id=RunId("RUN-ID"),
                type=EventType.INVOCATION_COMPLETED,
                payload={"worker_id": "architect", "duration": 42.0},
            )
        )

        # Worker 2: testing - running AI7
        sink.emit(
            OrchestrationEvent(
                event_id="e7",
                run_id=RunId("RUN-ID"),
                type=EventType.PROFILE_LEASED,
                payload={"worker_id": "testing", "profile_name": "AI7"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e8",
                run_id=RunId("RUN-ID"),
                type=EventType.INVOCATION_STARTED,
                payload={"worker_id": "testing", "role": "TESTING"},
            )
        )

        # Worker 3: alternative - succeeded AI2 31s
        sink.emit(
            OrchestrationEvent(
                event_id="e9",
                run_id=RunId("RUN-ID"),
                type=EventType.PROFILE_LEASED,
                payload={"worker_id": "alternative", "profile_name": "AI2"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e10",
                run_id=RunId("RUN-ID"),
                type=EventType.INVOCATION_STARTED,
                payload={"worker_id": "alternative", "role": "ALTERNATIVE_DESIGN"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e11",
                run_id=RunId("RUN-ID"),
                type=EventType.INVOCATION_COMPLETED,
                payload={"worker_id": "alternative", "duration": 31.0},
            )
        )

        rendered = sink.render(use_color=False)
        lines = [line.rstrip() for line in rendered.splitlines()]

        # Line-by-line verification
        self.assertEqual(lines[0], "AGYM Orchestrator · RUN-ID")
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], "Assessment     ✓ LARGE")
        self.assertEqual(lines[3], "Fleet          ✓ 8 available")
        self.assertEqual(lines[4], "")
        self.assertEqual(lines[5], "Wave 1")
        self.assertEqual(lines[6], "  architect    ✓ AI3   42s")
        self.assertEqual(lines[7], "  testing      ⠹ AI7")
        self.assertEqual(lines[8], "  alternative  ✓ AI2   31s")

    def test_assessment_pending_and_failed_states(self) -> None:
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="run-pending")
        sink.emit(
            OrchestrationEvent(
                event_id="e1",
                run_id=RunId("run-pending"),
                type=EventType.RUN_CREATED,
            )
        )
        rendered = sink.render(use_color=False)
        self.assertIn("Assessment     ⠹ Assessing...", rendered)

        sink_failed = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="run-failed")
        sink_failed.emit(
            OrchestrationEvent(
                event_id="e1",
                run_id=RunId("run-failed"),
                type=EventType.RUN_FAILED,
                payload={"error": "Coordination crashed"},
            )
        )
        rendered_fail = sink_failed.render(use_color=False)
        self.assertIn("Assessment     ✗ FAILED", rendered_fail)

    def test_multiple_waves_display(self) -> None:
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="run-multi")
        sink.emit(
            OrchestrationEvent(
                event_id="e1",
                run_id=RunId("run-multi"),
                type=EventType.ROUND_STARTED,
                payload={"round_number": 1},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e2",
                run_id=RunId("run-multi"),
                type=EventType.INVOCATION_COMPLETED,
                payload={"worker_id": "architect", "role": "ARCHITECTURE", "profile_name": "AI1", "duration": 10},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e3",
                run_id=RunId("run-multi"),
                type=EventType.ROUND_STARTED,
                payload={"round_number": 2},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e4",
                run_id=RunId("run-multi"),
                type=EventType.INVOCATION_COMPLETED,
                payload={"worker_id": "synthesizer", "role": "SYNTHESIZER", "profile_name": "AI2", "duration": 15},
            )
        )

        rendered = sink.render(use_color=False)
        self.assertIn("Wave 1", rendered)
        self.assertIn("architect    ✓ AI1   10s", rendered)
        self.assertIn("Wave 2", rendered)
        self.assertIn("synthesizer  ✓ AI2   15s", rendered)

    def test_live_tty_stream_updates(self) -> None:
        """Verifies in-place cursor movement during live TTY updates."""
        stream = io.StringIO()
        sink = TerminalEventSink(stream=stream, is_tty=True, use_color=False, run_id="run-live")

        sink.emit(
            OrchestrationEvent(
                event_id="e1",
                run_id=RunId("run-live"),
                type=EventType.RUN_CREATED,
            )
        )
        first_output = stream.getvalue()
        self.assertIn("AGYM Orchestrator · run-live", first_output)

        sink.emit(
            OrchestrationEvent(
                event_id="e2",
                run_id=RunId("run-live"),
                type=EventType.TASK_ASSESSED,
                payload={"complexity": "SMALL"},
            )
        )
        second_output = stream.getvalue()
        # Verify ANSI cursor up code was emitted to redraw in place
        self.assertIn("\033[", second_output)


class TestNonTTYModeRendering(unittest.TestCase):
    """Verifies non-animated log output for CI/log capture."""

    def test_non_tty_log_sequence(self) -> None:
        stream = io.StringIO()
        sink = TerminalEventSink(stream=stream, is_tty=False, run_id="run-nontty")

        sink.emit(
            OrchestrationEvent(
                event_id="e1",
                run_id=RunId("run-nontty"),
                type=EventType.RUN_CREATED,
                payload={"task": "Clean build test"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e2",
                run_id=RunId("run-nontty"),
                type=EventType.INVOCATION_STARTED,
                payload={"worker_id": "architect", "role": "ARCHITECTURE", "profile_name": "AI3"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e3",
                run_id=RunId("run-nontty"),
                type=EventType.INVOCATION_COMPLETED,
                payload={"worker_id": "architect", "role": "ARCHITECTURE", "duration": 42.0},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e4",
                run_id=RunId("run-nontty"),
                type=EventType.RUN_COMPLETED,
                payload={"summary": "Build succeeded"},
            )
        )

        output = stream.getvalue()
        lines = [line.strip() for line in output.splitlines() if line.strip()]

        self.assertEqual(lines[0], "[run] created run-nontty - Clean build test")
        self.assertEqual(lines[1], "[worker] architect started (AI3)")
        self.assertEqual(lines[2], "[worker] architect completed (42s)")
        self.assertEqual(lines[3], "[run] completed: Build succeeded")

        # Invariant: No terminal animation or cursor escape codes in non-TTY mode
        self.assertNotIn("\033[", output)
        self.assertNotIn("\r", output)

    def test_non_tty_all_event_types(self) -> None:
        stream = io.StringIO()
        sink = TerminalEventSink(stream=stream, is_tty=False, run_id="run-all")

        events = [
            OrchestrationEvent(event_id="1", run_id=RunId("run-all"), type=EventType.TASK_ASSESSED, payload={"complexity": "LARGE", "task_type": "ARCHITECTURE"}),
            OrchestrationEvent(event_id="2", run_id=RunId("run-all"), type=EventType.ROUND_STARTED, payload={"round_number": 1}),
            OrchestrationEvent(event_id="3", run_id=RunId("run-all"), type=EventType.ACTION_REQUESTED, payload={"kind": "RUN_WORKERS"}),
            OrchestrationEvent(event_id="4", run_id=RunId("run-all"), type=EventType.ACTION_ACCEPTED, payload={"kind": "RUN_WORKERS"}),
            OrchestrationEvent(event_id="5", run_id=RunId("run-all"), type=EventType.PROFILE_LEASED, payload={"profile_name": "AI1", "worker_id": "architect"}),
            OrchestrationEvent(event_id="6", run_id=RunId("run-all"), type=EventType.PROFILE_RELEASED, payload={"profile_name": "AI1"}),
            OrchestrationEvent(event_id="7", run_id=RunId("run-all"), type=EventType.ROUND_COMPLETED, payload={"round_number": 1}),
        ]

        for e in events:
            sink.emit(e)

        output = stream.getvalue()
        self.assertIn("[assessment] completed: LARGE (ARCHITECTURE)", output)
        self.assertIn("[wave] Wave 1 started", output)
        self.assertIn("[action] requested RUN_WORKERS", output)
        self.assertIn("[action] accepted RUN_WORKERS", output)
        self.assertIn("[fleet] leased AI1 for architect", output)
        self.assertIn("[fleet] released AI1", output)
        self.assertIn("[wave] Wave 1 completed", output)


class TestFailureDistinctions(unittest.TestCase):
    """Verifies clear visual and textual distinction between all 5 failure states:

    FAILED, CANCELLED, RETRYING, REJECTED, INTERRUPTED
    """

    def test_failures_in_non_tty_mode(self) -> None:
        stream = io.StringIO()
        sink = TerminalEventSink(stream=stream, is_tty=False, run_id="run-fail-diff")

        # 1. FAILED
        sink.emit(
            OrchestrationEvent(
                event_id="f1",
                run_id=RunId("run-fail-diff"),
                type=EventType.INVOCATION_FAILED,
                payload={"worker_id": "architect", "error": "fatal syntax error", "failure": "UNRECOVERABLE"},
            )
        )
        # 2. CANCELLED
        sink.emit(
            OrchestrationEvent(
                event_id="f2",
                run_id=RunId("run-fail-diff"),
                type=EventType.INVOCATION_FAILED,
                payload={"worker_id": "testing", "status": "CANCELLED"},
            )
        )
        # 3. RETRYING
        sink.emit(
            OrchestrationEvent(
                event_id="f3",
                run_id=RunId("run-fail-diff"),
                type=EventType.INVOCATION_FAILED,
                payload={"worker_id": "alternative", "failure": "RETRYABLE"},
            )
        )
        # 4. REJECTED
        sink.emit(
            OrchestrationEvent(
                event_id="f4",
                run_id=RunId("run-fail-diff"),
                type=EventType.ACTION_REJECTED,
                payload={"reason": "fleet capacity quota exhausted"},
            )
        )
        # 5. INTERRUPTED
        sink.emit(
            OrchestrationEvent(
                event_id="f5",
                run_id=RunId("run-fail-diff"),
                type=EventType.RUN_INTERRUPTED,
                payload={"reason": "SIGINT signal received"},
            )
        )

        output = stream.getvalue()

        # Check all 5 tags are distinct and present
        self.assertIn("[worker] architect FAILED: fatal syntax error", output)
        self.assertIn("[worker] testing CANCELLED", output)
        self.assertIn("[worker] alternative RETRYING", output)
        self.assertIn("[action] REJECTED: fleet capacity quota exhausted", output)
        self.assertIn("[run] INTERRUPTED: SIGINT signal received", output)

    def test_failures_in_tty_mode(self) -> None:
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="run-fail-tty")

        sink.emit(
            OrchestrationEvent(
                event_id="f1",
                run_id=RunId("run-fail-tty"),
                type=EventType.INVOCATION_FAILED,
                payload={"worker_id": "architect", "role": "ARCHITECTURE", "profile_name": "AI3", "failure": "UNRECOVERABLE"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="f2",
                run_id=RunId("run-fail-tty"),
                type=EventType.INVOCATION_FAILED,
                payload={"worker_id": "testing", "role": "TESTING", "profile_name": "AI7", "status": "CANCELLED"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="f3",
                run_id=RunId("run-fail-tty"),
                type=EventType.INVOCATION_FAILED,
                payload={"worker_id": "alternative", "role": "ALTERNATIVE_DESIGN", "profile_name": "AI2", "failure": "RETRYABLE"},
            )
        )
        # Action rejected rejects pending workers
        sink.emit(
            OrchestrationEvent(
                event_id="f4_req",
                run_id=RunId("run-fail-tty"),
                type=EventType.ACTION_REQUESTED,
                payload={"workers": [{"worker_id": "auditor", "role": "AUDITOR"}]},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="f4_rej",
                run_id=RunId("run-fail-tty"),
                type=EventType.ACTION_REJECTED,
                payload={"reason": "quota limit"},
            )
        )
        # Run interrupted
        sink.emit(
            OrchestrationEvent(
                event_id="f5",
                run_id=RunId("run-fail-tty"),
                type=EventType.RUN_INTERRUPTED,
                payload={"reason": "user aborted"},
            )
        )

        rendered = sink.render(use_color=False)

        self.assertIn("architect    ✗ AI3   FAILED", rendered)
        self.assertIn("testing      ⊘ AI7   CANCELLED", rendered)
        self.assertIn("alternative  ↻ AI2   RETRYING", rendered)
        self.assertIn("auditor      ⚠       REJECTED", rendered)
        self.assertIn("⚡ Orchestration INTERRUPTED: user aborted", rendered)


class TestDryRunViewRendering(unittest.TestCase):
    """Verifies dry-run view renders all 7 required items:

    Task, Complexity, available capacity, initial requested wave,
    strategy, required concurrency, hard invocation limit
    """

    def test_render_dry_run_with_direct_arguments(self) -> None:
        output = render_dry_run(
            task="Refactor session storage",
            complexity="LARGE",
            available_capacity="8 available",
            initial_requested_wave=["architect", "testing", "alternative"],
            strategy="STANDARD",
            required_concurrency=3,
            hard_invocation_limit=20,
            use_color=False,
        )

        # Check all 7 required items
        self.assertIn("Task:", output)
        self.assertIn("Refactor session storage", output)

        self.assertIn("Complexity:", output)
        self.assertIn("LARGE", output)

        self.assertIn("available capacity:", output)
        self.assertIn("8 available", output)

        self.assertIn("initial requested wave:", output)
        self.assertIn("architect, testing, alternative", output)

        self.assertIn("strategy:", output)
        self.assertIn("STANDARD", output)

        self.assertIn("required concurrency:", output)
        self.assertIn("3", output)

        self.assertIn("hard invocation limit:", output)
        self.assertIn("20", output)

    def test_render_dry_run_with_dataclasses(self) -> None:
        assessment = TaskAssessment(
            task_type=TaskType.ARCHITECTURE,
            complexity=ComplexityLevel.VERY_LARGE,
            confidence=0.95,
            mutation_required=True,
            repository_scope="agym/",
            summary="Subsystem redesign",
            proposed_initial_work=["architect", "security_auditor"],
        )
        budget = OrchestrationBudget(max_invocations=35)
        fleet = FleetView(available_profiles=12)

        output = render_dry_run(
            assessment=assessment,
            budget=budget,
            fleet_view=fleet,
            strategy=ExecutionStrategy.HIGH_EFFORT,
            use_color=False,
        )

        self.assertIn("Task:", output)
        self.assertIn("Subsystem redesign", output)

        self.assertIn("Complexity:", output)
        self.assertIn("VERY_LARGE", output)

        self.assertIn("available capacity:", output)
        self.assertIn("12 available", output)

        self.assertIn("initial requested wave:", output)
        self.assertIn("architect, security_auditor", output)

        self.assertIn("strategy:", output)
        self.assertIn("HIGH_EFFORT", output)

        self.assertIn("required concurrency:", output)
        self.assertIn("2", output)

        self.assertIn("hard invocation limit:", output)
        self.assertIn("35", output)

    def test_sink_render_dry_run_method(self) -> None:
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="run-dry")
        sink.emit(
            OrchestrationEvent(
                event_id="e1",
                run_id=RunId("run-dry"),
                type=EventType.RUN_CREATED,
                payload={"task": "Method test task"},
            )
        )
        sink.emit(
            OrchestrationEvent(
                event_id="e2",
                run_id=RunId("run-dry"),
                type=EventType.TASK_ASSESSED,
                payload={"complexity": "MEDIUM"},
            )
        )

        out = sink.render_dry_run(
            initial_requested_wave=["architect"],
            available_capacity=5,
        )

        self.assertIn("Task:", out)
        self.assertIn("Method test task", out)
        self.assertIn("Complexity:", out)
        self.assertIn("MEDIUM", out)
        self.assertIn("available capacity:", out)
        self.assertIn("5 available", out)


class TestThreadSafetyAndEdgeCases(unittest.TestCase):
    """Verifies concurrent emissions, malformed payloads, and utilities."""

    def test_concurrent_event_emission(self) -> None:
        """Multiple threads emitting events concurrently must not crash or lose events."""
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="run-concurrent")

        def worker_thread(tid: int) -> None:
            for i in range(25):
                e = OrchestrationEvent(
                    event_id=f"t{tid}-{i}",
                    run_id=RunId("run-concurrent"),
                    type=EventType.INVOCATION_COMPLETED,
                    payload={"worker_id": f"worker-{tid}-{i}", "role": "GENERAL", "duration": 1.0},
                )
                sink.emit(e)

        threads = [threading.Thread(target=worker_thread, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = sink.get_events("run-concurrent")
        self.assertEqual(len(events), 100)
        self.assertEqual(len(sink.state.workers), 100)

    def test_malformed_and_unknown_events(self) -> None:
        sink = TerminalEventSink(stream=io.StringIO(), is_tty=False, use_color=False, run_id="run-unknown")

        # Event with empty payload
        sink.emit(OrchestrationEvent(event_id="empty", run_id=RunId("run-unknown"), type=EventType.RUN_CREATED, payload={}))
        self.assertEqual(sink.state.run_id, "run-unknown")

        # Event with arbitrary payload fields
        sink.emit(OrchestrationEvent(event_id="cust", run_id=RunId("run-unknown"), type=EventType.RUN_CREATED, payload={"custom_key": 123}))
        self.assertEqual(len(sink.get_events("run-unknown")), 2)

    def test_format_duration_helper(self) -> None:
        self.assertEqual(format_duration(None), "")
        self.assertEqual(format_duration(-5.0), "")
        self.assertEqual(format_duration(0.0), "0s")
        self.assertEqual(format_duration(42.3), "42s")
        self.assertEqual(format_duration(59.4), "59s")
        self.assertEqual(format_duration(60.0), "1m 0s")
        self.assertEqual(format_duration(72.6), "1m 13s")
        self.assertEqual(format_duration(3600.0), "60m 0s")

    def test_colorize_helper(self) -> None:
        self.assertEqual(colorize("hello", "\033[31m", use_color=False), "hello")
        colored = colorize("hello", "\033[31m", use_color=True)
        self.assertTrue(colored.startswith("\033[31m"))
        self.assertTrue(colored.endswith("\033[0m"))


if __name__ == "__main__":
    unittest.main()
