from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agym.cache import CacheManager
from agym.picker import AccountPickerItem, prepare_accounts_for_picker
from agym.profiles import Profile
from agym.quota_api import fetch_quota_direct_async
from agym.tokens import (
    AccountTokenUsage,
    TokenUsage,
    fetch_account_tokens_async,
    render_tokens_table_view,
    scan_profile_conversations,
)
from agym.usage import (
    AccountUsage,
    UsageBucket,
    UsageGroup,
    extract_quota_bucket,
    fetch_account_usage_async,
)
from agym.usage_graphs import (
    compute_fleet_telemetry,
    render_usage_matrix_lines,
    render_usage_telemetry_lines,
)


class TokenSemanticsRegressionTests(unittest.TestCase):
    """Plan §1 & §4: Token accounting semantics & exclusive categories."""

    def test_token_accounting_semantics_formula(self) -> None:
        """Assert: total == input + output, NOT input + output + thinking + cache."""
        inp = 10415
        out = 657
        thk = 616
        crd = 8113
        tot = 11072

        usage = TokenUsage(
            input_tokens=inp,
            output_tokens=out,
            thinking_tokens=thk,
            cache_read_tokens=crd,
        )

        # 1. Total must equal input + output
        self.assertEqual(usage.total_tokens, tot)
        self.assertEqual(usage.total_tokens, inp + out)

        # 2. Total must NOT equal input + output + thinking + cache
        buggy_old_total = inp + out + thk + crd
        self.assertNotEqual(usage.total_tokens, buggy_old_total)
        self.assertEqual(buggy_old_total, 19801)

        # 3. Cache efficiency is cache_read / input
        expected_efficiency = (crd / inp) * 100.0
        self.assertAlmostEqual(usage.cache_efficiency, expected_efficiency, places=4)

        # 4. Exclusive categories internally sum to total
        uncached_input = inp - crd
        cached_input = crd
        visible_output = out - thk
        thinking = thk
        exclusive_sum = uncached_input + cached_input + visible_output + thinking
        self.assertEqual(exclusive_sum, usage.total_tokens)
        self.assertEqual(uncached_input, 2302)
        self.assertEqual(visible_output, 41)

    def test_tokens_volume_percentage_is_share_of_total_accounts_volume(self) -> None:
        """Volume % must be profile_volume / total_accounts_volume, not profile_volume / max_account_volume."""
        acc1 = AccountTokenUsage(
            account="p1",
            status="success",
            usage=TokenUsage(input_tokens=70_000, output_tokens=10_000, total_tokens=80_000),
        )
        acc2 = AccountTokenUsage(
            account="p2",
            status="success",
            usage=TokenUsage(input_tokens=15_000, output_tokens=5_000, total_tokens=20_000),
        )
        # Total accounts volume = 80_000 + 20_000 = 100_000
        lines = render_tokens_table_view([acc1, acc2], use_color=False)
        p1_line = next(l for l in lines if l.startswith("p1"))
        p2_line = next(l for l in lines if l.startswith("p2"))

        # p1 should show 80% (80k/100k), NOT 100% (80k/80k)
        self.assertIn(" 80%", p1_line)
        self.assertNotIn("100%", p1_line)

        # p2 should show 20% (20k/100k), NOT 25% (20k/80k)
        self.assertIn(" 20%", p2_line)
        self.assertNotIn(" 25%", p2_line)


