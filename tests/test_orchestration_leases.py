"""Tests for cross-process profile leasing and lock primitives."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from agym.orchestration.contracts import (
    LeaseId,
    ProfileLease,
    ProfileLeaseManager as ProfileLeaseManagerProtocol,
    RunId,
    WorkerId,
)
from agym.orchestration.leases import (
    ProfileAlreadyLeasedError,
    ProfileLeaseManager,
    is_lease_stale,
    is_pid_alive,
)
from agym.orchestration.locking import FileLock, LockTimeoutError, file_lock


def _race_worker(lease_root: str, profile: str, run_id: str, worker_id: str, queue: Any) -> None:
    mgr = ProfileLeaseManager(lease_root=Path(lease_root))
    try:
        lease = mgr.acquire(profile, run_id=RunId(run_id), worker_id=WorkerId(worker_id))
        queue.put(("success", run_id, str(lease.lease_id)))
    except ProfileAlreadyLeasedError:
        queue.put(("failed", run_id, "already_leased"))
    except Exception as exc:
        queue.put(("error", run_id, str(exc)))


class TestFileLock(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.lock_path = self.temp_dir / "test.lock"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_basic_acquire_and_release(self) -> None:
        lock = FileLock(self.lock_path, timeout=1.0)
        self.assertTrue(lock.acquire())
        self.assertTrue(self.lock_path.exists())
        lock.release()

    def test_context_manager(self) -> None:
        with file_lock(self.lock_path, timeout=1.0) as lock:
            self.assertTrue(self.lock_path.exists())

    def test_reentrancy_on_same_instance(self) -> None:
        lock = FileLock(self.lock_path, timeout=1.0)
        with lock:
            with lock:
                self.assertTrue(self.lock_path.exists())

    def test_timeout_when_held(self) -> None:
        lock1 = FileLock(self.lock_path, timeout=1.0)
        lock2 = FileLock(self.lock_path, timeout=0.05)

        with lock1:
            with self.assertRaises(LockTimeoutError):
                lock2.acquire()

        # Once lock1 released, lock2 can acquire
        self.assertTrue(lock2.acquire())
        lock2.release()


class TestProfileLeaseManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.lease_root = self.temp_dir / "leases"
        self.mgr = ProfileLeaseManager(lease_root=self.lease_root, stale_timeout_seconds=60.0)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self.mgr, ProfileLeaseManagerProtocol)

    def test_acquire_creates_lease_file_with_correct_fields(self) -> None:
        lease = self.mgr.acquire("AI1", run_id="run-1", worker_id="worker-1", pid=os.getpid())
        self.assertEqual(lease.profile_name, "AI1")
        self.assertEqual(lease.run_id, "run-1")
        self.assertEqual(lease.worker_id, "worker-1")
        self.assertEqual(lease.pid, os.getpid())
        self.assertTrue(lease.acquired_at)
        self.assertTrue(lease.heartbeat_at)

        # Verify on-disk file
        file_path = self.lease_root / "AI1.json"
        self.assertTrue(file_path.exists())

        loaded = self.mgr.get_lease("AI1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.lease_id, lease.lease_id)
        self.assertEqual(loaded.profile_name, "AI1")
        self.assertTrue(self.mgr.is_leased("AI1"))

    def test_double_acquire_rejected(self) -> None:
        self.mgr.acquire("AI1", run_id="run-1", worker_id="worker-1")

        with self.assertRaises(ProfileAlreadyLeasedError) as ctx:
            self.mgr.acquire("AI1", run_id="run-2", worker_id="worker-2")

        self.assertIn("already leased by run 'run-1'", str(ctx.exception))

    def test_release_removes_lease(self) -> None:
        lease = self.mgr.acquire("AI1", run_id="run-1", worker_id="worker-1")
        self.assertTrue(self.mgr.is_leased("AI1"))

        released = self.mgr.release(lease.lease_id, run_id="run-1")
        self.assertTrue(released)
        self.assertFalse(self.mgr.is_leased("AI1"))
        self.assertIsNone(self.mgr.get_lease("AI1"))

    def test_release_object_directly(self) -> None:
        lease = self.mgr.acquire("AI1", run_id="run-1", worker_id="worker-1")
        released = self.mgr.release(lease)
        self.assertTrue(released)
        self.assertFalse(self.mgr.is_leased("AI1"))

    def test_release_profile_name_direct(self) -> None:
        lease = self.mgr.acquire("AI1", run_id="run-1", worker_id="worker-1")
        released = self.mgr.release_profile("AI1", lease_id=lease.lease_id, run_id="run-1")
        self.assertTrue(released)
        self.assertFalse(self.mgr.is_leased("AI1"))

    def test_wrong_owner_release_rejected(self) -> None:
        lease = self.mgr.acquire("AI1", run_id="run-1", worker_id="worker-1")

        # Wrong run_id
        rejected = self.mgr.release(lease.lease_id, run_id="run-wrong")
        self.assertFalse(rejected)
        self.assertTrue(self.mgr.is_leased("AI1"))

        # Wrong lease_id
        rejected_id = self.mgr.release(LeaseId("lease_nonexistent"), run_id="run-1")
        self.assertFalse(rejected_id)
        self.assertTrue(self.mgr.is_leased("AI1"))

        # release_profile with wrong lease_id
        rejected_prof = self.mgr.release_profile("AI1", lease_id=LeaseId("lease_other"))
        self.assertFalse(rejected_prof)
        self.assertTrue(self.mgr.is_leased("AI1"))

        # release_profile with wrong run_id
        rejected_prof_run = self.mgr.release_profile("AI1", lease_id=lease.lease_id, run_id="run-other")
        self.assertFalse(rejected_prof_run)
        self.assertTrue(self.mgr.is_leased("AI1"))

    def test_heartbeat_updates_timestamp(self) -> None:
        lease = self.mgr.acquire("AI1", run_id="run-1", worker_id="worker-1")
        orig_heartbeat = lease.heartbeat_at

        time.sleep(0.01)
        hb_success = self.mgr.heartbeat(lease.lease_id)
        self.assertTrue(hb_success)

        updated = self.mgr.get_lease("AI1")
        assert updated is not None
        self.assertGreater(updated.heartbeat_at, orig_heartbeat)

        # Heartbeat on nonexistent lease returns False
        self.assertFalse(self.mgr.heartbeat(LeaseId("lease_bogus")))

    def test_stale_cleanup_dead_pid_and_expired(self) -> None:
        mgr = ProfileLeaseManager(lease_root=self.lease_root, stale_timeout_seconds=0.05)
        # Dead PID: 99999999 (not alive)
        dead_pid = 99999999
        self.assertFalse(is_pid_alive(dead_pid))

        mgr.acquire("AI_DEAD", run_id="run-dead", worker_id="w-dead", pid=dead_pid)
        time.sleep(0.1)

        revoked = mgr.revoke_stale()
        self.assertEqual(len(revoked), 1)
        self.assertEqual(revoked[0].profile_name, "AI_DEAD")
        self.assertFalse(mgr.is_leased("AI_DEAD"))

    def test_non_stale_lease_preserved_alive_pid(self) -> None:
        """Conservative invariant: alive PID is preserved even if execution takes long time."""
        mgr = ProfileLeaseManager(lease_root=self.lease_root, stale_timeout_seconds=0.02)
        alive_pid = os.getpid()
        self.assertTrue(is_pid_alive(alive_pid))

        mgr.acquire("AI_ALIVE", run_id="run-alive", worker_id="w-alive", pid=alive_pid)
        time.sleep(0.05)

        # Even though 0.05s > 0.02s timeout, the process PID is still alive
        revoked = mgr.revoke_stale()
        self.assertEqual(len(revoked), 0)
        self.assertTrue(mgr.is_leased("AI_ALIVE"))

    def test_non_stale_lease_preserved_within_timeout(self) -> None:
        """Lease with dead PID but within timeout threshold is not yet stale."""
        mgr = ProfileLeaseManager(lease_root=self.lease_root, stale_timeout_seconds=100.0)
        mgr.acquire("AI_RECENT", run_id="run-1", worker_id="w-1", pid=99999999)

        revoked = mgr.revoke_stale()
        self.assertEqual(len(revoked), 0)
        self.assertTrue(mgr.is_leased("AI_RECENT"))

    def test_acquire_evicts_stale_lease_automatically(self) -> None:
        mgr = ProfileLeaseManager(lease_root=self.lease_root, stale_timeout_seconds=0.05)
        mgr.acquire("AI_AUTO", run_id="run-old", worker_id="w-old", pid=99999999)
        time.sleep(0.1)

        # Double acquire should succeed because old lease is stale
        new_lease = mgr.acquire("AI_AUTO", run_id="run-new", worker_id="w-new", pid=os.getpid())
        self.assertEqual(new_lease.run_id, "run-new")
        self.assertEqual(new_lease.profile_name, "AI_AUTO")

    def test_list_leases(self) -> None:
        self.mgr.acquire("AI1", run_id="run-1", worker_id="w-1")
        self.mgr.acquire("AI2", run_id="run-1", worker_id="w-2")

        leases = self.mgr.list_leases()
        self.assertEqual(len(leases), 2)
        names = [l.profile_name for l in leases]
        self.assertEqual(names, ["AI1", "AI2"])

    def test_two_process_race(self) -> None:
        """Simultaneous processes must never both successfully lease the same profile."""
        queue: multiprocessing.Queue[Any] = multiprocessing.Queue()
        p1 = multiprocessing.Process(
            target=_race_worker,
            args=(str(self.lease_root), "AI_SHARED", "run-A", "worker-A", queue),
        )
        p2 = multiprocessing.Process(
            target=_race_worker,
            args=(str(self.lease_root), "AI_SHARED", "run-B", "worker-B", queue),
        )

        p1.start()
        p2.start()

        p1.join(timeout=5.0)
        p2.join(timeout=5.0)

        res1 = queue.get(timeout=2.0)
        res2 = queue.get(timeout=2.0)

        statuses = {res1[0], res2[0]}
        self.assertEqual(
            statuses,
            {"success", "failed"},
            f"Expected exactly one success and one failure, got {res1} and {res2}",
        )


if __name__ == "__main__":
    unittest.main()
