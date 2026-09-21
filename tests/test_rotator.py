from __future__ import annotations

import io
import json
import logging
import multiprocessing as mp
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.launcher import build_browser_args, build_profile_env, cleanup_profile_locks
from agym.profiles import Profile, ProfileSettings, ProfileStore, load_accounts_file
from agym.rotator import (
    AccountRotator,
    RotationError,
    RotationState,
    _atomic_load_state,
    _atomic_save_state,
    dispatch_concurrent_workers,
)


def _dummy_worker(account_name: str) -> str:
    """Simulated worker task executed in a spawned child process."""
    return f"processed_{account_name}"


class RotatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        self.data_dir = self.root / "data"
        self.store = ProfileStore(self.config_dir, self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_root_cause_c_crlf_and_utf8_bom_sanitization(self) -> None:
        """Verify CRLF (\r\n) and UTF-8 BOM are stripped so tokens/names aren't corrupted on Windows."""
        acc_file = self.root / "accounts_crlf.txt"
        # Write lines with \r\n and UTF-8 BOM
        content = "\ufeffaccount_one\r\naccount_two\r\n\r\n# comment line\r\naccount_three\r\n"
        acc_file.write_bytes(content.encode("utf-8"))

        accounts = load_accounts_file(acc_file)
        self.assertEqual(accounts, ["account_one", "account_two", "account_three"])
        for acc in accounts:
            self.assertFalse(acc.endswith("\r"))
            self.assertFalse(acc.endswith("\n"))
            self.assertNotIn("\ufeff", acc)

    def test_root_cause_c_json_accounts_file(self) -> None:
        """Verify JSON account files (both list and object format) are parsed cleanly with BOM."""
        json_file = self.root / "accounts.json"
        data = {"accounts": ["user1\r\n", "user2", "user3"]}
        json_file.write_text("\ufeff" + json.dumps(data), encoding="utf-8")

        accounts = load_accounts_file(json_file)
        self.assertEqual(accounts, ["user1", "user2", "user3"])

    def test_root_cause_d_atomic_state_persistence(self) -> None:
        """Verify state file is written atomically via tempfile + os.replace without locking collisions."""
        state_file = self.data_dir / "rotation_state.json"
        state = RotationState(index=2, last_account="acc_beta", history=["acc_alpha", "acc_beta"])
        _atomic_save_state(state_file, state)

        self.assertTrue(state_file.exists())
        loaded = _atomic_load_state(state_file)
        self.assertEqual(loaded.index, 2)
        self.assertEqual(loaded.last_account, "acc_beta")
        self.assertEqual(loaded.history, ["acc_alpha", "acc_beta"])

    def test_sequential_non_repeating_rotation(self) -> None:
        """Verify rotation advances strictly non-repeating across 3+ accounts and logs appropriately."""
        # Setup 3 profiles
        self.store.create("account_a")
        self.store.create("account_b")
        self.store.create("account_c")

        rotator = AccountRotator(store=self.store)

        with self.assertLogs("agym.rotator", level="INFO") as log_capture:
            # 1st rotation -> index 0 (account_a)
            idx1, acc1, path1 = rotator.rotate()
            self.assertEqual(idx1, 0)
            self.assertEqual(acc1, "account_a")
            self.assertTrue(path1.exists())

            # 2nd rotation -> index 1 (account_b)
            idx2, acc2, path2 = rotator.rotate()
            self.assertEqual(idx2, 1)
            self.assertEqual(acc2, "account_b")
            self.assertTrue(path2.exists())

            # 3rd rotation -> index 2 (account_c)
            idx3, acc3, path3 = rotator.rotate()
            self.assertEqual(idx3, 2)
            self.assertEqual(acc3, "account_c")
            self.assertTrue(path3.exists())

            # 4th rotation -> wraps back to index 0 (account_a)
            idx4, acc4, path4 = rotator.rotate()
            self.assertEqual(idx4, 0)
            self.assertEqual(acc4, "account_a")

        # Verify explicit debug logs required by Phase 3 Step 3
        logs = "\n".join(log_capture.output)
        self.assertIn("Active Account: account_a", logs)
        self.assertIn("Active Account: account_b", logs)
        self.assertIn("Active Account: account_c", logs)
        self.assertIn("Rotation Index: 1/3", logs)
        self.assertIn("Rotation Index: 2/3", logs)
        self.assertIn("Rotation Index: 3/3", logs)

    def test_rotator_with_custom_accounts_file(self) -> None:
        """Verify rotation with custom accounts text file."""
        acc_file = self.root / "accounts.txt"
        acc_file.write_text("dev1\r\ndev2\r\ndev3\r\n", encoding="utf-8")

        rotator = AccountRotator(store=self.store, accounts_file=acc_file)
        accounts = rotator.get_accounts()
        self.assertEqual(accounts, ["dev1", "dev2", "dev3"])

        idx, acc, path = rotator.rotate()
        self.assertEqual(acc, "dev1")
        self.assertEqual(idx, 0)

        idx2, acc2, path2 = rotator.rotate()
        self.assertEqual(acc2, "dev2")
        self.assertEqual(idx2, 1)

    def test_rotator_status_and_reset(self) -> None:
        """Verify status reporting and state reset functionality."""
        rotator = AccountRotator(store=self.store, accounts=["user_x", "user_y"])
        st = rotator.get_status()
        self.assertEqual(st["total_accounts"], 2)
        self.assertIsNone(st["current_index"])

        rotator.rotate()
        st2 = rotator.get_status()
        self.assertEqual(st2["current_index"], 0)
        self.assertEqual(st2["active_account"], "user_x")
        self.assertEqual(st2["next_account"], "user_y")

        rotator.reset()
        st3 = rotator.get_status()
        self.assertIsNone(st3["active_account"])

    def test_root_cause_b_profile_isolation_windows(self) -> None:
        """Verify distinct isolated directories and Windows AppData redirection prevent fallback to Default."""
        p1 = self.store.create("profile_alpha")
        p2 = self.store.create("profile_beta")

        env1 = build_profile_env(p1.home, system="Windows")
        env2 = build_profile_env(p2.home, system="Windows")

        # Distinct AppData directories must be configured
        self.assertNotEqual(env1["LOCALAPPDATA"], env2["LOCALAPPDATA"])
        self.assertNotEqual(env1["APPDATA"], env2["APPDATA"])
        self.assertNotEqual(env1["USERPROFILE"], env2["USERPROFILE"])

        # Directories must physically exist on the filesystem
        self.assertTrue(Path(env1["LOCALAPPDATA"]).is_dir())
        self.assertTrue(Path(env2["LOCALAPPDATA"]).is_dir())

        # Verify browser arg construction points to isolated directory
        args1 = build_browser_args(p1.home / "browser_data")
        args2 = build_browser_args(p2.home / "browser_data")
        self.assertNotEqual(args1, args2)
        self.assertIn(str((p1.home / "browser_data").resolve()), args1[0])
        self.assertIn(str((p2.home / "browser_data").resolve()), args2[0])

    def test_root_cause_e_stale_lockfile_cleanup(self) -> None:
        """Verify stale Chromium SingletonLock files are unlinked prior to rotation launch."""
        p = self.store.create("lock_test")
        lock_file = p.home / "AppData" / "Local" / "SingletonLock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text("12345", encoding="utf-8")

        self.assertTrue(lock_file.exists())
        removed = cleanup_profile_locks(p.home)
        self.assertIn(lock_file, removed)
        self.assertFalse(lock_file.exists())

    def test_root_cause_a_multiprocessing_spawn_worker_queue(self) -> None:
        """Simulate Windows multiprocessing spawn mode with IPC queue.

        Ensures workers receive distinct accounts explicitly and do not re-initialize
        module-level iterators to account 0.
        """
        accounts = ["worker_acc_1", "worker_acc_2", "worker_acc_3"]
        # Use spawn context if supported
        context = "spawn" if "spawn" in mp.get_all_start_methods() else None
        results = dispatch_concurrent_workers(
            accounts=accounts,
            worker_func=_dummy_worker,
            num_workers=2,
            timeout=5.0,
            mp_context=context,
        )

        self.assertEqual(len(results), 3)
        processed_accounts = [r[0] for r in results]
        self.assertCountEqual(processed_accounts, accounts)
        for acc, status, res in results:
            self.assertEqual(status, "success")
            self.assertEqual(res, f"processed_{acc}")