class ProtobufScannerRegressionTests(unittest.TestCase):
    """Plan §2: Scan all usage-bearing steps and use field 3 full output."""

    @staticmethod
    def _make_varint(fn: int, val: int) -> bytes:
        tag = (fn << 3) | 0
        res = [tag]
        while val > 0x7F:
            res.append((val & 0x7F) | 0x80)
            val >>= 7
        res.append(val)
        return bytes(res)

    def test_scanner_includes_multiple_step_types_and_full_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conv_dir = home / ".gemini" / "antigravity-cli" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            db_path = conv_dir / "session1.db"

            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB);")

            # Turn 1: step_type 15
            # field 2=5000 (input), field 3=600 (full output), field 5=2000 (cache),
            # field 9=450 (thinking), field 10=150 (visible output: 450 + 150 == 600)
            stats1 = (
                self._make_varint(2, 5000)
                + self._make_varint(3, 600)
                + self._make_varint(5, 2000)
                + self._make_varint(9, 450)
                + self._make_varint(10, 150)
            )
            blob1 = bytes([(9 << 3) | 2, len(stats1)]) + stats1

            # Turn 2: step_type 16 (Checkpoint / model step != 15)
            # field 2=3000, field 3=400 (full output), field 5=1000, field 9=300, field 10=100
            stats2 = (
                self._make_varint(2, 3000)
                + self._make_varint(3, 400)
                + self._make_varint(5, 1000)
                + self._make_varint(9, 300)
                + self._make_varint(10, 100)
            )
            blob2 = bytes([(9 << 3) | 2, len(stats2)]) + stats2

            c.execute("INSERT INTO steps VALUES (0, 14, NULL);")  # user input
            c.execute("INSERT INTO steps VALUES (1, 15, ?);", (blob1,))
            c.execute("INSERT INTO steps VALUES (2, 132, NULL);")  # tool call
            c.execute("INSERT INTO steps VALUES (3, 16, ?);", (blob2,))  # step_type != 15
            conn.commit()
            conn.close()

            usage, turns, latest = scan_profile_conversations(home)

            # Both turns scanned (step_type 15 AND step_type 16)
            self.assertEqual(turns, 2)
            self.assertEqual(usage.input_tokens, 8000)
            # Output uses field 3 (full output: 600 + 400 = 1000), NOT field 10 (150 + 100 = 250)
            self.assertEqual(usage.output_tokens, 1000)
            self.assertEqual(usage.thinking_tokens, 750)
            self.assertEqual(usage.cache_read_tokens, 3000)
            self.assertEqual(usage.total_tokens, 9000)  # 8000 + 1000

            self.assertIsNotNone(latest)
            self.assertEqual(latest.input_tokens, 3000)
            self.assertEqual(latest.output_tokens, 400)
            self.assertEqual(latest.thinking_tokens, 300)
            self.assertEqual(latest.cache_read_tokens, 1000)
            self.assertEqual(latest.total_tokens, 3400)

    def test_scanner_field10_fallback_when_field3_absent(self) -> None:
        """When field 3 is absent, output_tokens falls back to field 10 + field 9."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conv_dir = home / ".gemini" / "antigravity-cli" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            db_path = conv_dir / "session_fallback.db"

            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB);")

            # Missing field 3; has field 10=200 and field 9=100
            stats = (
                self._make_varint(2, 1000)
                + self._make_varint(9, 100)
                + self._make_varint(10, 200)
            )
            blob = bytes([(9 << 3) | 2, len(stats)]) + stats
            c.execute("INSERT INTO steps VALUES (1, 15, ?);", (blob,))
            conn.commit()
            conn.close()

            usage, turns, _ = scan_profile_conversations(home)
            self.assertEqual(turns, 1)
            self.assertEqual(usage.input_tokens, 1000)
            self.assertEqual(usage.output_tokens, 300)  # 200 + 100
            self.assertEqual(usage.total_tokens, 1300)


class RefreshBehaviorRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Plan §3: Refreshing tokens -f must never increase totals without new conversation usage."""

    async def test_tokens_force_refresh_does_not_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir(parents=True, exist_ok=True)
            profile = Profile(name="prod", home=home, created_at="")
            cache_mgr = CacheManager(cache_root=tmp_path / "cache")
            sem = asyncio.Semaphore(1)

            sample_agy_response = json.dumps({
                "status": "SUCCESS",
                "usage": {
                    "input_tokens": 5000,
                    "output_tokens": 1000,
                    "thinking_tokens": 400,
                    "cache_read_tokens": 2000,
                    "total_tokens": 6000,
                },
                "command": {"name": "usage", "data": {"groups": []}},
            })

            calls = 0

            async def runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                nonlocal calls
                calls += 1
                return 0, sample_agy_response, ""

            # 1. Initial run
            res1 = await fetch_account_tokens_async(
                Path("/fake/agy"), profile, sem, cache_mgr, runner=runner
            )
            self.assertEqual(calls, 1)
            self.assertEqual(res1.usage.total_tokens, 6000)
            self.assertEqual(res1.snapshot_count, 1)

            # 2. Force refresh 1
            res2 = await fetch_account_tokens_async(
                Path("/fake/agy"), profile, sem, cache_mgr, force_refresh=True, runner=runner
            )
            self.assertEqual(calls, 2)
            self.assertEqual(res2.usage.total_tokens, 6000)
            self.assertEqual(res2.snapshot_count, 1)  # No duplicate snapshot added!

            # 3. Force refresh 2
            res3 = await fetch_account_tokens_async(
                Path("/fake/agy"), profile, sem, cache_mgr, force_refresh=True, runner=runner
            )
            self.assertEqual(calls, 3)
            self.assertEqual(res3.usage.total_tokens, 6000)
            self.assertEqual(res3.snapshot_count, 1)  # Still 1!


