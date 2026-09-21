from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agym.cache import (
    CacheManager,
    TTL_TOKENS_SECONDS,
    TTL_USAGE_SECONDS,
    format_age,
    format_freshness_badge,
)


class CacheAgeAndBadgeFormattingTests(unittest.TestCase):
    def test_format_age(self) -> None:
        self.assertEqual(format_age(0.4), "just now")
        self.assertEqual(format_age(5.0), "5s ago")
        self.assertEqual(format_age(59.9), "59s ago")
        self.assertEqual(format_age(60.0), "1m ago")
        self.assertEqual(format_age(125.0), "2m ago")
        self.assertEqual(format_age(3600.0), "1h ago")
        self.assertEqual(format_age(3665.0), "1h 1m ago")
        self.assertEqual(format_age(86400.0), "1d ago")

    def test_format_freshness_badge(self) -> None:
        self.assertEqual(format_freshness_badge(False, 0.0, use_color=False), "Live")
        self.assertIn("Live", format_freshness_badge(False, 0.0, use_color=True))
        self.assertEqual(format_freshness_badge(True, 42.0, use_color=False), "Cached 42s ago")
        badge_colored = format_freshness_badge(True, 42.0, use_color=True)
        self.assertIn("Cached 42s ago", badge_colored)
        self.assertIn("\033[90m", badge_colored)


class CacheManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.temp_dir.name) / "cache"
        self.cm = CacheManager(cache_root=self.cache_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_directories_and_file_permissions(self) -> None:
        now = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
        self.cm.set_usage("test-prof", {"status": "success"}, "{}", now=now)

        self.assertTrue(self.cache_root.exists())
        self.assertTrue(self.cm.usage_dir.exists())
        self.assertTrue(self.cm.tokens_dir.exists())

        if os.name != "nt":
            mode_root = stat.S_IMODE(self.cache_root.stat().st_mode)
            mode_usage = stat.S_IMODE(self.cm.usage_dir.stat().st_mode)
            self.assertEqual(mode_root & 0o077, 0)
            self.assertEqual(mode_usage & 0o077, 0)

            cache_file = self.cm.usage_dir / "test-prof.json"
            self.assertTrue(cache_file.exists())
            mode_file = stat.S_IMODE(cache_file.stat().st_mode)
            self.assertEqual(mode_file & 0o077, 0)

    def test_usage_cache_hit_and_expiry(self) -> None:
        t0 = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
        self.cm.set_usage("personal", {"status": "success", "groups": []}, '{"status": "SUCCESS"}', now=t0)

        # Immediate lookup: hit
        hit = self.cm.get_usage("personal", max_age=TTL_USAGE_SECONDS, now=t0)
        self.assertIsNotNone(hit)
        parsed, raw, age, cached_at = hit
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(raw, '{"status": "SUCCESS"}')
        self.assertAlmostEqual(age, 0.0, places=2)
        self.assertEqual(cached_at, t0.isoformat())

        # Lookup after 30 seconds: still a hit
        t_30s = t0 + timedelta(seconds=30)
        hit_30 = self.cm.get_usage("personal", max_age=TTL_USAGE_SECONDS, now=t_30s)
        self.assertIsNotNone(hit_30)
        self.assertAlmostEqual(hit_30[2], 30.0, places=2)

        # Lookup after 61 seconds: expired miss
        t_61s = t0 + timedelta(seconds=61)
        miss_expired = self.cm.get_usage("personal", max_age=TTL_USAGE_SECONDS, now=t_61s)
        self.assertIsNone(miss_expired)

        # Lookup nonexistent profile: miss
        self.assertIsNone(self.cm.get_usage("nonexistent", now=t0))

    def test_tokens_cache_hit_and_expiry(self) -> None:
        t0 = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
        snapshot = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "thinking_tokens": 100,
            "cache_read_tokens": 500,
            "total_tokens": 1800,
        }
        self.cm.record_token_snapshot("work", snapshot, now=t0)

        # Lookup after 5 minutes (300s): hit
        t_5m = t0 + timedelta(seconds=300)
        hit = self.cm.get_tokens("work", max_age=TTL_TOKENS_SECONDS, now=t_5m)
        self.assertIsNotNone(hit)
        data, age, cached_at = hit
        self.assertAlmostEqual(age, 300.0, places=2)
        self.assertEqual(data["cumulative"]["total_tokens"], 1800)

        # Lookup after 11 minutes (660s): expired miss
        t_11m = t0 + timedelta(seconds=660)
        miss = self.cm.get_tokens("work", max_age=TTL_TOKENS_SECONDS, now=t_11m)
        self.assertIsNone(miss)

    def test_cumulative_ledger_accumulation(self) -> None:
        t0 = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
        snap1 = {
            "input_tokens": 500,
            "output_tokens": 150,
            "thinking_tokens": 50,
            "cache_read_tokens": 200,
            "total_tokens": 900,
        }
        res1 = self.cm.record_token_snapshot("dev", snap1, now=t0)
        self.assertEqual(res1["cumulative"]["total_tokens"], 900)
        self.assertEqual(res1["cumulative"]["snapshot_count"], 1)

        # Record second snapshot
        t1 = t0 + timedelta(minutes=10)
        snap2 = {
            "input_tokens": 300,
            "output_tokens": 100,
            "thinking_tokens": 50,
            "cache_read_tokens": 100,
            "total_tokens": 550,
        }
        res2 = self.cm.record_token_snapshot("dev", snap2, now=t1)
        self.assertEqual(res2["cumulative"]["input_tokens"], 800)
        self.assertEqual(res2["cumulative"]["output_tokens"], 250)
        self.assertEqual(res2["cumulative"]["thinking_tokens"], 100)
        self.assertEqual(res2["cumulative"]["cache_read_tokens"], 300)
        self.assertEqual(res2["cumulative"]["total_tokens"], 1450)
        self.assertEqual(res2["cumulative"]["snapshot_count"], 2)

        # Duplicate 0-token snapshot is not piled repeatedly
        snap_zero = {
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
        }
        self.cm.record_token_snapshot("dev", snap_zero, now=t1)
        res_zero = self.cm.load_cumulative_ledger("dev")
        self.assertEqual(res_zero["cumulative"]["snapshot_count"], 2)

    def test_corrupted_cache_recovery(self) -> None:
        now = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
        self.cm._ensure_dirs()
        corrupt_file = self.cm.usage_dir / "corrupted.json"
        corrupt_file.write_text("{ incomplete json ...", encoding="utf-8")

        # Corrupted cache file is handled gracefully as a miss
        result = self.cm.get_usage("corrupted", max_age=TTL_USAGE_SECONDS, now=now)
        self.assertIsNone(result)

        # Overwriting with valid data recovers cleanly
        self.cm.set_usage("corrupted", {"status": "success"}, "{}", now=now)
        recovered = self.cm.get_usage("corrupted", max_age=TTL_USAGE_SECONDS, now=now)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered[0]["status"], "success")

    def test_clear_cache(self) -> None:
        now = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
        self.cm.set_usage("p1", {"status": "success"}, "{}", now=now)
        self.cm.set_usage("p2", {"status": "success"}, "{}", now=now)
        self.cm.record_token_snapshot("p1", {"total_tokens": 100}, now=now)

        # Clear specific profile
        self.cm.clear("p1")
        self.assertIsNone(self.cm.get_usage("p1", now=now))
        self.assertIsNotNone(self.cm.get_usage("p2", now=now))

        # Clear all
        self.cm.clear()
        self.assertIsNone(self.cm.get_usage("p2", now=now))

    def test_rename_cache(self) -> None:
        now = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
        self.cm.set_usage("p1", {"status": "success"}, '{"status": "ok"}', now=now)
        self.cm.record_token_snapshot(
            "p1",
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "thinking_tokens": 20,
                "cache_read_tokens": 30,
                "total_tokens": 200,
            },
            now=now,
        )

        self.cm.rename("p1", "p2")

        # p1 cache files should be gone
        self.assertIsNone(self.cm.get_usage("p1", now=now))
        self.assertIsNone(self.cm.get_tokens("p1", now=now))

        # p2 cache files should exist with updated profile name
        u2 = self.cm.get_usage("p2", now=now)
        self.assertIsNotNone(u2)
        self.assertEqual(u2[0]["status"], "success")

        t2 = self.cm.get_tokens("p2", now=now)
        self.assertIsNotNone(t2)
        self.assertEqual(t2[0]["profile"], "p2")
        self.assertEqual(t2[0]["cumulative"]["total_tokens"], 200)
