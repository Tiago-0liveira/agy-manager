"""Tests for fleet capacity scheduling and profile allocation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from agym.cache import CacheManager
from agym.profiles import ProfileStore
from agym.orchestration.contracts import (
    AuditRequest,
    ExecutionStrategy,
    FleetSnapshot,
    FleetView,
    ProfileCapacity,
    ProfileScheduler as ProfileSchedulerProtocol,
    RunId,
    WorkerId,
    WorkerRequest,
    WorkerRole,
)
from agym.orchestration.leases import ProfileAlreadyLeasedError, ProfileLeaseManager
from agym.orchestration.scheduler import (
    InsufficientCapacityError,
    ProfileScheduler,
    extract_quota_from_parsed_data,
    snapshot_to_fleet_view,
)


class TestSchedulerHelpers(unittest.TestCase):
    def test_extract_quota_from_parsed_data_hierarchical(self) -> None:
        data = {
            "groups": [
                {
                    "name": "Gemini 2.5 Flash",
                    "buckets": [
                        {"id": "gemini_5h", "window": "5h", "remaining_fraction": 0.85},
                        {"id": "gemini_week", "window": "week", "remaining_fraction": 0.70},
                    ],
                }
            ]
        }
        f5, fw = extract_quota_from_parsed_data(data)
        self.assertEqual(f5, 0.85)
        self.assertEqual(fw, 0.70)

    def test_extract_quota_fallback_flat(self) -> None:
        data = {"five_hour_remaining": 0.65, "weekly_remaining": 0.50}
        f5, fw = extract_quota_from_parsed_data(data)
        self.assertEqual(f5, 0.65)
        self.assertEqual(fw, 0.50)


class TestProfileScheduler(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.cfg_root = self.temp_dir / "cfg"
        self.data_root = self.temp_dir / "data"

        self.ps = ProfileStore(config_root=self.cfg_root, data_root=self.data_root)
        self.cm = CacheManager(cache_root=self.data_root / "cache")
        self.lm = ProfileLeaseManager(lease_root=self.data_root / "leases")

        # Create baseline fleet of 4 profiles
        for name in ["AI_Alpha", "AI_Beta", "AI_Gamma", "AI_Delta"]:
            self.ps.create(name)

        # Quotas
        # Alpha: 90% 5h, 90% week (High healthy)
        self._set_profile_quota("AI_Alpha", 0.90, 0.90)
        # Beta: 50% 5h, 50% week (Medium)
        self._set_profile_quota("AI_Beta", 0.50, 0.50)
        # Gamma: 20% 5h, 20% week (Low)
        self._set_profile_quota("AI_Gamma", 0.20, 0.20)
        # Delta: 5% 5h, 5% week (Reserve exhausted)
        self._set_profile_quota("AI_Delta", 0.05, 0.05)

        self.scheduler = ProfileScheduler(
            profile_store=self.ps,
            lease_manager=self.lm,
            cache_manager=self.cm,
            min_reserve=0.10,
            auth_checker=lambda _: True,  # All profiles auth ready by default
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _set_profile_quota(self, name: str, five_hour: float, weekly: float) -> None:
        parsed_data = {
            "groups": [
                {
                    "name": "Gemini 2.5 Flash",
                    "buckets": [
                        {"id": "gemini_5h", "window": "5h", "remaining_fraction": five_hour},
                        {"id": "gemini_week", "window": "week", "remaining_fraction": weekly},
                    ],
                }
            ]
        }
        self.cm.set_usage(name, parsed_data, "")

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self.scheduler, ProfileSchedulerProtocol)

    def test_snapshot_building(self) -> None:
        snap = self.scheduler.snapshot()
        self.assertEqual(len(snap.profiles), 4)

        alpha = next(p for p in snap.profiles if p.profile_name == "AI_Alpha")
        self.assertEqual(alpha.five_hour_remaining, 0.90)
        self.assertEqual(alpha.weekly_remaining, 0.90)
        self.assertFalse(alpha.leased)
        self.assertTrue(alpha.auth_ready)
        self.assertEqual(alpha.recent_failures, 0)
        self.assertTrue(alpha.observed_at)

    def test_fleet_view_hides_names(self) -> None:
        """FleetView must contain capability counts only, never profile identities."""
        fv = self.scheduler.get_fleet_view()
        self.assertIsInstance(fv, FleetView)

        # Delta is below min_reserve=0.10, so available is Alpha, Beta, Gamma (3)
        self.assertEqual(fv.available_profiles, 3)
        self.assertEqual(fv.max_parallel, 3)
        self.assertEqual(fv.standard_capacity, 3)     # 0.90, 0.50, 0.20 are >= 0.10
        self.assertEqual(fv.high_effort_capacity, 2)  # 0.90, 0.50 are >= 0.40
        self.assertEqual(fv.boost_capacity, 1)        # 0.90 is >= 0.70

        fv_dict = fv.to_dict()
        fv_json = fv.to_json()

        # Invariant check: No profile names, filesystem paths, or tokens anywhere
        for name in ["AI_Alpha", "AI_Beta", "AI_Gamma", "AI_Delta"]:
            self.assertNotIn(name, fv_dict)
            self.assertNotIn(name, fv_json)
        self.assertNotIn("path", fv_dict)
        self.assertNotIn("token", fv_dict)
        self.assertNotIn("credential", fv_dict)

    def test_healthy_profile_chosen(self) -> None:
        """Healthy profile with higher quota is chosen over lower quota."""
        req = WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.ARCHITECTURE)
        chosen = self.scheduler.select_profile(req)
        self.assertEqual(chosen, "AI_Alpha")

    def test_leased_account_excluded(self) -> None:
        """Actively leased accounts must be excluded from selection."""
        # Lease AI_Alpha
        self.lm.acquire("AI_Alpha", run_id="run-ext", worker_id="w-ext")

        # Now AI_Beta should be selected instead
        req = WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.TESTING)
        chosen = self.scheduler.select_profile(req)
        self.assertEqual(chosen, "AI_Beta")

    def test_low_5h_quota_excluded(self) -> None:
        """Profiles with 5h quota below threshold must be excluded."""
        # AI_Gamma has 0.20 5h quota. Require 0.30:
        req = WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.EXECUTOR)
        # Exclude Alpha and Beta manually to test Gamma
        chosen = self.scheduler._filter_and_rank_candidates(
            self.scheduler.snapshot(),
            min_five_hour=0.30,
            excluded_profiles={"AI_Alpha", "AI_Beta"},
        )
        self.assertEqual(len(chosen), 0)

    def test_low_weekly_quota_excluded(self) -> None:
        """Profiles with weekly quota below threshold must be excluded."""
        # Gamma has 0.20 weekly quota.
        chosen = self.scheduler._filter_and_rank_candidates(
            self.scheduler.snapshot(),
            min_weekly=0.30,
            excluded_profiles={"AI_Alpha", "AI_Beta"},
        )
        self.assertEqual(len(chosen), 0)

    def test_minimum_reserve_honored(self) -> None:
        """Profiles below min_reserve must never be selected."""
        # AI_Delta has 0.05 quota, below min_reserve=0.10.
        candidates = self.scheduler._filter_and_rank_candidates(
            self.scheduler.snapshot(),
            excluded_profiles={"AI_Alpha", "AI_Beta", "AI_Gamma"},
        )
        self.assertEqual(len(candidates), 0)

    def test_recent_failures_affect_selection(self) -> None:
        """Recent failures penalize profile ranking even if quota is higher."""
        # AI_Alpha (0.90 quota) receives 2 failures
        self.scheduler.record_failure("AI_Alpha")
        self.scheduler.record_failure("AI_Alpha")

        # AI_Beta (0.50 quota) has 0 failures
        req = WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.GENERAL)
        chosen = self.scheduler.select_profile(req)
        self.assertEqual(chosen, "AI_Beta")

        # Clear failures on AI_Alpha -> AI_Alpha is top again
        self.scheduler.clear_failures("AI_Alpha")
        chosen_after_clear = self.scheduler.select_profile(req)
        self.assertEqual(chosen_after_clear, "AI_Alpha")

    def test_strategy_requirements_honored(self) -> None:
        """ExecutionStrategy dictates minimum quota requirements."""
        # BOOST strategy requires >= 0.70. Only AI_Alpha qualifies.
        req_boost = WorkerRequest(
            worker_id=WorkerId("w-boost"),
            role=WorkerRole.EXECUTOR,
            strategy=ExecutionStrategy.BOOST,
        )
        chosen = self.scheduler.select_profile(req_boost)
        self.assertEqual(chosen, "AI_Alpha")

        # If Alpha is excluded, BOOST should return None
        chosen_none = self.scheduler.select_profile(req_boost, excluded_profiles={"AI_Alpha"})
        self.assertIsNone(chosen_none)

    def test_multiple_distinct_allocations(self) -> None:
        """Allocating multiple concurrent workers must return distinct profiles."""
        requests = [
            WorkerRequest(worker_id=WorkerId("w-arch"), role=WorkerRole.ARCHITECTURE),
            WorkerRequest(worker_id=WorkerId("w-test"), role=WorkerRole.TESTING),
            WorkerRequest(worker_id=WorkerId("w-sec"), role=WorkerRole.SECURITY),
        ]
        leases = self.scheduler.allocate(requests, run_id="run-wave1")

        self.assertEqual(len(leases), 3)
        leased_names = [l.profile_name for l in leases]
        self.assertEqual(len(set(leased_names)), 3)
        self.assertEqual(set(leased_names), {"AI_Alpha", "AI_Beta", "AI_Gamma"})

    def test_never_lease_account_twice_in_same_wave(self) -> None:
        """Guarantees distinct allocation within the same wave."""
        leases = self.scheduler.allocate(3, strategy=ExecutionStrategy.STANDARD)
        self.assertEqual(len(leases), 3)
        self.assertEqual(len(set(l.profile_name for l in leases)), 3)

    def test_insufficient_capacity_raises_and_rolls_back(self) -> None:
        """If requests exceed available capacity, raises and rolls back partial leases."""
        # Only 3 profiles are eligible (Alpha, Beta, Gamma). Delta is below reserve.
        # Requesting 4 should fail.
        with self.assertRaises(InsufficientCapacityError):
            self.scheduler.allocate(4, strategy=ExecutionStrategy.STANDARD, run_id="run-fail")

        # Crucial transactional check: 0 leases must remain active after failure!
        active_leases = self.lm.list_leases()
        self.assertEqual(len(active_leases), 0)

    def test_auth_not_ready_excluded(self) -> None:
        """Profiles with auth_ready=False must be excluded."""
        sched_unauthed = ProfileScheduler(
            profile_store=self.ps,
            lease_manager=self.lm,
            cache_manager=self.cm,
            auth_checker=lambda path: "AI_Alpha" not in str(path),
        )
        # AI_Alpha is unauthed, so Beta must be chosen
        chosen = sched_unauthed.select_profile(
            WorkerRequest(worker_id=WorkerId("w-1"), role=WorkerRole.EXECUTOR)
        )
        self.assertEqual(chosen, "AI_Beta")

    def test_role_names_do_not_affect_account_identity(self) -> None:
        """Worker role name alone must not dictate a hardcoded profile identity."""
        roles = [WorkerRole.ARCHITECTURE, WorkerRole.TESTING, WorkerRole.SECURITY, WorkerRole.SYNTHESIZER]
        for role in roles:
            req = WorkerRequest(worker_id=WorkerId("w-1"), role=role)
            # Should consistently pick the top healthy profile AI_Alpha
            self.assertEqual(self.scheduler.select_profile(req), "AI_Alpha")

    def test_definition_of_done_standalone(self) -> None:
        """Definition of done: independently usable via snapshot() and allocate()."""
        snap = self.scheduler.snapshot()
        self.assertIsInstance(snap, FleetSnapshot)

        leases = self.scheduler.allocate(2, strategy=ExecutionStrategy.STANDARD)
        self.assertEqual(len(leases), 2)
        for lease in leases:
            self.assertTrue(self.lm.is_leased(lease.profile_name))


if __name__ == "__main__":
    unittest.main()