class Quota5hRetryAndFallbackRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Plan §5: 5h quota validation, retry, and fallback flow."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.profile = Profile(name="test_acc", home=self.home, created_at="")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @mock.patch("agym.quota_api.query_quota_api_async")
    @mock.patch("agym.quota_api.load_profile_token_data")
    async def test_5h_missing_retries_once_then_valid(
        self, mock_load_token: mock.MagicMock, mock_query: mock.AsyncMock
    ) -> None:
        """missing 5h -> retry -> valid."""
        mock_load_token.return_value = {
            "access_token": "fake-token",
            "refresh_token": "fake-refresh-token",
            "expiry": 9999999999,
        }

        incomplete_resp = {
            "groups": [{
                "name": "Gemini Models",
                "buckets": [{
                    "id": "gemini-weekly",
                    "name": "Weekly Limit Remaining",
                    "window": "weekly",
                    "remainingFraction": 0.9,
                }],
            }],
        }
        valid_resp = {
            "groups": [{
                "name": "Gemini Models",
                "buckets": [
                    {
                        "id": "gemini-weekly",
                        "name": "Weekly Limit Remaining",
                        "window": "weekly",
                        "remainingFraction": 0.9,
                    },
                    {
                        "id": "gemini-5h",
                        "name": "Five Hour Limit Remaining",
                        "window": "5h",
                        "remainingFraction": 0.75,
                        "resetTime": "2026-09-27T03:00:00Z",
                    },
                ],
            }],
        }

        mock_query.side_effect = [
            incomplete_resp,
            valid_resp,
        ]

        result = await fetch_quota_direct_async(self.profile)
        self.assertIsNotNone(result)
        usage, _ = result
        self.assertEqual(mock_query.await_count, 2)
        b_5h = extract_quota_bucket(usage, "gemini", "5h")
        self.assertIsNotNone(b_5h)
        self.assertAlmostEqual(b_5h.remaining_fraction, 0.75)

    @mock.patch("agym.usage.fetch_quota_direct_async", new_callable=mock.AsyncMock)
    async def test_5h_missing_retry_fails_falls_back_to_usage_cli(
        self, mock_direct: mock.AsyncMock
    ) -> None:
        """missing 5h -> retry -> missing -> /usage fallback."""
        # Direct API failed to find 5h bucket after retry, returns None
        mock_direct.return_value = None

        cli_response = {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {
                    "groups": [{
                        "name": "Gemini Models",
                        "buckets": [{
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.65,
                            "reset_time": "2026-09-27T05:00:00Z",
                        }],
                    }],
                },
            },
        }

        async def mock_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
            return 0, json.dumps(cli_response), ""

        cache_mgr = CacheManager(cache_root=self.home / "cache")
        sem = asyncio.Semaphore(1)
        res = await fetch_account_usage_async(
            Path("/fake/agy"), self.profile, sem, cache_manager=cache_mgr, runner=mock_runner
        )

        self.assertEqual(res.status, "success")
        b_5h = extract_quota_bucket(res, "gemini", "5h")
        self.assertIsNotNone(b_5h)
        self.assertAlmostEqual(b_5h.remaining_fraction, 0.65)
        # Successfully cached valid response
        self.assertIsNotNone(cache_mgr.get_usage("test_acc"))

    @mock.patch("agym.usage.fetch_quota_direct_async", new_callable=mock.AsyncMock)
    async def test_5h_missing_on_usage_cli_returns_unknown_and_never_cached(
        self, mock_direct: mock.AsyncMock
    ) -> None:
        """If /usage also has no 5h bucket -> return unknown; do NOT cache."""
        mock_direct.return_value = None

        cli_response_no_5h = {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {
                    "groups": [{
                        "name": "Gemini Models",
                        "buckets": [{
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.8,
                        }],
                    }],
                },
            },
        }

        async def mock_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
            return 0, json.dumps(cli_response_no_5h), ""

        cache_mgr = CacheManager(cache_root=self.home / "cache")
        sem = asyncio.Semaphore(1)
        res = await fetch_account_usage_async(
            Path("/fake/agy"), self.profile, sem, cache_manager=cache_mgr, runner=mock_runner
        )

        self.assertEqual(res.status, "unknown")
        self.assertIn("missing 5h quota bucket", res.error or "")
        # Plan §5: Do NOT cache incomplete quota responses!
        self.assertIsNone(cache_mgr.get_usage("test_acc"))


