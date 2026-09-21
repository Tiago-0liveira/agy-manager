"""Adversarial stress harness for ProfileStore cross-process locking and concurrency.

This suite performs white-box adversarial stress testing against agym/profiles.py:
1. Process Crash Simulation:
   - SIGKILL (kill -9) during active lock hold; verification that OS kernel releases flock.
   - Consecutive crash barrage (multiple processes killed in succession).
   - Abandoned temporary files left behind during crash.
2. High-Concurrency Multiprocessing Contention:
   - 12+ worker processes simultaneously performing creates, updates, churn (create+delete),
     and concurrent readers under synchronized start barrier.
   - Zero lost updates oracle verification.
3. Race-Condition Probes:
   - Simultaneous create of the same profile name (exactly 1 wins, N-1 get ProfileExists).
   - Simultaneous remove of the same profile name (exactly 1 wins, N-1 get ProfileNotFound).
4. Multi-Thread Contention & Deep Re-entrancy:
   - Deep nested locking (depth up to 8) across concurrent threads.
   - Multi-instance synchronization within the same process.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agym.profiles import (
    ProfileError,
    ProfileExists,
    ProfileNotFound,
    ProfileSettings,
    ProfileStore,
)


# ----------------------------------------------------------------------
# Multiprocessing Worker Routines (Top-level for pickle compatibility)
# ----------------------------------------------------------------------

def _worker_hold_lock_and_signal(
    config_root_str: str,
    data_root_str: str,
    acquired_event: Any,
    release_event: Any,
    err_queue: mp.Queue,
) -> None:
    """Acquires the lock, signals acquired_event, then waits for release_event or kill."""
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        with store._profile_lock():
            acquired_event.set()
            release_event.wait(timeout=30.0)
    except Exception as exc:
        err_queue.put(("hold_lock", str(exc)))


def _worker_create_batch(
    config_root_str: str,
    data_root_str: str,
    worker_id: int,
    count: int,
    start_barrier: Any,
    err_queue: mp.Queue,
) -> None:
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        start_barrier.wait(timeout=10.0)
        for i in range(count):
            name = f"adv_p_{worker_id}_{i}"
            store.create(
                name,
                settings=ProfileSettings(model=f"model_{worker_id}"),
                subscription_date=f"2028-0{1 + (i % 9)}-01",
            )
    except Exception as exc:
        import traceback
        err_queue.put((worker_id, "create_batch", str(exc), traceback.format_exc()))


def _worker_update_shared(
    config_root_str: str,
    data_root_str: str,
    worker_id: int,
    shared_names: list[str],
    iterations: int,
    start_barrier: Any,
    err_queue: mp.Queue,
) -> None:
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        start_barrier.wait(timeout=10.0)
        for it in range(iterations):
            for name in shared_names:
                store.update_settings(
                    name,
                    ProfileSettings(
                        model=f"m_{worker_id}_{it}",
                        dangerously_skip_permissions=bool((worker_id + it) % 2),
                    ),
                )
                store.set_subscription_date(name, f"2029-{(it % 12) + 1:02d}-15")
    except Exception as exc:
        import traceback
        err_queue.put((worker_id, "update_shared", str(exc), traceback.format_exc()))


def _worker_churn_profiles(
    config_root_str: str,
    data_root_str: str,
    worker_id: int,
    iterations: int,
    start_barrier: Any,
    err_queue: mp.Queue,
) -> None:
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        start_barrier.wait(timeout=10.0)
        for it in range(iterations):
            churn_name = f"churn_{worker_id}_{it}"
            store.create(churn_name)
            store.update_settings(churn_name, ProfileSettings(model="transient"))
            store.remove(churn_name)
    except Exception as exc:
        import traceback
        err_queue.put((worker_id, "churn", str(exc), traceback.format_exc()))


def _worker_continuous_reader(
    config_root_str: str,
    data_root_str: str,
    iterations: int,
    start_barrier: Any,
    err_queue: mp.Queue,
) -> None:
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        start_barrier.wait(timeout=10.0)
        for _ in range(iterations):
            profiles = store.list()
            for p in profiles:
                # Basic invariant check on every observed profile
                assert isinstance(p.name, str)
                assert isinstance(p.created_at, str)
    except Exception as exc:
        import traceback
        err_queue.put(("reader", "read", str(exc), traceback.format_exc()))


def _worker_compete_create_same_name(
    config_root_str: str,
    data_root_str: str,
    worker_id: int,
    target_name: str,
    start_barrier: Any,
    results_queue: mp.Queue,
) -> None:
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        start_barrier.wait(timeout=10.0)
        store.create(target_name, settings=ProfileSettings(model=f"winner_{worker_id}"))
        results_queue.put((worker_id, "CREATED"))
    except ProfileExists:
        results_queue.put((worker_id, "EXISTS"))
    except Exception as exc:
        results_queue.put((worker_id, f"ERROR: {exc}"))


def _worker_compete_remove_same_name(
    config_root_str: str,
    data_root_str: str,
    worker_id: int,
    target_name: str,
    start_barrier: Any,
    results_queue: mp.Queue,
) -> None:
    try:
        store = ProfileStore(Path(config_root_str), Path(data_root_str))
        start_barrier.wait(timeout=10.0)
        store.remove(target_name)
        results_queue.put((worker_id, "REMOVED"))
    except ProfileNotFound:
        results_queue.put((worker_id, "NOT_FOUND"))
    except Exception as exc:
        results_queue.put((worker_id, f"ERROR: {exc}"))


# ----------------------------------------------------------------------
# Test Classes
# ----------------------------------------------------------------------

class TestAdversarialCrashSimulation(unittest.TestCase):
    """Stress tests simulating abrupt process crashes (kill -9 / SIGKILL) while holding locks."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self.store = ProfileStore(self.config_root, self.data_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @unittest.skipIf(os.name == "nt", "SIGKILL crash simulation is POSIX-specific")
    def test_sigkill_while_holding_lock_releases_flock_to_next_process(self) -> None:
        """Worker process killed with SIGKILL while holding the lock must NOT deadlock subsequent processes."""
        ctx = mp.get_context()
        acquired_evt = ctx.Event()
        release_evt = ctx.Event()
        err_q = ctx.Queue()

        p = ctx.Process(
            target=_worker_hold_lock_and_signal,
            args=(
                str(self.config_root),
                str(self.data_root),
                acquired_evt,
                release_evt,
                err_q,
            ),
        )
        p.start()

        # Wait until the child has explicitly acquired the file lock
        self.assertTrue(acquired_evt.wait(timeout=5.0), "Child failed to acquire lock in time")
        self.assertTrue(p.is_alive(), "Child died prematurely")

        # Terminate child abruptly with SIGKILL (kill -9) while it holds the lock
        os.kill(p.pid, signal.SIGKILL)
        p.join(timeout=5.0)
        self.assertFalse(p.is_alive())
        self.assertEqual(p.exitcode, -signal.SIGKILL)

        # Main process must be able to acquire the lock immediately without blocking/deadlocking
        t0 = time.monotonic()
        with self.store._profile_lock():
            created = self.store.create("survivor_profile")
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 2.0, f"Lock acquisition took too long ({elapsed:.2f}s) after SIGKILL")
        self.assertEqual(created.name, "survivor_profile")
        self.assertIsNotNone(self.store.get("survivor_profile"))

    @unittest.skipIf(os.name == "nt", "SIGKILL crash simulation is POSIX-specific")
    def test_sigkill_crash_barrage_consecutive_workers(self) -> None:
        """Repeated barrage: 6 successive workers killed with SIGKILL, each followed by clean mutation."""
        ctx = mp.get_context()

        for round_idx in range(6):
            acquired_evt = ctx.Event()
            release_evt = ctx.Event()
            err_q = ctx.Queue()

            p = ctx.Process(
                target=_worker_hold_lock_and_signal,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    acquired_evt,
                    release_evt,
                    err_q,
                ),
            )
            p.start()
            self.assertTrue(acquired_evt.wait(timeout=5.0), f"Round {round_idx}: Worker did not acquire lock")
            os.kill(p.pid, signal.SIGKILL)
            p.join(timeout=5.0)

            # Successor operation immediately after kill
            p_name = f"barrage_prof_{round_idx}"
            self.store.create(p_name, settings=ProfileSettings(model=f"model_{round_idx}"))
            self.assertEqual(self.store.get(p_name).name, p_name)

        all_profiles = self.store.list()
        self.assertEqual(len(all_profiles), 6)

    def test_abandoned_tempfile_does_not_break_subsequent_operations(self) -> None:
        """Simulate a crash during _write_json_private leaving .config.xyz.tmp behind."""
        self.config_root.mkdir(parents=True, exist_ok=True)
        junk_tmp = self.config_root / ".config.junk123.tmp"
        junk_tmp.write_text('{"corrupt": true', encoding="utf-8")

        # Normal store operations must succeed regardless of orphaned temp file
        p = self.store.create("p_after_abandoned_tmp")
        self.assertEqual(p.name, "p_after_abandoned_tmp")
        self.store.update_settings("p_after_abandoned_tmp", ProfileSettings(model="ok"))
        self.assertEqual(self.store.get("p_after_abandoned_tmp").settings.model, "ok")


