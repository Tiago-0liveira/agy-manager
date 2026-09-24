"""Periodic quota refresh for the live orchestration TUI.

This intentionally reuses the normal usage pipeline instead of spawning
"agym usage" as a nested CLI process. One refresh batches all configured
profiles, forces fresh quota data, and emits one synthetic UI-only event.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from agym.cache import CacheManager
from agym.orchestration.contracts import EventSink, EventType, OrchestrationEvent, RunId
from agym.profiles import Profile, ProfileStore
from agym.usage import AccountUsage, extract_quota_bucket, fetch_all_usage


UsageFetcher = Callable[..., Awaitable[list[AccountUsage]]]


class UsagePollingEventSink(EventSink):
    """EventSink decorator that periodically feeds fresh fleet quota to the TUI."""

    def __init__(
        self,
        sink: EventSink,
        *,
        profile_store: ProfileStore,
        agy_path: Path,
        cache_manager: CacheManager | None = None,
        interval_seconds: float = 60.0,
        timeout_seconds: float = 20.0,
        fetcher: UsageFetcher = fetch_all_usage,
    ) -> None:
        self._sink = sink
        self._profile_store = profile_store
        self._agy_path = Path(agy_path)
        self._cache_manager = cache_manager
        self._interval_seconds = max(5.0, float(interval_seconds))
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._fetcher = fetcher
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_id: RunId | None = None
        self._lock = threading.Lock()

    def __getattr__(self, name: str):
        return getattr(self._sink, name)

    def get_events(self, run_id: RunId | str):
        getter = getattr(self._sink, "get_events", None)
        return getter(run_id) if callable(getter) else []

    def emit(self, event: OrchestrationEvent) -> None:
        self._sink.emit(event)

        if event.type in {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_INTERRUPTED,
        }:
            self.close()
        else:
            # RUN_CREATED starts normal runs; the fallback also covers resume,
            # where the first observed event may be ROUND_STARTED/ACTION_REQUESTED.
            self._start(event.run_id)

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.25)
        close = getattr(self._sink, "close", None)
        if callable(close):
            close()

    def _start(self, run_id: RunId) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._run_id = run_id
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="agym-orchestration-usage",
                daemon=True,
            )
            self._thread.start()

    def _poll_loop(self) -> None:
        # Refresh immediately, then periodically for the rest of the run.
        while not self._stop.is_set():
            self._refresh_once()
            if self._stop.wait(self._interval_seconds):
                return

    def _refresh_once(self) -> None:
        run_id = self._run_id
        if run_id is None:
            return

        try:
            profiles = list(self._profile_store.list())
        except Exception as exc:
            self._emit_snapshot(run_id, [], error=f"profile list failed: {exc}")
            return

        if not profiles:
            self._emit_snapshot(run_id, [], error="no profiles configured")
            return

        self._sink.emit(
            OrchestrationEvent(
                event_id=f"usage-refresh-{datetime.now(timezone.utc).timestamp()}",
                run_id=run_id,
                type=EventType.USAGE_UPDATED,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={"fetching": True, "forced": True},
            )
        )

        try:
            usages = asyncio.run(
                self._fetcher(
                    self._agy_path,
                    profiles,
                    force_refresh=True,
                    cache_manager=self._cache_manager,
                    timeout=self._timeout_seconds,
                )
            )
        except Exception as exc:
            self._emit_snapshot(run_id, profiles, error=f"usage refresh failed: {exc}")
            return

        if self._stop.is_set():
            return

        payload_profiles = []
        for usage in usages:
            five_hour = extract_quota_bucket(usage, "gemini", "5h")
            week = extract_quota_bucket(usage, "gemini", "week")
            payload_profiles.append(
                {
                    "profile_name": usage.account,
                    "status": usage.status,
                    "five_hour_remaining": five_hour.percentage if five_hour else None,
                    "week_remaining": week.percentage if week else None,
                    "five_hour_reset": five_hour.reset_time_raw if five_hour else None,
                    "week_reset": week.reset_time_raw if week else None,
                    "error": usage.error,
                }
            )

        if self._stop.is_set():
            return

        self._sink.emit(
            OrchestrationEvent(
                event_id=f"usage-{datetime.now(timezone.utc).timestamp()}",
                run_id=run_id,
                type=EventType.USAGE_UPDATED,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={
                    "profiles": payload_profiles,
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                    "forced": True,
                },
            )
        )

    def _emit_snapshot(
        self,
        run_id: RunId,
        profiles: Sequence[Profile],
        *,
        error: str,
    ) -> None:
        if self._stop.is_set():
            return
        self._sink.emit(
            OrchestrationEvent(
                event_id=f"usage-{datetime.now(timezone.utc).timestamp()}",
                run_id=run_id,
                type=EventType.USAGE_UPDATED,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={
                    "profiles": [
                        {
                            "profile_name": profile.name,
                            "status": "error",
                            "five_hour_remaining": None,
                            "week_remaining": None,
                            "error": error,
                        }
                        for profile in profiles
                    ],
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                    "error": error,
                    "forced": True,
                },
            )
        )