class ExpiredResetTimeRegressionTests(unittest.TestCase):
    """Plan §7: Cached quota with reset_time < now must trigger a refresh immediately."""

    def test_expired_reset_time_invalidates_cache_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cm = CacheManager(cache_root=Path(tmp) / "cache")
            past_dt = datetime.now(timezone.utc) - timedelta(minutes=5)

            cached_data = {
                "status": "success",
                "groups": [{
                    "name": "Gemini Models",
                    "buckets": [{
                        "id": "gemini-5h",
                        "window": "5h",
                        "remaining_fraction": 0.1,
                        "reset_time": past_dt.isoformat(),
                    }],
                }],
            }

            # Cache was written just 2 seconds ago (well within 60s TTL)
            cm.set_usage("acc1", cached_data, json.dumps(cached_data))

            # Reading cache should return None immediately because reset_time < now
            cached = cm.get_usage("acc1", max_age=60.0)
            self.assertIsNone(cached)

    def test_future_reset_time_is_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cm = CacheManager(cache_root=Path(tmp) / "cache")
            future_dt = datetime.now(timezone.utc) + timedelta(hours=2)

            cached_data = {
                "status": "success",
                "groups": [{
                    "name": "Gemini Models",
                    "buckets": [{
                        "id": "gemini-5h",
                        "window": "5h",
                        "remaining_fraction": 0.8,
                        "reset_time": future_dt.isoformat(),
                    }],
                }],
            }

            cm.set_usage("acc1", cached_data, json.dumps(cached_data))
            cached = cm.get_usage("acc1", max_age=60.0)
            self.assertIsNotNone(cached)