class TestAdversarialMultiprocessingContention(unittest.TestCase):
    """High-concurrency stress testing with 12+ worker processes simultaneously contending."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self.store = ProfileStore(self.config_root, self.data_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_high_concurrency_multiprocessing_12_workers(self) -> None:
        """12 workers simultaneously creating, updating, churning, and reading."""
        ctx = mp.get_context()
        err_queue = ctx.Queue()

        # Pre-create shared profiles for update workers
        shared_names = [f"shared_prof_{i}" for i in range(4)]
        for name in shared_names:
            self.store.create(name)

        num_create_workers = 3
        creates_per_worker = 10
        num_update_workers = 3
        updates_per_worker = 8
        num_churn_workers = 3
        churn_per_worker = 6
        num_reader_workers = 3
        reads_per_worker = 30

        total_workers = (
            num_create_workers
            + num_update_workers
            + num_churn_workers
            + num_reader_workers
        )
        barrier = ctx.Barrier(total_workers)

        processes: list[mp.Process] = []

        # 3 Create workers
        for wid in range(num_create_workers):
            p = ctx.Process(
                target=_worker_create_batch,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    wid,
                    creates_per_worker,
                    barrier,
                    err_queue,
                ),
            )
            processes.append(p)

        # 3 Update workers
        for wid in range(num_update_workers):
            p = ctx.Process(
                target=_worker_update_shared,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    100 + wid,
                    shared_names,
                    updates_per_worker,
                    barrier,
                    err_queue,
                ),
            )
            processes.append(p)

        # 3 Churn workers
        for wid in range(num_churn_workers):
            p = ctx.Process(
                target=_worker_churn_profiles,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    200 + wid,
                    churn_per_worker,
                    barrier,
                    err_queue,
                ),
            )
            processes.append(p)

        # 3 Continuous readers
        for wid in range(num_reader_workers):
            p = ctx.Process(
                target=_worker_continuous_reader,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    reads_per_worker,
                    barrier,
                    err_queue,
                ),
            )
            processes.append(p)

        self.assertEqual(len(processes), 12)

        for p in processes:
            p.start()

        for p in processes:
            p.join(timeout=25.0)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2.0)
                self.fail(f"Worker process {p.pid} hung! Cross-process deadlock detected.")

        # Check errors
        errors = []
        while not err_queue.empty():
            errors.append(err_queue.get_nowait())
        self.assertEqual(errors, [], f"Worker errors occurred: {errors}")

        # Verification of final state
        # Expected: 4 shared + (3 * 10) created = 34 profiles
        all_profiles = self.store.list()
        expected_count = 4 + (num_create_workers * creates_per_worker)
        self.assertEqual(
            len(all_profiles),
            expected_count,
            f"Expected {expected_count} profiles, found {len(all_profiles)}. Lost updates detected!",
        )

        # Check all created profiles exist
        for wid in range(num_create_workers):
            for i in range(creates_per_worker):
                name = f"adv_p_{wid}_{i}"
                p = self.store.get(name)
                self.assertEqual(p.name, name)
                self.assertTrue(p.home.exists())

        # Check config.json is completely valid
        with self.store.config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["profiles"]), expected_count)


class TestAdversarialRaceConditionProbes(unittest.TestCase):
    """Probes exact race conditions when multiple processes compete for identical targets."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self.store = ProfileStore(self.config_root, self.data_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_simultaneous_creation_same_profile_name(self) -> None:
        """8 processes try to create the exact same profile name concurrently."""
        num_workers = 8
        target_name = "contested_profile"
        ctx = mp.get_context()
        barrier = ctx.Barrier(num_workers)
        results_q = ctx.Queue()

        processes = [
            ctx.Process(
                target=_worker_compete_create_same_name,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    wid,
                    target_name,
                    barrier,
                    results_q,
                ),
            )
            for wid in range(num_workers)
        ]

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=15.0)
            if p.is_alive():
                p.terminate()
                self.fail("Compete create worker hung")

        results = []
        while not results_q.empty():
            results.append(results_q.get_nowait())

        created_count = sum(1 for _, res in results if res == "CREATED")
        exists_count = sum(1 for _, res in results if res == "EXISTS")
        error_count = sum(1 for _, res in results if res.startswith("ERROR"))

        self.assertEqual(error_count, 0, f"Unexpected errors: {results}")
        self.assertEqual(created_count, 1, f"Expected exactly 1 winner, got {created_count}")
        self.assertEqual(exists_count, num_workers - 1, f"Expected {num_workers - 1} ProfileExists, got {exists_count}")

        # Directory and store must have exactly this one profile
        self.assertTrue((self.store.profiles_root / target_name).exists())
        self.assertEqual(len(self.store.list()), 1)

    def test_simultaneous_removal_same_profile_name(self) -> None:
        """8 processes try to remove the exact same profile name concurrently."""
        target_name = "target_for_removal"
        self.store.create(target_name)

        num_workers = 8
        ctx = mp.get_context()
        barrier = ctx.Barrier(num_workers)
        results_q = ctx.Queue()

        processes = [
            ctx.Process(
                target=_worker_compete_remove_same_name,
                args=(
                    str(self.config_root),
                    str(self.data_root),
                    wid,
                    target_name,
                    barrier,
                    results_q,
                ),
            )
            for wid in range(num_workers)
        ]

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=15.0)
            if p.is_alive():
                p.terminate()
                self.fail("Compete remove worker hung")

        results = []
        while not results_q.empty():
            results.append(results_q.get_nowait())

        removed_count = sum(1 for _, res in results if res == "REMOVED")
        not_found_count = sum(1 for _, res in results if res == "NOT_FOUND")
        error_count = sum(1 for _, res in results if res.startswith("ERROR"))

        self.assertEqual(error_count, 0, f"Unexpected errors: {results}")
        self.assertEqual(removed_count, 1, f"Expected exactly 1 winner, got {removed_count}")
        self.assertEqual(not_found_count, num_workers - 1, f"Expected {num_workers - 1} ProfileNotFound, got {not_found_count}")

        # Profile directory must no longer exist and config.json must be empty
        self.assertFalse((self.store.profiles_root / target_name).exists())
        self.assertEqual(len(self.store.list()), 0)


