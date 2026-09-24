from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.sessions import (
    SessionRecord,
    get_session_counts,
    is_pid_alive,
    list_active_sessions,
    register_session,
    unregister_session,
)


class TestSessions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_register_and_unregister_session(self) -> None:
        with mock.patch("agym.sessions.is_pid_alive", return_value=True):
            sid = register_session("profile-a", pid=12345, data_root=self.data_root)
            self.assertTrue(sid.startswith("sess-"))

            # Session file exists on disk
            sess_file = self.data_root / "sessions" / f"{sid}.json"
            self.assertTrue(sess_file.is_file())

            data = json.loads(sess_file.read_text(encoding="utf-8"))
            self.assertEqual(data["session_id"], sid)
            self.assertEqual(data["profile"], "profile-a")
            self.assertEqual(data["pid"], 12345)

            # Active sessions list has it
            active = list_active_sessions(data_root=self.data_root)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].session_id, sid)
            self.assertEqual(active[0].profile, "profile-a")

            # Unregister
            removed = unregister_session(sid, data_root=self.data_root)
            self.assertTrue(removed)
            self.assertFalse(sess_file.exists())

            # Unregister again returns False
            self.assertFalse(unregister_session(sid, data_root=self.data_root))

    def test_multiple_sessions_per_profile(self) -> None:
        with mock.patch("agym.sessions.is_pid_alive", return_value=True):
            sid1 = register_session("ttb", pid=101, data_root=self.data_root)
            sid2 = register_session("ttb", pid=102, data_root=self.data_root)
            sid3 = register_session("main", pid=103, data_root=self.data_root)

            counts = get_session_counts(data_root=self.data_root)
            self.assertEqual(counts.get("ttb"), 2)
            self.assertEqual(counts.get("main"), 1)

            # When requesting specific profiles, includes 0 for missing ones
            counts_with_zeros = get_session_counts(["ttb", "main", "empty"], data_root=self.data_root)
            self.assertEqual(counts_with_zeros["ttb"], 2)
            self.assertEqual(counts_with_zeros["main"], 1)
            self.assertEqual(counts_with_zeros["empty"], 0)

            # Unregister one session from ttb
            unregister_session(sid1, data_root=self.data_root)
            counts_after = get_session_counts(data_root=self.data_root)
            self.assertEqual(counts_after.get("ttb"), 1)
            self.assertEqual(counts_after.get("main"), 1)

    def test_dead_pid_cleanup(self) -> None:
        live_pids = {101, 102, 103}

        def mock_is_alive(pid: int | None) -> bool:
            return pid in live_pids

        with mock.patch("agym.sessions.is_pid_alive", side_effect=mock_is_alive):
            sid1 = register_session("prof1", pid=101, data_root=self.data_root)
            sid2 = register_session("prof1", pid=102, data_root=self.data_root)
            sid3 = register_session("prof2", pid=103, data_root=self.data_root)

            file1 = self.data_root / "sessions" / f"{sid1}.json"
            file2 = self.data_root / "sessions" / f"{sid2}.json"
            file3 = self.data_root / "sessions" / f"{sid3}.json"

            self.assertTrue(file1.exists())
            self.assertTrue(file2.exists())
            self.assertTrue(file3.exists())

            # Simulate process 102 dying
            live_pids.remove(102)

            # Query active sessions -> prunes dead pid 102
            active = list_active_sessions(data_root=self.data_root)
            self.assertEqual(len(active), 2)
            active_sids = {s.session_id for s in active}
            self.assertEqual(active_sids, {sid1, sid3})

            # Dead session file was pruned from disk
            self.assertTrue(file1.exists())
            self.assertFalse(file2.exists())
            self.assertTrue(file3.exists())

            # Counts accurately reflect pruned state
            counts = get_session_counts(data_root=self.data_root)
            self.assertEqual(counts.get("prof1"), 1)
            self.assertEqual(counts.get("prof2"), 1)

    def test_corrupted_session_file_cleanup(self) -> None:
        with mock.patch("agym.sessions.is_pid_alive", return_value=True):
            sid1 = register_session("prof", pid=201, data_root=self.data_root)
            sessions_dir = self.data_root / "sessions"
            corrupt_file = sessions_dir / "sess-corrupt.json"
            corrupt_file.write_text("invalid json contents", encoding="utf-8")

            active = list_active_sessions(data_root=self.data_root)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].session_id, sid1)
            self.assertFalse(corrupt_file.exists())

    def test_is_pid_alive_current_process(self) -> None:
        # Current process PID must be alive
        self.assertTrue(is_pid_alive(os.getpid()))
        # Invalid or non-existent PID
        self.assertFalse(is_pid_alive(None))
        self.assertFalse(is_pid_alive(-1))
        self.assertFalse(is_pid_alive(0))
        # Large PID that almost certainly doesn't exist
        self.assertFalse(is_pid_alive(99999999))