class MissingBucketNever100RegressionTests(unittest.TestCase):
    """Plan §6 & §8: Missing bucket must remain unknown and never become 100% or Ready."""

    def test_picker_missing_bucket_is_unknown_and_ranks_below_healthy(self) -> None:
        healthy = AccountUsage(
            account="healthy_50pct",
            status="success",
            groups=[UsageGroup(
                name="Gemini Models",
                description=None,
                buckets=[UsageBucket(
                    id="gemini-5h", name="5h", window="5h",
                    remaining_fraction=0.50,
                    reset_time=datetime.now(timezone.utc) + timedelta(hours=2),
                )],
            )],
        )
        empty_acc = AccountUsage(
            account="empty_0pct",
            status="success",
            groups=[UsageGroup(
                name="Gemini Models",
                description=None,
                buckets=[UsageBucket(
                    id="gemini-5h", name="5h", window="5h",
                    remaining_fraction=0.0,
                    reset_time=datetime.now(timezone.utc) + timedelta(minutes=30),
                )],
            )],
        )
        missing_bucket_acc = AccountUsage(
            account="missing_5h",
            status="unknown",
            error="missing 5h quota bucket",
            groups=[],
        )
        error_acc = AccountUsage(
            account="error_acc",
            status="error",
            error="auth error",
            groups=[],
        )

        items = prepare_accounts_for_picker([healthy, empty_acc, missing_bucket_acc, error_acc], use_color=False)

        # Verify missing_bucket_acc attributes
        missing_item = next(it for it in items if it.account_name == "missing_5h")
        self.assertEqual(missing_item.status, "unknown")
        self.assertEqual(missing_item.limit_5h, "Unknown")
        self.assertEqual(missing_item.reset_time, "-")
        self.assertEqual(missing_item.remaining_fraction, -1.0)
        self.assertEqual(missing_item.raw_quota_sort_key, -1.0)

        # Verify sort order:
        # 1. healthy (50% quota)
        # 2. empty (0% quota)
        # 3. unknown (quota missing)
        # 4. error
        sorted_names = [it.account_name for it in items]
        self.assertEqual(sorted_names, ["healthy_50pct", "empty_0pct", "missing_5h", "error_acc"])

    def test_telemetry_excludes_unknown_from_averages_and_classification(self) -> None:
        u_valid = AccountUsage(
            account="valid",
            status="success",
            groups=[UsageGroup(
                name="Gemini Models",
                description=None,
                buckets=[UsageBucket(
                    id="gemini-5h", name="5h", window="5h",
                    remaining_fraction=0.80,
                    reset_time=None,
                )],
            )],
        )
        u_unknown = AccountUsage(
            account="unknown_acc",
            status="unknown",
            error="missing 5h quota bucket",
        )

        telemetry = compute_fleet_telemetry(
            {"valid": u_valid, "unknown_acc": u_unknown},
            extract_quota_bucket,
        )

        self.assertEqual(telemetry.total_accounts, 2)
        # Gemini average should be 80% (NOT pulled down or up by unknown account)
        self.assertEqual(telemetry.gemini_avg_pct, 80.0)
        # Ready count should be 1 (only valid)
        self.assertEqual(telemetry.ready_count, 1)
        self.assertEqual(telemetry.consuming_count, 0)
        self.assertEqual(telemetry.depleted_count, 0)

    def test_rendering_matrix_and_telemetry_shows_unknown_marker(self) -> None:
        u_unknown = AccountUsage(
            account="pending",
            status="unknown",
            error="missing 5h quota bucket",
        )
        p = Profile(name="pending", home=Path("/fake"), created_at="")

        # Matrix lines
        matrix_lines = render_usage_matrix_lines(
            [p], {"pending": u_unknown}, extract_quota_bucket, use_color=False
        )
        matrix_text = "\n".join(matrix_lines)
        self.assertIn("G:  ?% -", matrix_text)
        self.assertNotIn("100%", matrix_text)

        # Telemetry lines
        telemetry_lines = render_usage_telemetry_lines(
            [p], {"pending": u_unknown}, extract_quota_bucket, lambda dt: "-", use_color=False
        )
        telemetry_text = "\n".join(telemetry_lines)
        self.assertIn("UNKNOWN / PENDING QUOTA", telemetry_text)
        self.assertNotIn("READY (>70%)", telemetry_text)
        self.assertNotIn("Recommendation: Profile 'pending'", telemetry_text)


if __name__ == "__main__":
    unittest.main()