class TestAdversarialThreadContentionAndReentrancy(unittest.TestCase):
    """Stress testing deep re-entrancy and thread contention within a single process."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self.store = ProfileStore(self.config_root, self.data_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_deep_reentrant_nesting(self) -> None:
        """Verify lock behaves correctly under recursion depth of 10."""
        def recursive_lock(depth: int) -> None:
            if depth == 0:
                self.store.create("deep_profile")
                return
            with self.store._profile_lock():
                recursive_lock(depth - 1)

        recursive_lock(10)
        self.assertEqual(self.store.get("deep_profile").name, "deep_profile")

    def test_high_concurrency_threads_with_distinct_store_instances(self) -> None:
        """Multiple threads using distinct ProfileStore instances pointing to the same directory."""
        num_threads = 12
        ops_per_thread = 15
        errors: list[tuple[int, Exception]] = []

        def worker(thread_idx: int) -> None:
            try:
                local_store = ProfileStore(self.config_root, self.data_root)
                for i in range(ops_per_thread):
                    name = f"inst_th_{thread_idx}_{i}"
                    local_store.create(name)
                    local_store.update_settings(name, ProfileSettings(model="instance_test"))
                    local_store.set_subscription_date(name, "2030-01-01")
            except Exception as exc:
                errors.append((thread_idx, exc))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        expected_total = num_threads * ops_per_thread
        self.assertEqual(len(self.store.list()), expected_total)

    def test_extreme_multithreading_contention_50_threads(self) -> None:
        """50 concurrent threads executing mixed mutations and nested locks simultaneously."""
        self.store.create("contended_root")
        num_threads = 50
        errors: list[tuple[int, Exception]] = []

        def thread_body(tid: int) -> None:
            try:
                for i in range(10):
                    # Nested lock with mutation
                    with self.store._profile_lock():
                        with self.store._profile_lock():
                            self.store.update_settings(
                                "contended_root",
                                ProfileSettings(model=f"m_{tid}_{i}"),
                            )
                    # Create and remove private
                    pname = f"th50_{tid}_{i}"
                    self.store.create(pname)
                    self.store.remove(pname)
            except Exception as exc:
                errors.append((tid, exc))

        threads = [threading.Thread(target=thread_body, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20.0)

        self.assertEqual(errors, [], f"50-thread errors: {errors}")
        self.assertIsNotNone(self.store.get("contended_root"))

    @unittest.skipIf(not Path("/proc/self/fd").exists(), "Requires /proc/self/fd on Linux")
    def test_no_fd_leak_under_repeated_acquisitions(self) -> None:
        """Verify zero file descriptor leaks across 300 lock/unlock cycles."""
        fd_dir = Path("/proc/self/fd")

        # Warm up lock mechanism once
        with self.store._profile_lock():
            pass

        initial_fd_count = len(list(fd_dir.iterdir()))

        for _ in range(300):
            with self.store._profile_lock():
                with self.store._profile_lock():
                    pass

        final_fd_count = len(list(fd_dir.iterdir()))
        self.assertEqual(
            initial_fd_count,
            final_fd_count,
            f"FD leak detected! Initial FDs: {initial_fd_count}, Final FDs: {final_fd_count}",
        )

    @unittest.skipIf(not Path("/proc/self/fd").exists(), "Requires /proc/self/fd on Linux")
    def test_no_fd_leak_on_exception_inside_lock(self) -> None:
        """Verify zero file descriptor leaks when exceptions are thrown inside lock blocks."""
        fd_dir = Path("/proc/self/fd")

        with self.store._profile_lock():
            pass

        initial_fd_count = len(list(fd_dir.iterdir()))

        for _ in range(200):
            try:
                with self.store._profile_lock():
                    raise RuntimeError("simulated error inside lock")
            except RuntimeError:
                pass

        final_fd_count = len(list(fd_dir.iterdir()))
        self.assertEqual(
            initial_fd_count,
            final_fd_count,
            f"FD leak detected on exceptions! Initial: {initial_fd_count}, Final: {final_fd_count}",
        )


class TestAdversarialExtremeMultiprocessing16Workers(unittest.TestCase):
    """Extreme multiprocessing test with 16 workers and synchronized execution."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self.store = ProfileStore(self.config_root, self.data_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_16_workers_extreme_concurrency(self) -> None:
        """16 workers (4 create, 4 update, 4 churn, 4 read) competing simultaneously."""
        ctx = mp.get_context()
        err_queue = ctx.Queue()

        shared_names = [f"extreme_shared_{i}" for i in range(4)]
        for name in shared_names:
            self.store.create(name)

        num_workers = 16
        barrier = ctx.Barrier(num_workers)
        processes: list[mp.Process] = []

        # 4 create workers (10 profiles each = 40)
        for wid in range(4):
            p = ctx.Process(
                target=_worker_create_batch,
                args=(str(self.config_root), str(self.data_root), wid, 10, barrier, err_queue),
            )
            processes.append(p)

        # 4 update workers
        for wid in range(4):
            p = ctx.Process(
                target=_worker_update_shared,
                args=(str(self.config_root), str(self.data_root), 100 + wid, shared_names, 10, barrier, err_queue),
            )
            processes.append(p)

        # 4 churn workers
        for wid in range(4):
            p = ctx.Process(
                target=_worker_churn_profiles,
                args=(str(self.config_root), str(self.data_root), 200 + wid, 10, barrier, err_queue),
            )
            processes.append(p)

        # 4 continuous readers
        for wid in range(4):
            p = ctx.Process(
                target=_worker_continuous_reader,
                args=(str(self.config_root), str(self.data_root), 40, barrier, err_queue),
            )
            processes.append(p)

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=30.0)
            if p.is_alive():
                p.terminate()
                self.fail(f"Worker {p.pid} hung under 16-worker extreme stress")

        errors = []
        while not err_queue.empty():
            errors.append(err_queue.get_nowait())
        self.assertEqual(errors, [], f"16-worker errors: {errors}")

        # Final profiles: 4 shared + 40 created = 44 profiles
        all_profiles = self.store.list()
        self.assertEqual(len(all_profiles), 44)
        with self.store.config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["profiles"]), 44)


if __name__ == "__main__":
    unittest.main()
