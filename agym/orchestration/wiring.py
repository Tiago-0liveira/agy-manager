"""Centralized dependency construction and conservative V1 defaults for AGYM Orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence, TextIO

from agym.cache import CacheManager
from agym.orchestration.contracts import (
    AuditResult,
    CoordinatorClient as CoordinatorClientProtocol,
    EventSink,
    ModelRunner,
    OrchestrationBudget,
    OrchestrationEvent,
    ProfileLeaseManager as ProfileLeaseManagerProtocol,
    ProfileScheduler as ProfileSchedulerProtocol,
    RunId,
    RunState,
    RunStore,
    WorkerResult,
)
from agym.orchestration.coordinator import CoordinatorClient
from agym.orchestration.engine import OrchestrationEngine
from agym.orchestration.leases import ProfileLeaseManager
from agym.orchestration.persistence import FileRunStore
from agym.orchestration.runner import AntigravityRunner
from agym.orchestration.scheduler import ProfileScheduler
from agym.orchestration.ui import TerminalEventSink
from agym.profiles import ProfileStore

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_PARALLEL",
    "DEFAULT_MAX_INVOCATIONS",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MAX_BOOST_INVOCATIONS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_RUNTIME_SECONDS",
    "DEFAULT_MIN_QUOTA_REMAINING_PERCENT",
    "DEFAULT_MIN_QUOTA_RESERVE_FRACTION",
    "BroadcastRunStore",
    "OrchestrationDependencies",
    "build_default_budget",
    "build_orchestration_dependencies",
]

# ============================================================================
# Conservative V1 Defaults
# ============================================================================

DEFAULT_MAX_PARALLEL: int = 4
DEFAULT_MAX_INVOCATIONS: int = 20
DEFAULT_MAX_ROUNDS: int = 10
DEFAULT_MAX_BOOST_INVOCATIONS: int = 2
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_MAX_RUNTIME_SECONDS: float = 1800.0  # 30 minutes
DEFAULT_MIN_QUOTA_REMAINING_PERCENT: float = 10.0  # 10% min reserve for budget
DEFAULT_MIN_QUOTA_RESERVE_FRACTION: float = 0.05  # 5% reserve for scheduler


def build_default_budget() -> OrchestrationBudget:
    """Create an OrchestrationBudget configured with conservative V1 defaults."""
    return OrchestrationBudget(
        max_parallel=DEFAULT_MAX_PARALLEL,
        max_invocations=DEFAULT_MAX_INVOCATIONS,
        max_rounds=DEFAULT_MAX_ROUNDS,
        max_boost_invocations=DEFAULT_MAX_BOOST_INVOCATIONS,
        max_retries=DEFAULT_MAX_RETRIES,
        max_runtime_seconds=DEFAULT_MAX_RUNTIME_SECONDS,
        min_quota_remaining=DEFAULT_MIN_QUOTA_REMAINING_PERCENT,
    )


# ============================================================================
# Event Broadcasting RunStore Adapter
# ============================================================================


class BroadcastRunStore:
    """RunStore adapter that writes events to underlying store and broadcasts to an EventSink."""

    def __init__(self, store: RunStore, event_sink: EventSink | None = None) -> None:
        self._store = store
        self._event_sink = event_sink

    @property
    def inner_store(self) -> RunStore:
        return self._store

    @property
    def event_sink(self) -> EventSink | None:
        return self._event_sink

    def save_run(self, state: RunState) -> None:
        self._store.save_run(state)

    def get_run(self, run_id: RunId) -> RunState | None:
        return self._store.get_run(run_id)

    def load_run(self, run_id: RunId | str) -> RunState:
        if hasattr(self._store, "load_run"):
            return self._store.load_run(run_id)
        state = self._store.get_run(RunId(run_id))
        if state is None:
            raise KeyError(f"Run '{run_id}' not found")
        return state

    def list_runs(self, limit: int = 100) -> list[RunState]:
        return self._store.list_runs(limit=limit)

    def save_result(self, run_id: RunId, result: WorkerResult | AuditResult) -> None:
        self._store.save_result(run_id, result)

    def get_results(self, run_id: RunId) -> list[WorkerResult | AuditResult]:
        return self._store.get_results(run_id)

    def emit(self, event: OrchestrationEvent) -> None:
        if hasattr(self._store, "emit"):
            try:
                self._store.emit(event)
            except Exception as exc:
                logger.warning("Underlying store failed to emit event: %s", exc)
        if self._event_sink is not None:
            try:
                self._event_sink.emit(event)
            except Exception as exc:
                logger.warning("EventSink failed to emit event: %s", exc)

    def get_events(self, run_id: RunId) -> list[OrchestrationEvent]:
        if hasattr(self._store, "get_events"):
            return self._store.get_events(run_id)
        if self._event_sink is not None and hasattr(self._event_sink, "get_events"):
            return self._event_sink.get_events(run_id)
        return []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


# ============================================================================
# Centralized Orchestration Dependencies Container
# ============================================================================


@dataclass
class OrchestrationDependencies:
    """Container holding all wired subsystems for orchestration execution."""

    profile_store: ProfileStore
    cache_manager: CacheManager
    lease_manager: ProfileLeaseManagerProtocol
    scheduler: ProfileSchedulerProtocol
    run_store: RunStore
    runner: ModelRunner
    coordinator: CoordinatorClientProtocol
    event_sink: EventSink
    engine: OrchestrationEngine
    budget: OrchestrationBudget


def build_orchestration_dependencies(
    *,
    profile_store: ProfileStore | None = None,
    cache_manager: CacheManager | None = None,
    lease_manager: ProfileLeaseManagerProtocol | None = None,
    scheduler: ProfileSchedulerProtocol | None = None,
    run_store: RunStore | None = None,
    runner: ModelRunner | None = None,
    coordinator: CoordinatorClientProtocol | None = None,
    event_sink: EventSink | None = None,
    engine: OrchestrationEngine | None = None,
    budget: OrchestrationBudget | None = None,
    stream: TextIO | None = None,
    is_tty: bool | None = None,
    use_color: bool | None = None,
    min_reserve: float | None = None,
) -> OrchestrationDependencies:
    """Centralized factory for constructing orchestration subsystems.

    Initializes components predictably and avoids duplicate initialization across CLI branches.
    Allows injecting mock components for tests.
    """
    p_store = profile_store or ProfileStore()
    c_mgr = cache_manager or CacheManager()
    l_mgr = lease_manager or ProfileLeaseManager()
    sched = scheduler or ProfileScheduler(
        profile_store=p_store,
        lease_manager=l_mgr,
        cache_manager=c_mgr,
        min_reserve=min_reserve if min_reserve is not None else DEFAULT_MIN_QUOTA_RESERVE_FRACTION,
    )
    r_store = run_store or FileRunStore()
    e_sink = event_sink or TerminalEventSink(stream=stream, is_tty=is_tty, use_color=use_color)
    effective_store = BroadcastRunStore(r_store, e_sink)
    mdl_runner = runner or AntigravityRunner(profile_store=p_store)
    coord = coordinator or CoordinatorClient(runner=mdl_runner, run_store=r_store)
    bgt = budget or build_default_budget()
    eng = engine or OrchestrationEngine(
        scheduler=sched,
        lease_manager=l_mgr,
        store=effective_store,
        runner=mdl_runner,
        coordinator=coord,
        default_budget=bgt,
    )
    return OrchestrationDependencies(
        profile_store=p_store,
        cache_manager=c_mgr,
        lease_manager=l_mgr,
        scheduler=sched,
        run_store=r_store,
        runner=mdl_runner,
        coordinator=coord,
        event_sink=e_sink,
        engine=eng,
        budget=bgt,
    )
