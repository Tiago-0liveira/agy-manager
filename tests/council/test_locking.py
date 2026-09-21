"""Tests for ProfileStore cross-process file locking, thread safety, and re-entrancy.

Specifications for Milestone 1 / Slice 1:
- Feature 3: Cross-Process Profile Locking & In-Process Re-entrant Lock
- Multi-process stress testing with multiprocessing
- Single-process thread contention
- Exception safety & guaranteed lock release
- Race condition and data loss prevention in config.json
- Re-entrancy within the same thread
- Profile collision immunity (profile 'council' locking parity)
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agym.profiles import (
    InvalidProfileName,
    ProfileError,
    ProfileExists,
    ProfileNotFound,
    ProfileSettings,
    ProfileStore,
)

# ----------------------------------------------------------------------
# Top-level helper functions for multiprocessing
# Must be at module level for spawn/forkserver pickle compatibility
# ----------------------------------------------------------------------

def _mp_worker_create_profiles(
    config_root_str: str,
    data_root_str: str,
    worker_id: int,
    count: int,
    err_queue: mp.Queue,
) -> None:
    """Worker process that creates 'count' uniquely named profiles."""
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        for i in range(count):
            name = f"proc_{worker_id}_{i}"
            store.create(
                name,
                settings=ProfileSettings(model=f"model_{worker_id}"),
                subscription_date="2027-01-01",
            )
    except Exception as exc:
        import traceback
        err_queue.put((worker_id, "create", str(exc), traceback.format_exc()))


def _mp_worker_update_profiles(
    config_root_str: str,
    data_root_str: str,
    worker_id: int,
    profile_names: list[str],
    iterations: int,
    err_queue: mp.Queue,
) -> None:
    """Worker process that repeatedly updates settings on existing profiles."""
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        for it in range(iterations):
            for name in profile_names:
                store.update_settings(
                    name,
                    ProfileSettings(
                        model=f"model_{worker_id}_{it}",
                        dangerously_skip_permissions=bool(it % 2),
                    ),
                )
    except Exception as exc:
        import traceback
        err_queue.put((worker_id, "update", str(exc), traceback.format_exc()))


def _mp_worker_mixed_workload(
    config_root_str: str,
    data_root_str: str,
    worker_id: int,
    iterations: int,
    err_queue: mp.Queue,
) -> None:
    """Worker process performing mixed create, update, and remove operations."""
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        for it in range(iterations):
            temp_name = f"temp_{worker_id}_{it}"
            store.create(temp_name)
            store.update_settings(
                temp_name,
                ProfileSettings(model="transient", dangerously_skip_permissions=True),
            )
            store.set_subscription_date(temp_name, "2028-06-15")
            store.remove(temp_name)
    except Exception as exc:
        import traceback
        err_queue.put((worker_id, "mixed", str(exc), traceback.format_exc()))


# ----------------------------------------------------------------------
# Test Classes
# ----------------------------------------------------------------------

class TestProfileStoreThreadSafety(unittest.TestCase):
    """Verifies that ProfileStore is fully thread-safe under concurrent in-process access."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self.store = ProfileStore(self.config_root, self.data_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_threads_create_unique_profiles(self) -> None:
        """Multiple threads creating distinct profiles concurrently without data loss."""
        num_threads = 8
        profiles_per_thread = 10
        errors: list[tuple[int, Exception]] = []

        def worker(thread_idx: int) -> None:
            try:
                for i in range(profiles_per_thread):
                    name = f"thread_{thread_idx}_{i}"
                    self.store.create(name)
            except Exception as exc:
                errors.append((thread_idx, exc))

        threads = [
            threading.Thread(target=worker, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], f"Thread errors encountered: {errors}")
        all_profiles = self.store.list()
        expected_count = num_threads * profiles_per_thread
        self.assertEqual(
            len(all_profiles),
            expected_count,
            f"Expected {expected_count} profiles, but found {len(all_profiles)}. Lost updates detected!",
        )

        # Verify config.json is intact and readable
        with self.store.config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["profiles"]), expected_count)

    def test_concurrent_threads_update_settings(self) -> None:
        """Multiple threads updating settings concurrently without corrupting config.json."""
        # Pre-create profiles
        for i in range(5):
            self.store.create(f"target_{i}")

        num_threads = 6
        updates_per_thread = 15
        errors: list[tuple[int, Exception]] = []

        def worker(thread_idx: int) -> None:
            try:
                for i in range(updates_per_thread):
                    target = f"target_{i % 5}"
                    self.store.update_settings(
                        target,
                        ProfileSettings(
                            model=f"model_{thread_idx}_{i}",
                            dangerously_skip_permissions=bool(i % 2),
                        ),
                    )
            except Exception as exc:
                errors.append((thread_idx, exc))

        threads = [
            threading.Thread(target=worker, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], f"Thread update errors: {errors}")
        # Verify valid config.json and each profile is readable
        for i in range(5):
            p = self.store.get(f"target_{i}")
            self.assertIsNotNone(p.settings.model)

    def test_concurrent_threads_mixed_crud(self) -> None:
        """Concurrent creates, updates, subscription dates, and removes across threads."""
        self.store.create("shared_profile")
        num_threads = 6
        errors: list[tuple[int, Exception]] = []

        def worker(thread_idx: int) -> None:
            try:
                for i in range(10):
                    # update shared
                    self.store.update_settings(
                        "shared_profile",
                        ProfileSettings(model=f"m_{thread_idx}_{i}"),
                    )
                    # create private
                    priv_name = f"priv_{thread_idx}_{i}"
                    self.store.create(priv_name)
                    self.store.set_subscription_date(priv_name, "2027-05-01")
                    # remove private
                    self.store.remove(priv_name)
            except Exception as exc:
                errors.append((thread_idx, exc))

        threads = [
            threading.Thread(target=worker, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        self.assertEqual(errors, [], f"Mixed CRUD thread errors: {errors}")
        # Shared profile must still exist and config.json must be valid
        shared = self.store.get("shared_profile")
        self.assertIsNotNone(shared)


class TestProfileStoreMultiprocessing(unittest.TestCase):
    """Verifies cross-process locking and concurrency with multiprocessing."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self.store = ProfileStore(self.config_root, self.data_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_multiprocess_concurrent_creates(self) -> None:
        """Separate OS processes creating profiles concurrently must not lose any profiles."""
        num_processes = 4
        profiles_per_process = 15
        ctx = mp.get_context()
        err_queue: mp.Queue = ctx.Queue()

        processes: list[mp.Process] = []
        for worker_id in range(num_processes):
            p = ctx.Process(
                target=_mp_worker_create_profiles,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    worker_id,
                    profiles_per_process,
                    err_queue,
                ),
            )
            processes.append(p)

        for p in processes:
            p.start()

        for p in processes:
            p.join(timeout=15.0)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2.0)
                self.fail(f"Worker process {p.pid} hung; cross-process deadlock detected!")

        # Check errors from queue
        errors = []
        while not err_queue.empty():
            errors.append(err_queue.get_nowait())
        self.assertEqual(errors, [], f"Process errors: {errors}")

        # Assert no lost updates
        all_profiles = self.store.list()
        expected_total = num_processes * profiles_per_process
        self.assertEqual(
            len(all_profiles),
            expected_total,
            f"Expected {expected_total} profiles in store, found {len(all_profiles)}. Lost updates across processes!",
        )

    def test_multiprocess_concurrent_updates(self) -> None:
        """Separate OS processes repeatedly updating existing profiles without corruption."""
        profile_names = [f"mp_prof_{i}" for i in range(5)]
        for name in profile_names:
            self.store.create(name)

        num_processes = 4
        iterations = 10
        ctx = mp.get_context()
        err_queue: mp.Queue = ctx.Queue()

        processes = [
            ctx.Process(
                target=_mp_worker_update_profiles,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    w_id,
                    profile_names,
                    iterations,
                    err_queue,
                ),
            )
            for w_id in range(num_processes)
        ]

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=15.0)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2.0)
                self.fail("Update worker process hung!")

        errors = []
        while not err_queue.empty():
            errors.append(err_queue.get_nowait())
        self.assertEqual(errors, [], f"Process update errors: {errors}")

        # All 5 profiles must be intact and readable
        for name in profile_names:
            prof = self.store.get(name)
            self.assertIsNotNone(prof.settings.model)

    def test_multiprocess_mixed_workload(self) -> None:
        """Separate OS processes performing rapid create-update-remove cycles."""
        num_processes = 4
        iterations = 8
        ctx = mp.get_context()
        err_queue: mp.Queue = ctx.Queue()

        processes = [
            ctx.Process(
                target=_mp_worker_mixed_workload,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    w_id,
                    iterations,
                    err_queue,
                ),
            )
            for w_id in range(num_processes)
        ]

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=15.0)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2.0)
                self.fail("Mixed workload worker process hung!")

        errors = []
        while not err_queue.empty():
            errors.append(err_queue.get_nowait())
        self.assertEqual(errors, [], f"Process mixed workload errors: {errors}")

        # config.json must remain valid JSON
        self.assertTrue(self.store.config_path.exists())
        with self.store.config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("profiles", data)


class TestProfileStoreLockReentrancy(unittest.TestCase):
    """Verifies lock re-entrancy within the same thread to prevent self-deadlocks."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(self.root / "config", self.root / "data")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_nested_lock_acquisition_same_thread(self) -> None:
        """Calling lock context manager in nested blocks must succeed without deadlock."""
        lock_fn = getattr(self.store, "_profile_lock", None)
        if lock_fn is None:
            self.skipTest("_profile_lock helper not yet exposed on ProfileStore")

        with lock_fn():
            with lock_fn():
                with lock_fn():
                    p = self.store.create("nested_profile")
                    self.assertEqual(p.name, "nested_profile")

    def test_mutating_method_invoking_locked_subcalls(self) -> None:
        """Methods that internally call other locked methods (e.g. update_settings -> get) do not deadlock."""
        self.store.create("test_prof")
        # update_settings calls self.get() internally
        updated = self.store.update_settings(
            "test_prof",
            ProfileSettings(model="gemini-flash", dangerously_skip_permissions=True),
        )
        self.assertEqual(updated.settings.model, "gemini-flash")

        # set_subscription_date calls self.get() internally
        sub = self.store.set_subscription_date("test_prof", "2027-10-10")
        self.assertEqual(sub.subscription_date, "2027-10-10")

        # remove calls self.get() internally
        self.store.remove("test_prof")
        with self.assertRaises(ProfileNotFound):
            self.store.get("test_prof")


class TestProfileStoreLockExceptionSafety(unittest.TestCase):
    """Verifies that locks are unconditionally released when exceptions occur."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(self.root / "config", self.root / "data")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_exception_in_explicit_lock_block_releases_lock(self) -> None:
        """Lock must be released even if code inside the with block raises an exception."""
        lock_fn = getattr(self.store, "_profile_lock", None)
        if lock_fn is None:
            self.skipTest("_profile_lock helper not yet exposed on ProfileStore")

        with self.assertRaises(RuntimeError):
            with lock_fn():
                raise RuntimeError("simulated error")

        # Immediately acquire lock again; if not released, this hangs or fails
        with lock_fn():
            self.store.create("p_after_exception")
        self.assertEqual(self.store.get("p_after_exception").name, "p_after_exception")

    def test_validation_error_releases_lock(self) -> None:
        """InvalidProfileName raised during create must release lock immediately."""
        with self.assertRaises(InvalidProfileName):
            self.store.create("invalid/name")

        # Next valid creation must succeed
        p = self.store.create("valid_name")
        self.assertEqual(p.name, "valid_name")

    def test_profile_exists_error_releases_lock(self) -> None:
        """ProfileExists error must release lock immediately."""
        self.store.create("dup_profile")
        with self.assertRaises(ProfileExists):
            self.store.create("dup_profile")

        # Subsequent mutation must succeed
        self.store.create("another_profile")
        self.assertEqual(len(self.store.list()), 2)

    def test_simulated_io_error_during_save_releases_lock(self) -> None:
        """If _save raises an OSError, lock must still be released."""
        with mock.patch.object(self.store, "_save", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.store.create("doomed_profile")

        # Once IO error is cleared, next call succeeds
        p = self.store.create("recovered_profile")
        self.assertEqual(p.name, "recovered_profile")


class TestLockFilePropertiesAndParity(unittest.TestCase):
    """Verifies lockfile location, POSIX permissions, and parity for profile 'council'."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(self.root / "config", self.root / "data")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lockfile_created_in_config_root(self) -> None:
        """Lockfile config.lock must reside directly inside config_root alongside config.json."""
        self.store.create("p1")
        expected_lock = self.store.config_root / "config.lock"
        self.assertTrue(
            expected_lock.exists(),
            f"Expected lockfile at {expected_lock} to exist after mutating operation",
        )

    @unittest.skipIf(os.name == "nt", "POSIX permission check")
    def test_lockfile_private_permissions_posix(self) -> None:
        """Lockfile must have private permissions (no world or group read/write)."""
        self.store.create("p1")
        lock_path = self.store.config_root / "config.lock"
        if lock_path.exists():
            mode = lock_path.stat().st_mode & 0o777
            self.assertEqual(
                mode & 0o077,
                0,
                f"Expected private permissions for lockfile, got: {oct(mode)}",
            )

    def test_council_profile_locking_parity(self) -> None:
        """Profile literally named 'council' participates fully in locking and mutations."""
        p = self.store.create("council")
        self.assertEqual(p.name, "council")

        updated = self.store.update_settings(
            "council",
            ProfileSettings(model="gemini-2.5-pro", dangerously_skip_permissions=True),
        )
        self.assertEqual(updated.settings.model, "gemini-2.5-pro")

        sub = self.store.set_subscription_date("council", "2027-12-31")
        self.assertEqual(sub.subscription_date, "2027-12-31")

        self.store.remove("council")
        with self.assertRaises(ProfileNotFound):
            self.store.get("council")


if __name__ == "__main__":
    unittest.main()
