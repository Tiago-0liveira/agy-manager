"""Fleet capacity scheduling and profile allocation for AGYM orchestration.

Provides:
- FleetSnapshot generation querying ProfileStore, CacheManager, tokens, and active leases
- Capability-based profile ranking honoring quotas, health, strategy, and minimum reserve
- Redacted FleetView generation hiding all profile identities
- Transactional multi-profile allocation for concurrent workers
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Sequence

from agym.cache import CacheManager
from agym.profiles import Profile, ProfileStore
from agym.orchestration.contracts import (
    AuditRequest,
    ExecutionStrategy,
    FleetSnapshot,
    FleetView,
    ProfileCapacity,
    ProfileLease,
    RunId,
    WorkerId,
    WorkerRequest,
)
from agym.orchestration.leases import LeaseError, ProfileLeaseManager

logger = logging.getLogger(__name__)

# Strategy quota threshold requirements
STRATEGY_MIN_QUOTA: dict[ExecutionStrategy, float] = {
    ExecutionStrategy.STANDARD: 0.10,
    ExecutionStrategy.HIGH_EFFORT: 0.40,
    ExecutionStrategy.BOOST: 0.70,
}

# Quota band thresholds
BAND_HIGH_THRESHOLD = 0.70
BAND_MEDIUM_THRESHOLD = 0.30


class SchedulerError(RuntimeError):
    """Base exception for scheduler errors."""


class InsufficientCapacityError(SchedulerError):
    """Raised when the fleet cannot satisfy requested profile allocations."""


def extract_quota_from_parsed_data(parsed_data: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extracts 5h and weekly remaining fractions from cached usage parsed data."""
    five_hour: float | None = None
    weekly: float | None = None

    if not isinstance(parsed_data, dict):
        return None, None

    # Direct top-level shortcuts (useful for mocks or flat representations)
    if "five_hour_remaining" in parsed_data:
        try:
            five_hour = float(parsed_data["five_hour_remaining"])
        except (ValueError, TypeError):
            pass
    if "weekly_remaining" in parsed_data:
        try:
            weekly = float(parsed_data["weekly_remaining"])
        except (ValueError, TypeError):
            pass

    groups = parsed_data.get("groups", [])
    if isinstance(groups, list):
        # 1. Match within gemini group first
        for group in groups:
            if not isinstance(group, dict):
                continue
            g_name = str(group.get("name", "")).lower()
            if "gemini" not in g_name:
                continue
            for bucket in group.get("buckets", []):
                if not isinstance(bucket, dict):
                    continue
                window = str(bucket.get("window", "")).lower()
                b_id = str(bucket.get("id", "")).lower()
                rem = bucket.get("remaining_fraction")
                if rem is not None:
                    try:
                        frac = float(rem)
                    except (ValueError, TypeError):
                        continue
                    if five_hour is None and ("5h" in window or "5h" in b_id):
                        five_hour = frac
                    elif weekly is None and ("week" in window or "week" in b_id or "7d" in window):
                        weekly = frac

        # 2. Fallback to bucket id matching if gemini group name wasn't present
        if five_hour is None or weekly is None:
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for bucket in group.get("buckets", []):
                    if not isinstance(bucket, dict):
                        continue
                    b_id = str(bucket.get("id", "")).lower()
                    window = str(bucket.get("window", "")).lower()
                    if "gemini" in b_id:
                        rem = bucket.get("remaining_fraction")
                        if rem is not None:
                            try:
                                frac = float(rem)
                            except (ValueError, TypeError):
                                continue
                            if five_hour is None and ("5h" in b_id or "5h" in window):
                                five_hour = frac
                            if weekly is None and ("week" in b_id or "week" in window or "7d" in window):
                                weekly = frac

    return five_hour, weekly


def snapshot_to_fleet_view(
    snapshot: FleetSnapshot,
    reserve_threshold: float = 0.05,
) -> FleetView:
    """Transforms an internal FleetSnapshot into a redacted FleetView.

    FleetView exposes aggregate capability and quota bands only.
    Contains NO profile names, paths, credentials, tokens, or emails.
    """
    available_count = 0
    standard_count = 0
    high_effort_count = 0
    boost_count = 0
    quota_band_counts: dict[str, int] = {}

    for p in snapshot.profiles:
        # A profile is available if unleased and auth ready
        if p.leased or not p.auth_ready:
            continue

        f5 = p.five_hour_remaining if p.five_hour_remaining is not None else 1.0
        fw = p.weekly_remaining if p.weekly_remaining is not None else 1.0
        eff = min(f5, fw)

        # Exclude exhausted profiles below reserve threshold
        if eff < reserve_threshold:
            continue

        available_count += 1

        if eff >= STRATEGY_MIN_QUOTA[ExecutionStrategy.STANDARD]:
            standard_count += 1
        if eff >= STRATEGY_MIN_QUOTA[ExecutionStrategy.HIGH_EFFORT]:
            high_effort_count += 1
        if eff >= STRATEGY_MIN_QUOTA[ExecutionStrategy.BOOST]:
            boost_count += 1

        # Classify quota band
        if eff >= BAND_HIGH_THRESHOLD:
            quota_band_counts["HIGH"] = quota_band_counts.get("HIGH", 0) + 1
        elif eff >= BAND_MEDIUM_THRESHOLD:
            quota_band_counts["MEDIUM"] = quota_band_counts.get("MEDIUM", 0) + 1
        else:
            quota_band_counts["LOW"] = quota_band_counts.get("LOW", 0) + 1

    return FleetView(
        available_profiles=available_count,
        max_parallel=available_count,
        standard_capacity=standard_count,
        high_effort_capacity=high_effort_count,
        boost_capacity=boost_count,
        quota_band_counts=quota_band_counts,
    )


