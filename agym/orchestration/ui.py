"""Terminal UI and EventSink for AGYM Orchestration subsystem.

Renders orchestration events in both TTY (interactive, animated) and Non-TTY (CI/log) modes.
Maintains internal presentation state derived purely from lifecycle events.
Provides dry-run plan inspection.

Never interacts with runner, scheduler, coordinator, or persistence modules.
"""

from __future__ import annotations

import io
import math
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence, TextIO

from agym.orchestration.contracts import (
    ActionId,
    ActionKind,
    ComplexityLevel,
    EventSink,
    EventType,
    ExecutionStrategy,
    FailureClass,
    FleetView,
    InvocationId,
    InvocationStatus,
    OrchestrationBudget,
    OrchestrationEvent,
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

__all__ = [
    "TerminalEventSink",
    "OrchestrationUI",
    "PresentationState",
    "WorkerPresentation",
    "WavePresentation",
    "WorkerStatus",
    "render_dry_run",
    "format_duration",
    "ROLE_DISPLAY_NAMES",
]

# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GRAY = "\033[90m"

# Spinner sequence starting with default braille frame
SPINNER_FRAMES = ["⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏", "⠋", "⠙"]
DEFAULT_SPINNER = "⠹"

# Status icons
ICON_SUCCESS = "✓"
ICON_SPINNER = "⠹"
ICON_FAILED = "✗"
ICON_CANCELLED = "⊘"
ICON_RETRYING = "↻"
ICON_REJECTED = "⚠"
ICON_INTERRUPTED = "⚡"
ICON_PENDING = "·"

# Friendly role display names
ROLE_DISPLAY_NAMES: dict[str, str] = {
    "ARCHITECTURE": "architect",
    "TESTING": "testing",
    "ALTERNATIVE_DESIGN": "alternative",
    "MINIMAL_CHANGE": "minimal_change",
    "DEBUGGING": "debugging",
    "SECURITY": "security",
    "PERFORMANCE": "performance",
    "MAINTAINABILITY": "maintainability",
    "IMPLEMENTATION_REVIEW": "review",
    "AUDITOR": "auditor",
    "SYNTHESIZER": "synthesizer",
    "EXECUTOR": "executor",
    "GENERAL": "general",
}


def colorize(text: str, color: str, use_color: bool = True) -> str:
    """Wraps text in ANSI color sequence if use_color is True."""
    if not use_color:
        return text
    return f"{color}{text}{RESET}"


def format_duration(seconds: float | None) -> str:
    """Formats duration in seconds into human-readable representation (e.g. 42s, 1m 12s)."""
    if seconds is None or seconds < 0:
        return ""
    secs = int(round(seconds))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    rem_secs = secs % 60
    return f"{mins}m {rem_secs}s"


def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parses an ISO timestamp string into a datetime object."""
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


class WorkerStatus(str, Enum):
    """Presentation state status for worker invocations."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RETRYING = "RETRYING"
    REJECTED = "REJECTED"
    INTERRUPTED = "INTERRUPTED"


@dataclass
class WorkerPresentation:
    """Visual representation of a worker task within a wave."""

    worker_id: str
    role: str = ""
    display_name: str = ""
    profile_name: str | None = None
    status: WorkerStatus = WorkerStatus.PENDING
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    wave_number: int = 1


@dataclass
class WavePresentation:
    """Visual representation of a wave / round of worker execution."""

    wave_number: int
    workers: list[WorkerPresentation] = field(default_factory=list)
    status: str = "RUNNING"


@dataclass
class PresentationState:
    """Self-contained presentation state maintained solely from events."""

    run_id: str = ""
    task: str = ""
    mode: str = ""
    run_status: str = "CREATED"
    complexity: str | None = None
    task_type: str | None = None
    assessment_done: bool = False
    fleet_available: int | None = None
    current_wave: int = 1
    waves: dict[int, WavePresentation] = field(default_factory=dict)
    workers: dict[str, WorkerPresentation] = field(default_factory=dict)
    failure_reason: str | None = None
    final_result: str | None = None


def render_dry_run(
    task: str = "",
    complexity: str | ComplexityLevel | None = None,
    available_capacity: int | str | None = None,
    initial_requested_wave: list[str] | list[WorkerRequest] | str | None = None,
    strategy: str | ExecutionStrategy | None = None,
    required_concurrency: int | str | None = None,
    hard_invocation_limit: int | str | None = None,
    *,
    assessment: TaskAssessment | None = None,
    budget: OrchestrationBudget | None = None,
    fleet_view: FleetView | None = None,
    use_color: bool = False,
) -> str:
    """Renders the Dry-run view of orchestration requirements and resources."""
    # Resolve Task
    task_str = task
    if not task_str and assessment and assessment.summary:
        task_str = assessment.summary
    if not task_str:
        task_str = "None"

    # Resolve Complexity
    complexity_str = ""
    if complexity is not None:
        complexity_str = complexity.value if isinstance(complexity, ComplexityLevel) else str(complexity)
    elif assessment is not None:
        complexity_str = assessment.complexity.value
    else:
        complexity_str = "UNKNOWN"

    # Resolve Available Capacity
    if available_capacity is not None:
        if isinstance(available_capacity, int):
            capacity_str = f"{available_capacity} available"
        else:
            capacity_str = str(available_capacity)
    elif fleet_view is not None:
        capacity_str = f"{fleet_view.available_profiles} available"
    else:
        capacity_str = "0 available"

    # Resolve Initial Requested Wave
    wave_items: list[str] = []
    if initial_requested_wave is not None:
        if isinstance(initial_requested_wave, list):
            for item in initial_requested_wave:
                if isinstance(item, WorkerRequest):
                    role_name = ROLE_DISPLAY_NAMES.get(item.role.value, item.role.value.lower())
                    wave_items.append(role_name)
                elif hasattr(item, "role"):
                    r = getattr(item, "role")
                    r_val = r.value if hasattr(r, "value") else str(r)
                    wave_items.append(ROLE_DISPLAY_NAMES.get(r_val, r_val.lower()))
                else:
                    wave_items.append(str(item))
        else:
            wave_items = [str(initial_requested_wave)]
    elif assessment is not None and assessment.proposed_initial_work:
        wave_items = [str(w) for w in assessment.proposed_initial_work]

    wave_str = ", ".join(wave_items) if wave_items else "None"

    # Resolve Strategy
    if strategy is not None:
        strategy_str = strategy.value if isinstance(strategy, ExecutionStrategy) else str(strategy)
    else:
        strategy_str = ExecutionStrategy.STANDARD.value

    # Resolve Required Concurrency
    if required_concurrency is not None:
        concurrency_str = str(required_concurrency)
    elif wave_items:
        concurrency_str = str(len(wave_items))
    else:
        concurrency_str = "1"

    # Resolve Hard Invocation Limit
    if hard_invocation_limit is not None:
        limit_str = str(hard_invocation_limit)
    elif budget is not None:
        limit_str = str(budget.max_invocations)
    else:
        limit_str = "20"

    header = colorize("AGYM Orchestrator · Dry Run", BOLD + CYAN, use_color)

    # 7 Required Fields
    fields = [
        ("Task", task_str),
        ("Complexity", complexity_str),
        ("available capacity", capacity_str),
        ("initial requested wave", wave_str),
        ("strategy", strategy_str),
        ("required concurrency", concurrency_str),
        ("hard invocation limit", limit_str),
    ]

    lines = [header, ""]
    label_width = 24
    for label, val in fields:
        colon_label = f"{label}:"
        formatted_label = colorize(colon_label.ljust(label_width), BOLD, use_color)
        formatted_val = colorize(val, GREEN if label in ("Complexity", "available capacity") else "", use_color)
        lines.append(f"{formatted_label}{formatted_val}")

    return "\n".join(lines)


class TerminalEventSink(EventSink):
    """Terminal UI rendering EventSink for AGYM Orchestration events.

    Supports both interactive TTY rendering and non-TTY log capture.
    Maintains internal presentation state derived solely from lifecycle events.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        is_tty: bool | None = None,
        use_color: bool | None = None,
        run_id: RunId | str | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        if is_tty is not None:
            self._is_tty = bool(is_tty)
        else:
            self._is_tty = hasattr(self._stream, "isatty") and self._stream.isatty()

        if use_color is not None:
            self._use_color = bool(use_color)
        else:
            no_color = bool(os.environ.get("NO_COLOR", ""))
            self._use_color = self._is_tty and not no_color

        self.state = PresentationState()
        if run_id:
            self.state.run_id = str(run_id)

        self._events: list[OrchestrationEvent] = []
        self._lock = threading.Lock()
        self._last_rendered_line_count = 0
        self._spinner_idx = 0

    @property
    def is_tty(self) -> bool:
        return self._is_tty

    @property
    def use_color(self) -> bool:
        return self._use_color

    def emit(self, event: OrchestrationEvent) -> None:
        """Emits an orchestration event to update state and render UI."""
        with self._lock:
            self._events.append(event)
            self._handle_event(event)

            if self._is_tty:
                self._render_tty_update()
            else:
                self._log_non_tty(event)

    def get_events(self, run_id: RunId | str) -> list[OrchestrationEvent]:
        """Returns all events recorded for a given run."""
        with self._lock:
            rid = str(run_id)
            return [e for e in self._events if str(e.run_id) == rid]

    def render(self, use_color: bool | None = None) -> str:
        """Renders the current presentation state into string format."""
        with self._lock:
            return self._format_tty_view(
                use_color=self._use_color if use_color is None else use_color
            )

    def render_dry_run(
        self,
        task: str = "",
        complexity: str | ComplexityLevel | None = None,
        available_capacity: int | str | None = None,
        initial_requested_wave: list[str] | list[WorkerRequest] | str | None = None,
        strategy: str | ExecutionStrategy | None = None,
        required_concurrency: int | str | None = None,
        hard_invocation_limit: int | str | None = None,
        *,
        assessment: TaskAssessment | None = None,
        budget: OrchestrationBudget | None = None,
        fleet_view: FleetView | None = None,
        use_color: bool | None = None,
    ) -> str:
        """Renders dry-run output using UI's color preferences."""
        color = self._use_color if use_color is None else use_color
        return render_dry_run(
            task=task or self.state.task,
            complexity=complexity or self.state.complexity,
            available_capacity=available_capacity or self.state.fleet_available,
            initial_requested_wave=initial_requested_wave,
            strategy=strategy,
            required_concurrency=required_concurrency,
            hard_invocation_limit=hard_invocation_limit,
            assessment=assessment,
            budget=budget,
            fleet_view=fleet_view,
            use_color=color,
        )

    # ========================================================================
    # Event Handling & State Derivation
    # ========================================================================

    def _resolve_worker_display_name(self, worker_id: str, role: str | None) -> str:
        """Resolves friendly display name for a worker."""
        if role:
            role_str = role.value if hasattr(role, "value") else str(role)
            norm = role_str.upper()
            if norm in ROLE_DISPLAY_NAMES:
                # If worker_id is generic or role-like, prefer role display name
                w_lower = worker_id.lower()
                if (
                    w_lower.startswith("worker")
                    or w_lower == norm.lower()
                    or w_lower == ROLE_DISPLAY_NAMES[norm]
                ):
                    return ROLE_DISPLAY_NAMES[norm]
        # Check if worker_id itself is a role name
        w_norm = worker_id.upper()
        if w_norm in ROLE_DISPLAY_NAMES:
            return ROLE_DISPLAY_NAMES[w_norm]
        return worker_id

    def _ensure_wave(self, wave_num: int) -> WavePresentation:
        """Ensures wave container exists in presentation state."""
        if wave_num not in self.state.waves:
            self.state.waves[wave_num] = WavePresentation(wave_number=wave_num)
        return self.state.waves[wave_num]

    def _ensure_worker(
        self,
        worker_id: str,
        role: str | None = None,
        wave_number: int | None = None,
    ) -> WorkerPresentation:
        """Ensures worker container exists in presentation state and wave."""
        wave_num = wave_number if wave_number is not None else self.state.current_wave
        wave = self._ensure_wave(wave_num)

        if worker_id in self.state.workers:
            wp = self.state.workers[worker_id]
            if role and not wp.role:
                wp.role = role.value if hasattr(role, "value") else str(role)
                wp.display_name = self._resolve_worker_display_name(worker_id, wp.role)
            return wp

        display_name = self._resolve_worker_display_name(worker_id, role)
        wp = WorkerPresentation(
            worker_id=worker_id,
            role=role.value if hasattr(role, "value") else str(role or ""),
            display_name=display_name,
            wave_number=wave_num,
        )
        self.state.workers[worker_id] = wp
        wave.workers.append(wp)
        return wp

    def _extract_fleet_info(self, payload: dict[str, Any]) -> None:
        """Extracts fleet capacity information if present in payload."""
        if "fleet_view" in payload:
            fv = payload["fleet_view"]
            if isinstance(fv, dict):
                self.state.fleet_available = fv.get("available_profiles")
            elif hasattr(fv, "available_profiles"):
                self.state.fleet_available = getattr(fv, "available_profiles")
        elif "available_profiles" in payload:
            try:
                self.state.fleet_available = int(payload["available_profiles"])
            except (ValueError, TypeError):
                pass
        elif "fleet" in payload:
            f = payload["fleet"]
            if isinstance(f, dict) and "available_profiles" in f:
                try:
                    self.state.fleet_available = int(f["available_profiles"])
                except (ValueError, TypeError):
                    pass

    def _handle_event(self, event: OrchestrationEvent) -> None:
        """Derives UI presentation state updates from lifecycle events."""
        if not self.state.run_id:
            self.state.run_id = str(event.run_id)

        etype = event.type
        payload = event.payload or {}
        self._extract_fleet_info(payload)

        if etype == EventType.RUN_CREATED:
            self.state.run_status = RunStatus.CREATED.value
            if "task" in payload:
                self.state.task = str(payload["task"])
            if "mode" in payload:
                self.state.mode = str(payload["mode"])

        elif etype == EventType.TASK_ASSESSED:
            self.state.assessment_done = True
            ass = payload.get("assessment")
            if ass is not None:
                if hasattr(ass, "complexity"):
                    self.state.complexity = ass.complexity.value if hasattr(ass.complexity, "value") else str(ass.complexity).upper()
                elif isinstance(ass, dict) and "complexity" in ass:
                    self.state.complexity = str(ass["complexity"]).upper()
                if hasattr(ass, "task_type"):
                    self.state.task_type = ass.task_type.value if hasattr(ass.task_type, "value") else str(ass.task_type).upper()
                elif isinstance(ass, dict) and "task_type" in ass:
                    self.state.task_type = str(ass["task_type"]).upper()
                if hasattr(ass, "summary") and ass.summary:
                    self.state.task = str(ass.summary)
                elif isinstance(ass, dict) and ass.get("summary"):
                    self.state.task = str(ass["summary"])
            if "complexity" in payload:
                self.state.complexity = str(payload["complexity"]).upper()
            if "task_type" in payload:
                self.state.task_type = str(payload["task_type"]).upper()

        elif etype == EventType.ROUND_STARTED:
            round_num = payload.get("round_number") or payload.get("round") or payload.get("wave")
            if round_num is not None:
                try:
                    self.state.current_wave = int(round_num)
                except (ValueError, TypeError):
                    pass
            self._ensure_wave(self.state.current_wave)

        elif etype == EventType.ROUND_COMPLETED:
            round_num = payload.get("round_number") or payload.get("round") or payload.get("wave")
            if round_num is not None:
                try:
                    wn = int(round_num)
                    if wn in self.state.waves:
                        self.state.waves[wn].status = "COMPLETED"
                except (ValueError, TypeError):
                    pass

        elif etype in (EventType.ACTION_REQUESTED, EventType.ACTION_ACCEPTED):
            act = payload.get("action")
            workers_list = payload.get("workers") or (getattr(act, "workers", []) if act else [])
            for w in workers_list:
                wid = w.get("worker_id") if isinstance(w, dict) else getattr(w, "worker_id", None)
                wrole = w.get("role") if isinstance(w, dict) else getattr(w, "role", None)
                if wid:
                    self._ensure_worker(str(wid), wrole, self.state.current_wave)

            auditors_list = payload.get("auditors") or (getattr(act, "auditors", []) if act else [])
            for a in auditors_list:
                aid = a.get("worker_id") if isinstance(a, dict) else getattr(a, "worker_id", None)
                if aid:
                    self._ensure_worker(str(aid), WorkerRole.AUDITOR, self.state.current_wave)

        elif etype == EventType.ACTION_REJECTED:
            reason = payload.get("reason", "Action rejected")
            self.state.failure_reason = str(reason)
            for w in self.state.workers.values():
                if w.status == WorkerStatus.PENDING:
                    w.status = WorkerStatus.REJECTED
                    w.error_message = reason

        elif etype == EventType.PROFILE_LEASED:
            lease = payload.get("lease")
            p_name = payload.get("profile_name") or payload.get("profile") or (getattr(lease, "profile_name", None) if lease else None)
            wid = payload.get("worker_id") or payload.get("worker") or (getattr(lease, "worker_id", None) if lease else None)
            if wid and p_name:
                wp = self._ensure_worker(str(wid))
                wp.profile_name = str(p_name)

        elif etype == EventType.INVOCATION_STARTED:
            inv = payload.get("invocation")
            wid = payload.get("worker_id") or (getattr(inv, "worker_id", None) if inv else None)
            role = payload.get("role") or (getattr(inv, "role", None) if inv else None)
            if wid:
                wp = self._ensure_worker(str(wid), role)
                wp.status = WorkerStatus.RUNNING
                wp.started_at = payload.get("started_at") or event.timestamp
                if "profile_name" in payload and payload["profile_name"]:
                    wp.profile_name = str(payload["profile_name"])

        elif etype == EventType.INVOCATION_COMPLETED:
            res = payload.get("result")
            wid = payload.get("worker_id") or (getattr(res, "worker_id", None) if res else None)
            role = payload.get("role") or (getattr(res, "role", None) if res else None)
            if wid:
                wp = self._ensure_worker(str(wid), role)
                wp.status = WorkerStatus.SUCCEEDED
                wp.completed_at = payload.get("completed_at") or event.timestamp
                if "profile_name" in payload and payload["profile_name"]:
                    wp.profile_name = str(payload["profile_name"])
                if "duration" in payload or "duration_seconds" in payload:
                    try:
                        wp.duration_seconds = float(payload.get("duration") or payload.get("duration_seconds"))
                    except (ValueError, TypeError):
                        pass
                if wp.duration_seconds is None and wp.started_at and wp.completed_at:
                    st = _parse_timestamp(wp.started_at)
                    et = _parse_timestamp(wp.completed_at)
                    if st and et:
                        wp.duration_seconds = max(0.0, (et - st).total_seconds())

        elif etype == EventType.INVOCATION_FAILED:
            res = payload.get("result")
            wid = payload.get("worker_id") or (getattr(res, "worker_id", None) if res else None)
            role = payload.get("role") or (getattr(res, "role", None) if res else None)
            if wid:
                wp = self._ensure_worker(str(wid), role)
                if "profile_name" in payload and payload["profile_name"]:
                    wp.profile_name = str(payload["profile_name"])
                error = payload.get("error") or (getattr(res, "error", None) if res else None) or ""
                failure = payload.get("failure") or (getattr(res, "failure", None) if res else None) or ""
                if hasattr(failure, "value"):
                    failure = failure.value
                is_cancelled = (
                    payload.get("cancelled")
                    or payload.get("status") == InvocationStatus.CANCELLED.value
                )
                is_rejected = (
                    payload.get("rejected")
                    or payload.get("status") == "REJECTED"
                )
                is_retrying = (
                    payload.get("retrying")
                    or failure == FailureClass.RETRYABLE.value
                )
                is_interrupted = (
                    payload.get("interrupted")
                    or payload.get("status") == "INTERRUPTED"
                )

                if is_cancelled:
                    wp.status = WorkerStatus.CANCELLED
                elif is_rejected:
                    wp.status = WorkerStatus.REJECTED
                elif is_retrying:
                    wp.status = WorkerStatus.REJECTED if failure == "REJECTED" else WorkerStatus.RETRYING
                elif is_interrupted:
                    wp.status = WorkerStatus.INTERRUPTED
                else:
                    wp.status = WorkerStatus.FAILED

                wp.error_message = str(error) if error else None
                wp.completed_at = payload.get("completed_at") or event.timestamp
                if wp.started_at and wp.completed_at:
                    st = _parse_timestamp(wp.started_at)
                    et = _parse_timestamp(wp.completed_at)
                    if st and et:
                        wp.duration_seconds = max(0.0, (et - st).total_seconds())

        elif etype == EventType.RUN_COMPLETED:
            self.state.run_status = RunStatus.COMPLETED.value
            self.state.final_result = payload.get("summary") or payload.get("final_result")

        elif etype == EventType.RUN_FAILED:
            self.state.run_status = RunStatus.FAILED.value
            self.state.failure_reason = payload.get("error") or payload.get("reason")
            for w in self.state.workers.values():
                if w.status == WorkerStatus.RUNNING:
                    w.status = WorkerStatus.FAILED

        elif etype == EventType.RUN_INTERRUPTED:
            self.state.run_status = RunStatus.INTERRUPTED.value
            self.state.failure_reason = payload.get("reason") or "Run interrupted"
            active_ids = payload.get("active_invocations", [])
            for wid, w in self.state.workers.items():
                if w.status == WorkerStatus.RUNNING or wid in active_ids:
                    w.status = WorkerStatus.INTERRUPTED

    # ========================================================================
    # TTY Rendering Mode
    # ========================================================================

    def _format_tty_view(self, use_color: bool) -> str:
        """Formats the interactive TTY view."""
        lines: list[str] = []

        # 1. Header: AGYM Orchestrator · RUN-ID
        run_label = self.state.run_id or "PENDING"
        header = f"AGYM Orchestrator · {run_label}"
        lines.append(colorize(header, BOLD + CYAN, use_color))
        lines.append("")

        # 2. Assessment
        label_assessment = "Assessment".ljust(15)
        if self.state.assessment_done and self.state.complexity:
            icon = colorize(ICON_SUCCESS, GREEN, use_color)
            val = colorize(self.state.complexity, BOLD, use_color)
            lines.append(f"{label_assessment}{icon} {val}")
        elif self.state.run_status == RunStatus.FAILED.value:
            icon = colorize(ICON_FAILED, RED, use_color)
            val = colorize("FAILED", RED + BOLD, use_color)
            lines.append(f"{label_assessment}{icon} {val}")
        else:
            icon = colorize(ICON_SPINNER, YELLOW, use_color)
            val = colorize("Assessing...", DIM, use_color)
            lines.append(f"{label_assessment}{icon} {val}")

        # 3. Fleet
        label_fleet = "Fleet".ljust(15)
        if self.state.fleet_available is not None:
            if self.state.fleet_available > 0:
                icon = colorize(ICON_SUCCESS, GREEN, use_color)
                val = f"{self.state.fleet_available} available"
                lines.append(f"{label_fleet}{icon} {val}")
            else:
                icon = colorize(ICON_REJECTED, YELLOW, use_color)
                val = colorize("0 available", YELLOW, use_color)
                lines.append(f"{label_fleet}{icon} {val}")
        else:
            icon = colorize(ICON_PENDING, GRAY, use_color)
            lines.append(f"{label_fleet}{icon} checking")

        lines.append("")

        # 4. Waves
        for wave_num in sorted(self.state.waves.keys()):
            wave = self.state.waves[wave_num]
            lines.append(colorize(f"Wave {wave_num}", BOLD, use_color))

            for w in wave.workers:
                name_col = w.display_name.ljust(13)

                if w.status == WorkerStatus.SUCCEEDED:
                    icon = colorize(ICON_SUCCESS, GREEN, use_color)
                    p_name = (w.profile_name or "").ljust(6)
                    dur = format_duration(w.duration_seconds)
                    line = f"  {name_col}{icon} {p_name}{dur}".rstrip()

                elif w.status == WorkerStatus.RUNNING:
                    spinner_char = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
                    icon = colorize(spinner_char, YELLOW, use_color)
                    p_name = w.profile_name or ""
                    line = f"  {name_col}{icon} {p_name}".rstrip()

                elif w.status == WorkerStatus.FAILED:
                    icon = colorize(ICON_FAILED, RED, use_color)
                    p_name = (w.profile_name or "").ljust(6)
                    status_text = colorize("FAILED", RED + BOLD, use_color)
                    line = f"  {name_col}{icon} {p_name}{status_text}"

                elif w.status == WorkerStatus.CANCELLED:
                    icon = colorize(ICON_CANCELLED, GRAY, use_color)
                    p_name = (w.profile_name or "").ljust(6)
                    status_text = colorize("CANCELLED", GRAY, use_color)
                    line = f"  {name_col}{icon} {p_name}{status_text}"

                elif w.status == WorkerStatus.RETRYING:
                    icon = colorize(ICON_RETRYING, YELLOW, use_color)
                    p_name = (w.profile_name or "").ljust(6)
                    status_text = colorize("RETRYING", YELLOW + BOLD, use_color)
                    line = f"  {name_col}{icon} {p_name}{status_text}"

                elif w.status == WorkerStatus.REJECTED:
                    icon = colorize(ICON_REJECTED, MAGENTA, use_color)
                    p_name = (w.profile_name or "").ljust(6)
                    status_text = colorize("REJECTED", MAGENTA + BOLD, use_color)
                    line = f"  {name_col}{icon} {p_name}{status_text}"

                elif w.status == WorkerStatus.INTERRUPTED:
                    icon = colorize(ICON_INTERRUPTED, RED, use_color)
                    p_name = (w.profile_name or "").ljust(6)
                    status_text = colorize("INTERRUPTED", RED + BOLD, use_color)
                    line = f"  {name_col}{icon} {p_name}{status_text}"

                else:  # PENDING
                    icon = colorize(ICON_PENDING, GRAY, use_color)
                    p_name = w.profile_name or ""
                    line = f"  {name_col}{icon} {p_name}".rstrip()

                lines.append(line)

            lines.append("")

        # Trailing run status if finished
        if self.state.run_status == RunStatus.COMPLETED.value:
            lines.append(colorize("✓ Orchestration completed successfully.", GREEN + BOLD, use_color))
        elif self.state.run_status == RunStatus.FAILED.value:
            reason = f": {self.state.failure_reason}" if self.state.failure_reason else ""
            lines.append(colorize(f"✗ Orchestration FAILED{reason}", RED + BOLD, use_color))
        elif self.state.run_status == RunStatus.INTERRUPTED.value:
            reason = f": {self.state.failure_reason}" if self.state.failure_reason else ""
            lines.append(colorize(f"⚡ Orchestration INTERRUPTED{reason}", YELLOW + BOLD, use_color))

        return "\n".join(lines).rstrip() + "\n"

    def _render_tty_update(self) -> None:
        """Rerenders TTY view in-place in active terminal."""
        rendered = self._format_tty_view(use_color=self._use_color)
        lines = rendered.splitlines()

        # In-place repositioning if previously rendered
        if self._last_rendered_line_count > 0:
            self._stream.write(f"\033[{self._last_rendered_line_count}A\r")

        for line in lines:
            self._stream.write(f"\033[K{line}\n")

        self._stream.write("\033[J")
        self._stream.flush()
        self._last_rendered_line_count = len(lines)
        self._spinner_idx += 1

    # ========================================================================
    # Non-TTY Rendering Mode (CI / Log capture)
    # ========================================================================

    def _format_non_tty_line(self, event: OrchestrationEvent) -> str | None:
        """Formats a single non-animated log entry for an event."""
        etype = event.type
        payload = event.payload or {}

        if etype == EventType.RUN_CREATED:
            task = payload.get("task", "")
            snippet = f" - {task[:60]}..." if len(task) > 60 else (f" - {task}" if task else "")
            return f"[run] created {event.run_id}{snippet}"

        if etype == EventType.TASK_ASSESSED:
            comp = payload.get("complexity", "")
            tt = payload.get("task_type", "")
            ass = payload.get("assessment")
            if ass is not None:
                if not comp:
                    comp = getattr(ass, "complexity", "")
                    if hasattr(comp, "value"):
                        comp = comp.value
                    elif isinstance(ass, dict):
                        comp = ass.get("complexity", "")
                if not tt:
                    tt = getattr(ass, "task_type", "")
                    if hasattr(tt, "value"):
                        tt = tt.value
                    elif isinstance(ass, dict):
                        tt = ass.get("task_type", "")
            return f"[assessment] completed: {comp} ({tt})"

        if etype == EventType.ROUND_STARTED:
            wn = payload.get("round_number") or payload.get("round") or payload.get("wave") or self.state.current_wave
            return f"[wave] Wave {wn} started"

        if etype == EventType.ROUND_COMPLETED:
            wn = payload.get("round_number") or payload.get("round") or payload.get("wave") or self.state.current_wave
            return f"[wave] Wave {wn} completed"

        if etype == EventType.ACTION_REQUESTED:
            act = payload.get("action")
            kind = payload.get("kind") or (getattr(act, "kind", "") if act else "")
            if hasattr(kind, "value"):
                kind = kind.value
            return f"[action] requested {kind}".rstrip()

        if etype == EventType.ACTION_ACCEPTED:
            act = payload.get("action")
            kind = payload.get("kind") or (getattr(act, "kind", "") if act else "")
            if hasattr(kind, "value"):
                kind = kind.value
            return f"[action] accepted {kind}".rstrip()

        if etype == EventType.ACTION_REJECTED:
            reason = payload.get("reason", "")
            return f"[action] REJECTED: {reason}"

        if etype == EventType.PROFILE_LEASED:
            lease = payload.get("lease")
            p_name = payload.get("profile_name") or payload.get("profile") or (getattr(lease, "profile_name", "") if lease else "")
            wid = payload.get("worker_id") or payload.get("worker") or (getattr(lease, "worker_id", "") if lease else "")
            return f"[fleet] leased {p_name} for {wid}"

        if etype == EventType.PROFILE_RELEASED:
            p_name = payload.get("profile_name") or payload.get("profile") or ""
            return f"[fleet] released {p_name}"

        if etype == EventType.INVOCATION_STARTED:
            inv = payload.get("invocation")
            wid = str(payload.get("worker_id") or (getattr(inv, "worker_id", "") if inv else ""))
            role = payload.get("role") or (getattr(inv, "role", None) if inv else None)
            name = self._resolve_worker_display_name(wid, role)
            p_name = payload.get("profile_name")
            p_info = f" ({p_name})" if p_name else ""
            return f"[worker] {name} started{p_info}"

        if etype == EventType.INVOCATION_COMPLETED:
            res = payload.get("result")
            wid = str(payload.get("worker_id") or (getattr(res, "worker_id", "") if res else ""))
            role = payload.get("role") or (getattr(res, "role", None) if res else None)
            name = self._resolve_worker_display_name(wid, role)
            dur = payload.get("duration") or payload.get("duration_seconds")
            dur_info = f" ({int(round(float(dur)))}s)" if dur is not None else ""
            return f"[worker] {name} completed{dur_info}"

        if etype == EventType.INVOCATION_FAILED:
            res = payload.get("result")
            wid = str(payload.get("worker_id") or (getattr(res, "worker_id", "") if res else ""))
            role = payload.get("role") or (getattr(res, "role", None) if res else None)
            name = self._resolve_worker_display_name(wid, role)
            error = payload.get("error") or (getattr(res, "error", "") if res else "")
            failure = payload.get("failure") or (getattr(res, "failure", "") if res else "")
            is_cancelled = payload.get("cancelled") or payload.get("status") == InvocationStatus.CANCELLED.value
            is_rejected = payload.get("rejected") or payload.get("status") == "REJECTED" or failure == "REJECTED"
            is_retrying = payload.get("retrying") or failure == FailureClass.RETRYABLE.value
            is_interrupted = payload.get("interrupted") or payload.get("status") == "INTERRUPTED"

            if is_cancelled:
                tag = "CANCELLED"
            elif is_rejected:
                tag = "REJECTED"
            elif is_retrying:
                tag = "RETRYING"
            elif is_interrupted:
                tag = "INTERRUPTED"
            else:
                tag = "FAILED"

            err_info = f": {error}" if error else ""
            return f"[worker] {name} {tag}{err_info}"

        if etype == EventType.RUN_COMPLETED:
            summary = payload.get("summary") or ""
            s_info = f": {summary[:80]}" if summary else ""
            return f"[run] completed{s_info}"

        if etype == EventType.RUN_FAILED:
            err = payload.get("error") or payload.get("reason") or ""
            return f"[run] FAILED: {err}"

        if etype == EventType.RUN_INTERRUPTED:
            reason = payload.get("reason") or "Run interrupted"
            return f"[run] INTERRUPTED: {reason}"

        # Unknown / generic fallback
        etype_str = etype.value if hasattr(etype, "value") else str(etype)
        return f"[event] {etype_str}"

    def _log_non_tty(self, event: OrchestrationEvent) -> None:
        """Writes non-animated log entry to stream."""
        line = self._format_non_tty_line(event)
        if line:
            self._stream.write(line + "\n")
            self._stream.flush()


# Public alias for TerminalEventSink
OrchestrationUI = TerminalEventSink