class ProfileScheduler:
    """Schedules and leases AGYM profiles for orchestration requests."""

    def __init__(
        self,
        profile_store: ProfileStore | None = None,
        lease_manager: ProfileLeaseManager | None = None,
        cache_manager: CacheManager | None = None,
        min_reserve: float = 0.05,
        auth_checker: Callable[[Path], bool] | None = None,
    ) -> None:
        self.profile_store = profile_store or ProfileStore()
        self.lease_manager = lease_manager or ProfileLeaseManager()
        self.cache_manager = cache_manager or CacheManager()
        self.min_reserve = max(0.0, min_reserve)
        self._auth_checker = auth_checker
        self._recent_failures: dict[str, int] = {}

    def record_failure(self, profile_name: str) -> None:
        """Records an execution failure against a profile to penalize selection."""
        self._recent_failures[profile_name] = self._recent_failures.get(profile_name, 0) + 1

    def record_success(self, profile_name: str) -> None:
        """Records a successful execution, decrementing failure penalties."""
        current = self._recent_failures.get(profile_name, 0)
        if current > 1:
            self._recent_failures[profile_name] = current - 1
        elif current == 1:
            self._recent_failures.pop(profile_name, None)

    def clear_failures(self, profile_name: str | None = None) -> None:
        """Clears failure counts for a specific profile or all profiles."""
        if profile_name is None:
            self._recent_failures.clear()
        else:
            self._recent_failures.pop(profile_name, None)

    def _check_auth(self, profile: Profile) -> bool:
        if self._auth_checker is not None:
            return self._auth_checker(profile.home)
        try:
            from agym.quota_api import load_profile_token_data
            data = load_profile_token_data(profile.home)
            return bool(data and (data.get("access_token") or data.get("refresh_token")))
        except Exception:
            return False

    def get_fleet_snapshot(self) -> FleetSnapshot:
        """Builds an internal snapshot of the current fleet state.

        Queries:
        - ProfileStore for profile inventory
        - CacheManager for cached quota
        - Token storage for auth readiness
        - ProfileLeaseManager for active leases
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        profiles = self.profile_store.list()
        active_leases = self.lease_manager.list_leases()
        leased_names = {lease.profile_name for lease in active_leases}

        capacities: list[ProfileCapacity] = []
        for p in profiles:
            is_leased = p.name in leased_names
            auth_ready = self._check_auth(p)

            # Quota retrieval from cache only (Rule: Do not create another quota retrieval implementation)
            cached_meta = self.cache_manager.get_cached_usage_with_meta(p.name)
            if cached_meta and cached_meta[0]:
                f5, fw = extract_quota_from_parsed_data(cached_meta[0])
            else:
                f5, fw = None, None

            failures = self._recent_failures.get(p.name, 0)

            capacities.append(
                ProfileCapacity(
                    profile_name=p.name,
                    five_hour_remaining=f5,
                    weekly_remaining=fw,
                    leased=is_leased,
                    auth_ready=auth_ready,
                    recent_failures=failures,
                    observed_at=now_iso,
                )
            )

        return FleetSnapshot(profiles=capacities, observed_at=now_iso)

    def snapshot(self) -> FleetSnapshot:
        """Alias for get_fleet_snapshot for DoD requirement."""
        return self.get_fleet_snapshot()

    def get_fleet_view(self) -> FleetView:
        """Returns coordinator-safe redacted capability summary."""
        snap = self.get_fleet_snapshot()
        return snapshot_to_fleet_view(snap, reserve_threshold=self.min_reserve)

    def _filter_and_rank_candidates(
        self,
        snapshot: FleetSnapshot,
        strategy: ExecutionStrategy = ExecutionStrategy.STANDARD,
        min_quota: float | None = None,
        min_five_hour: float | None = None,
        min_weekly: float | None = None,
        excluded_profiles: Collection[str] | None = None,
    ) -> list[ProfileCapacity]:
        """Filters eligible profiles and ranks them by health and capability.

        Ranking criteria:
        1. Fewest recent failures
        2. Highest effective remaining quota
        3. Highest 5h remaining quota
        4. Highest weekly remaining quota
        5. Stable deterministic tie-breaker by name
        """
        excluded = set(excluded_profiles or [])
        eff_min_quota = max(self.min_reserve, min_quota or 0.0)
        strategy_threshold = STRATEGY_MIN_QUOTA.get(strategy, 0.10)
        required_floor = max(eff_min_quota, strategy_threshold)

        eligible: list[ProfileCapacity] = []

        for p in snapshot.profiles:
            # 1. Leased account excluded
            if p.leased:
                continue

            # 2. Excluded profile set
            if p.profile_name in excluded:
                continue

            # 3. Auth ready required
            if not p.auth_ready:
                continue

            # 4. Low 5h quota excluded
            if min_five_hour is not None:
                if p.five_hour_remaining is not None and p.five_hour_remaining < min_five_hour:
                    continue

            # 5. Low weekly quota excluded
            if min_weekly is not None:
                if p.weekly_remaining is not None and p.weekly_remaining < min_weekly:
                    continue

            # 6. Minimum reserve & strategy floor honored
            f5 = p.five_hour_remaining if p.five_hour_remaining is not None else 1.0
            fw = p.weekly_remaining if p.weekly_remaining is not None else 1.0
            eff = min(f5, fw)

            if eff < required_floor:
                continue

            eligible.append(p)

        def sort_key(p: ProfileCapacity) -> tuple[int, float, float, float, str]:
            f5 = p.five_hour_remaining if p.five_hour_remaining is not None else 1.0
            fw = p.weekly_remaining if p.weekly_remaining is not None else 1.0
            eff = min(f5, fw)
            # Higher is better for quota; lower is better for failures
            # Using negative failures so 0 failures (-0) is greater than 1 failure (-1)
            return (-p.recent_failures, eff, f5, fw, p.profile_name)

        eligible.sort(key=sort_key, reverse=True)
        return eligible

    def select_profile(
        self,
        request: WorkerRequest | AuditRequest,
        excluded_profiles: Collection[str] | None = None,
    ) -> str | None:
        """Selects an optimal profile name for the given request, or None if unavailable."""
        snap = self.get_fleet_snapshot()
        strategy = getattr(request, "strategy", ExecutionStrategy.STANDARD)
        candidates = self._filter_and_rank_candidates(
            snap,
            strategy=strategy,
            excluded_profiles=excluded_profiles,
        )
        return candidates[0].profile_name if candidates else None

    def allocate(
        self,
        requests_or_count: int | Sequence[WorkerRequest | AuditRequest] = 1,
        *,
        strategy: ExecutionStrategy = ExecutionStrategy.STANDARD,
        run_id: RunId | str | None = None,
        worker_ids: Sequence[WorkerId | str] | None = None,
        min_quota: float | None = None,
        min_five_hour: float | None = None,
        min_weekly: float | None = None,
        excluded_profiles: Collection[str] | None = None,
        raise_on_insufficient: bool = True,
    ) -> list[ProfileLease]:
        """Atomically and transactionally allocates distinct profile leases for workers.

        Guarantees:
        - If multiple workers are requested, each receives a DISTINCT profile.
        - Never leases one account twice within the same wave.
        - Transactional: if capacity is insufficient, all acquired leases in this batch
          are rolled back before raising InsufficientCapacityError or returning [].
        """
        # Normalize requests
        if isinstance(requests_or_count, int):
            count = requests_or_count
            if count <= 0:
                return []
            w_ids = list(worker_ids or [])
            req_list: list[WorkerRequest] = []
            for i in range(count):
                w_id = WorkerId(w_ids[i] if i < len(w_ids) else f"worker_{uuid.uuid4().hex[:6]}")
                req_list.append(WorkerRequest(worker_id=w_id, role="EXECUTOR", strategy=strategy))
        else:
            req_list = list(requests_or_count)
            count = len(req_list)
            if count <= 0:
                return []

        actual_run_id = RunId(run_id or f"run_{uuid.uuid4().hex[:8]}")
        excluded: set[str] = set(excluded_profiles or [])
        acquired_leases: list[ProfileLease] = []

        for req in req_list:
            req_strategy = getattr(req, "strategy", strategy)
            snap = self.get_fleet_snapshot()

            candidates = self._filter_and_rank_candidates(
                snap,
                strategy=req_strategy,
                min_quota=min_quota,
                min_five_hour=min_five_hour,
                min_weekly=min_weekly,
                excluded_profiles=excluded,
            )

            acquired_lease: ProfileLease | None = None

            for candidate in candidates:
                try:
                    lease = self.lease_manager.acquire(
                        profile_name=candidate.profile_name,
                        run_id=actual_run_id,
                        worker_id=req.worker_id,
                    )
                    acquired_lease = lease
                    acquired_leases.append(lease)
                    excluded.add(candidate.profile_name)
                    break
                except LeaseError:
                    # Profile was claimed concurrently by another process, try next candidate
                    continue

            if acquired_lease is None:
                # Insufficient capacity! Rollback all acquired leases in this wave
                logger.warning(
                    "Insufficient capacity allocating request %s (needed %d, got %d). Rolling back.",
                    req.worker_id, count, len(acquired_leases)
                )
                for lease in acquired_leases:
                    self.lease_manager.release(lease.lease_id, run_id=actual_run_id)

                if raise_on_insufficient:
                    raise InsufficientCapacityError(
                        f"Insufficient capacity: requested {count} profiles, "
                        f"but only {len(acquired_leases)} could be leased"
                    )
                return []

        return acquired_leases
