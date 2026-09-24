from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agym import cli
from agym.cache import (
    CacheManager,
    USAGE_CACHE_TTL_SECONDS,
    get_cached_usage_with_meta,
    should_refresh_cache,
)
from agym.picker import (
    AccountPicker,
    AccountPickerItem,
    prepare_accounts_for_picker,
    run_picker,
)
from agym.profiles import Profile, ProfileStore
from agym.usage import (
    AccountUsage,
    UsageBucket,
    UsageGroup,
    fetch_and_cache_usage,
)

SAMPLE_REAL_RESPONSE = json.dumps(
    {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "name": "Weekly Limit Remaining",
                                "window": "weekly",
                                "remaining_fraction": 0.99,
                                "reset_time": "2026-09-27T22:17:44Z",
                            },
                            {
                                "id": "gemini-5h",
                                "name": "Five Hour Limit Remaining",
                                "window": "5h",
                                "remaining_fraction": 0.84,
                                "reset_time": "2026-09-27T03:17:44Z",
                            },
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [
                            {
                                "id": "3p-5h",
                                "name": "Five Hour Limit Remaining",
                                "window": "5h",
                                "remaining_fraction": 1.0,
                                "reset_time": "2026-09-21T04:00:20Z",
                            },
                        ],
                    },
                ],
            },
        },
    }
)


def _make_sample_usage(
    account: str,
    gemini_5h_fraction: float = 1.0,
    gemini_5h_reset_dt: datetime | None = None,
    claude_5h_fraction: float = 1.0,
    status: str = "success",
    error: str | None = None,
    cached: bool = False,
    age_seconds: float = 0.0,
) -> AccountUsage:
    if status != "success":
        return AccountUsage(
            account=account,
            status="error",
            error=error or "simulated error",
            cached=cached,
            age_seconds=age_seconds,
        )

    g_buckets = [
        UsageBucket(
            id="gemini-weekly",
            name="Weekly Limit Remaining",
            window="weekly",
            remaining_fraction=1.0,
            reset_time=None,
        ),
        UsageBucket(
            id="gemini-5h",
            name="Five Hour Limit Remaining",
            window="5h",
            remaining_fraction=gemini_5h_fraction,
            reset_time=gemini_5h_reset_dt,
            reset_time_raw=gemini_5h_reset_dt.isoformat() if gemini_5h_reset_dt else None,
        ),
    ]

    c_buckets = [
        UsageBucket(
            id="3p-5h",
            name="Five Hour Limit Remaining",
            window="5h",
            remaining_fraction=claude_5h_fraction,
            reset_time=None,
        )
    ]

    groups = [
        UsageGroup(name="Gemini Models", description=None, buckets=g_buckets),
        UsageGroup(name="Claude and GPT models", description=None, buckets=c_buckets),
    ]

    return AccountUsage(
        account=account,
        status="success",
        groups=groups,
        cached=cached,
        age_seconds=age_seconds,
    )


class TestPickerCache(unittest.TestCase):
    def test_should_refresh_cache_ttl(self) -> None:
        now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
        now_ts = now.timestamp()

        # Missing cache -> True
        self.assertTrue(should_refresh_cache(None, now=now))

        # Force fresh -> True
        self.assertTrue(should_refresh_cache(now_ts, force_fresh=True, now=now))

        # Fresh cache (within 300s):
        # 0s ago -> False
        self.assertFalse(should_refresh_cache(now_ts, now=now))
        # 299s ago -> False
        self.assertFalse(should_refresh_cache(now_ts - 299.0, now=now))
        # Exactly 300s ago -> False
        self.assertFalse(should_refresh_cache(now_ts - 300.0, now=now))

        # Stale cache (> 300s):
        # 301s ago -> True
        self.assertTrue(should_refresh_cache(now_ts - 301.0, now=now))
        # 600s ago -> True
        self.assertTrue(should_refresh_cache(now_ts - 600.0, now=now))

    def test_get_cached_usage_with_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cm = CacheManager(cache_root=Path(tmp))
            # Missing entry
            data, ts = cm.get_cached_usage_with_meta("missing")
            self.assertIsNone(data)
            self.assertIsNone(ts)

            # Saved entry
            now = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)
            cm.set_usage("test-prof", {"status": "success"}, "raw json", now=now)

            data, ts = cm.get_cached_usage_with_meta("test-prof")
            self.assertEqual(data, {"status": "success"})
            self.assertEqual(ts, now.timestamp())

            # Convenience module-level function
            data2, ts2 = get_cached_usage_with_meta("test-prof", cache_manager=cm)
            self.assertEqual(data2, {"status": "success"})
            self.assertEqual(ts2, now.timestamp())

    def test_fetch_and_cache_usage_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir(parents=True)
            p = Profile(name="p1", home=home, created_at="")
            cm = CacheManager(cache_root=tmp_path / "cache")
            call_count = 0

            async def mock_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                nonlocal call_count
                call_count += 1
                return 0, SAMPLE_REAL_RESPONSE, ""

            # 1. First fetch: live execution, populates cache
            usages1 = fetch_and_cache_usage(
                profiles=[p],
                agy_path=Path("/fake/agy"),
                cache_manager=cm,
                runner=mock_runner,
            )
            self.assertEqual(call_count, 1)
            self.assertFalse(usages1[0].cached)

            # 2. Immediate second fetch: served from cache (< 300s TTL)
            usages2 = fetch_and_cache_usage(
                profiles=[p],
                agy_path=Path("/fake/agy"),
                cache_manager=cm,
                runner=mock_runner,
            )
            self.assertEqual(call_count, 1)
            self.assertTrue(usages2[0].cached)

            # 3. Third fetch with force=True: live refresh even if <300s
            usages3 = fetch_and_cache_usage(
                profiles=[p],
                force=True,
                agy_path=Path("/fake/agy"),
                cache_manager=cm,
                runner=mock_runner,
            )
            self.assertEqual(call_count, 2)
            self.assertFalse(usages3[0].cached)

            # 4. Age > 300s (e.g. 301s ago): automatically refreshes
            stale_time = datetime.now(timezone.utc) - timedelta(seconds=301)
            cm.set_usage("p1", {"status": "success"}, SAMPLE_REAL_RESPONSE, now=stale_time)
            usages4 = fetch_and_cache_usage(
                profiles=[p],
                force=False,
                agy_path=Path("/fake/agy"),
                cache_manager=cm,
                runner=mock_runner,
            )
            self.assertEqual(call_count, 3)
            self.assertFalse(usages4[0].cached)


class TestPickerFormattingAndSorting(unittest.TestCase):
    def test_prepare_accounts_for_picker_sorting(self) -> None:
        now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

        # 1. High quota (90%)
        u1 = _make_sample_usage("acc-high", gemini_5h_fraction=0.90, gemini_5h_reset_dt=now + timedelta(hours=2))
        # 2. Medium quota (50%)
        u2 = _make_sample_usage("acc-med", gemini_5h_fraction=0.50, gemini_5h_reset_dt=now + timedelta(hours=1))
        # 3. Exhausted quota (0%), resets in 10 minutes
        u3 = _make_sample_usage("acc-exhausted-soon", gemini_5h_fraction=0.0, gemini_5h_reset_dt=now + timedelta(minutes=10))
        # 4. Exhausted quota (0%), resets in 45 minutes
        u4 = _make_sample_usage("acc-exhausted-later", gemini_5h_fraction=0.0, gemini_5h_reset_dt=now + timedelta(minutes=45))
        # 5. Full quota (100%), resets in 0 (Ready)
        u5 = _make_sample_usage("acc-full", gemini_5h_fraction=1.0)
        # 6. Error account
        u6 = _make_sample_usage("acc-error", status="error")

        # Unsorted input
        usages = [u3, u6, u1, u4, u2, u5]
        items = prepare_accounts_for_picker(usages, now=now, use_color=False)

        names = [item.account_name for item in items]
        expected = [
            "acc-full",            # 100%
            "acc-high",            # 90%
            "acc-med",             # 50%
            "acc-exhausted-soon",  # 0%, resets in 10m (soonest)
            "acc-exhausted-later", # 0%, resets in 45m
            "acc-error",           # Error (-1.0)
        ]
        self.assertEqual(names, expected)

    def test_prepare_accounts_for_picker_tiebreak_alphabetical(self) -> None:
        now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
        u_b = _make_sample_usage("beta", gemini_5h_fraction=0.50, gemini_5h_reset_dt=now + timedelta(hours=1))
        u_a = _make_sample_usage("alpha", gemini_5h_fraction=0.50, gemini_5h_reset_dt=now + timedelta(hours=1))

        items = prepare_accounts_for_picker([u_b, u_a], now=now, use_color=False)
        self.assertEqual([item.account_name for item in items], ["alpha", "beta"])

    def test_color_coding_thresholds(self) -> None:
        now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
        u_green = _make_sample_usage("p-green", gemini_5h_fraction=0.85)
        u_yellow = _make_sample_usage("p-yellow", gemini_5h_fraction=0.40)
        u_red = _make_sample_usage("p-red", gemini_5h_fraction=0.05)
        u_err = _make_sample_usage("p-err", status="error")

        items = prepare_accounts_for_picker([u_green, u_yellow, u_red, u_err], now=now, use_color=True)
        item_map = {item.account_name: item for item in items}

        # Uses shared palette from usage_graphs
        self.assertIn("\033[38;5;40m", item_map["p-green"].formatted_line)   # Green for 85%
        self.assertIn("\033[38;5;214m", item_map["p-yellow"].formatted_line) # Amber for 40%
        self.assertIn("\033[38;5;196m", item_map["p-red"].formatted_line)    # Red for 5%
        self.assertIn("\033[31m", item_map["p-err"].formatted_line)          # Red for error

    def test_line_layout_format(self) -> None:
        now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
        u = _make_sample_usage("personal", gemini_5h_fraction=0.84, gemini_5h_reset_dt=now + timedelta(hours=1, minutes=45))
        items = prepare_accounts_for_picker([u], session_counts={"personal": 2}, now=now, use_color=False)
        item = items[0]

        # Check plain formatted line structure
        self.assertIn("personal", item.formatted_line_plain)
        self.assertIn("2", item.formatted_line_plain)
        self.assertIn("5h:", item.formatted_line_plain)
        self.assertIn("84%", item.formatted_line_plain)
        self.assertIn("Wk:", item.formatted_line_plain)
        self.assertNotIn("Sub", item.formatted_line_plain)

    def test_picker_display_renders_target_layout(self) -> None:
        now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
        u1 = _make_sample_usage("ttb", gemini_5h_fraction=0.82, gemini_5h_reset_dt=now + timedelta(hours=2))
        u2 = _make_sample_usage("account2", gemini_5h_fraction=0.98, gemini_5h_reset_dt=now + timedelta(hours=4))
        items = prepare_accounts_for_picker(
            [u1, u2],
            session_counts={"ttb": 2, "account2": 0},
            now=now,
            use_color=False,
        )
        picker = AccountPicker(items, freshness_summary="Data: Cached · 45s ago", use_color=False)
        lines = picker._render_menu_lines()
        rendered = "\n".join(lines)

        self.assertIn("Data: Cached · 45s ago", rendered)
        self.assertIn("Account          Sess   Gemini 5h                 Gemini Wk", rendered)
        self.assertIn("> account2       0      5h: [█████████▊]  98%   4h  Wk: [██████████] 100%    -", rendered)
        self.assertIn("  ttb            2      5h: [████████▎░]  82%   2h  Wk: [██████████] 100%    -", rendered)
        self.assertNotIn("Sub", rendered)

    def test_picker_sorting_complete_tiebreak(self) -> None:
        now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

        # 1. Gemini 5h remaining descending
        u_5h_high = _make_sample_usage("p_5h_high", gemini_5h_fraction=0.90)
        u_5h_low = _make_sample_usage("p_5h_low", gemini_5h_fraction=0.70)

        # 2. Gemini weekly remaining descending (same 5h 0.80)
        u_wk_high = _make_sample_usage("p_wk_high", gemini_5h_fraction=0.80)
        # Manually alter weekly bucket fraction
        u_wk_high.groups[0].buckets[0] = UsageBucket(
            id="gemini-weekly", name="Weekly", window="weekly", remaining_fraction=0.95, reset_time=None
        )
        u_wk_low = _make_sample_usage("p_wk_low", gemini_5h_fraction=0.80)
        u_wk_low.groups[0].buckets[0] = UsageBucket(
            id="gemini-weekly", name="Weekly", window="weekly", remaining_fraction=0.40, reset_time=None
        )

        # 3. Open sessions ascending (same 5h 0.60, same weekly 1.0)
        u_sess_0 = _make_sample_usage("p_sess_0", gemini_5h_fraction=0.60)
        u_sess_2 = _make_sample_usage("p_sess_2", gemini_5h_fraction=0.60)
        session_counts = {"p_sess_0": 0, "p_sess_2": 2}

        # 4. 5h reset ascending (same 5h 0.0, same weekly 1.0, same sess 0)
        u_reset_soon = _make_sample_usage("p_reset_soon", gemini_5h_fraction=0.0, gemini_5h_reset_dt=now + timedelta(minutes=10))
        u_reset_late = _make_sample_usage("p_reset_late", gemini_5h_fraction=0.0, gemini_5h_reset_dt=now + timedelta(minutes=45))

        # 5. Profile name ascending (same everything)
        u_name_a = _make_sample_usage("alpha", gemini_5h_fraction=0.50)
        u_name_b = _make_sample_usage("beta", gemini_5h_fraction=0.50)

        all_usages = [
            u_name_b,
            u_reset_late,
            u_sess_2,
            u_5h_low,
            u_wk_low,
            u_name_a,
            u_reset_soon,
            u_sess_0,
            u_wk_high,
            u_5h_high,
        ]

        items = prepare_accounts_for_picker(all_usages, session_counts=session_counts, now=now, use_color=False)
        order = [item.account_name for item in items]
        expected_order = [
            "p_5h_high",     # 5h: 0.90
            "p_wk_high",     # 5h: 0.80, wk: 0.95
            "p_wk_low",      # 5h: 0.80, wk: 0.40
            "p_5h_low",      # 5h: 0.70
            "p_sess_0",      # 5h: 0.60, sess: 0
            "p_sess_2",      # 5h: 0.60, sess: 2
            "alpha",         # 5h: 0.50, name: alpha
            "beta",          # 5h: 0.50, name: beta
            "p_reset_soon",  # 5h: 0.0, reset: 10m
            "p_reset_late",  # 5h: 0.0, reset: 45m
        ]
        self.assertEqual(order, expected_order)


class TestAccountPickerInteractive(unittest.TestCase):
    def test_picker_navigation_and_select(self) -> None:
        items = [
            AccountPickerItem(
                account_name=f"acc-{i}",
                limit_5h="100%",
                reset_time="Ready",
                raw_quota_sort_key=1.0,
                raw_reset_timestamp=None,
                remaining_fraction=1.0,
                status="success",
                formatted_line=f"[●] acc-{i}",
                formatted_line_plain=f"[●] acc-{i}",
            )
            for i in range(3)
        ]

        # Key sequence: Down arrow, Down arrow, Up arrow, Enter
        # Selected index starts at 0 -> Down -> 1 -> Down -> 2 -> Up -> 1 -> Enter -> "acc-1"
        keys = iter(["\x1b[B", "\x1b[B", "\x1b[A", "\r"])
        out = io.StringIO()

        selected = run_picker(
            items,
            stdout=out,
            key_reader=lambda: next(keys),
            use_color=False,
        )
        self.assertEqual(selected, "acc-1")

    def test_picker_cancellation(self) -> None:
        items = [
            AccountPickerItem(
                account_name="acc-0",
                limit_5h="100%",
                reset_time="Ready",
                raw_quota_sort_key=1.0,
                raw_reset_timestamp=None,
                remaining_fraction=1.0,
                status="success",
                formatted_line="[●] acc-0",
                formatted_line_plain="[●] acc-0",
            )
        ]

        # Cancel via 'q'
        selected_q = run_picker(items, stdout=io.StringIO(), key_reader=lambda: "q", use_color=False)
        self.assertIsNone(selected_q)

        # Cancel via Esc '\x1b'
        selected_esc = run_picker(items, stdout=io.StringIO(), key_reader=lambda: "\x1b", use_color=False)
        self.assertIsNone(selected_esc)

        # Cancel via Ctrl+C '\x03'
        selected_ctrlc = run_picker(items, stdout=io.StringIO(), key_reader=lambda: "\x03", use_color=False)
        self.assertIsNone(selected_ctrlc)


class TestSelectCLICommand(unittest.TestCase):
    @mock.patch("agym.cli.ProfileStore")
    def test_select_empty_profiles(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            Store.return_value = store

            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["select"])
            self.assertEqual(code, 0)
            self.assertIn("No profiles configured", out.getvalue())

    @mock.patch("agym.cli.fetch_and_cache_usage")
    @mock.patch("agym.cli.run_agy")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.ProfileStore")
    def test_select_cached_vs_fresh(
        self,
        Store: mock.Mock,
        resolve: mock.Mock,
        run_mock: mock.Mock,
        mock_fetch: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p1 = store.create("p1")
            p2 = store.create("p2")
            Store.return_value = store
            resolve.return_value = Path("/mock/agy")
            run_mock.return_value = 0

            cm = CacheManager(cache_root=Path(tmp) / "cache")
            now = datetime.now(timezone.utc)

            u1 = _make_sample_usage("p1", gemini_5h_fraction=0.95)
            u2 = _make_sample_usage("p2", gemini_5h_fraction=0.40)
            mock_fetch.return_value = [u1, u2]

            # Seed cache with fresh usage (<300s)
            cm.set_usage("p1", {"status": "success"}, SAMPLE_REAL_RESPONSE, now=now)
            cm.set_usage("p2", {"status": "success"}, SAMPLE_REAL_RESPONSE, now=now)

            # 1. Run select with warm cache: should NOT print "Fetching fresh usage data..."
            out1 = io.StringIO()
            with mock.patch("agym.cli.CacheManager", return_value=cm):
                with mock.patch("agym.cli.run_picker", return_value="p1"):
                    with mock.patch("sys.stdout", out1):
                        code1 = cli.main(["select"])

            self.assertEqual(code1, 0)
            self.assertNotIn("Fetching fresh usage data...", out1.getvalue())
            self.assertIn("Opening agy with account 'p1'...", out1.getvalue())
            run_mock.assert_called_with(
                Path("/mock/agy"), p1, [], replace_process=True, data_root=store.data_root
            )
            mock_fetch.assert_called_with(
                profiles=[p1, p2],
                force=False,
                agy_path=None,
                cache_manager=cm,
                cache_ttl=300.0,
                data_root=Path(tmp) / "data",
                runner=None,
            )

            # 2. Run select with -f / --fresh: must print "Fetching fresh usage data..."
            out2 = io.StringIO()
            with mock.patch("agym.cli.CacheManager", return_value=cm):
                with mock.patch("agym.cli.run_picker", return_value="p1"):
                    with mock.patch("sys.stdout", out2):
                        code2 = cli.main(["select", "-f"])

            self.assertEqual(code2, 0)
            self.assertIn("Fetching fresh usage data...", out2.getvalue())
            mock_fetch.assert_called_with(
                profiles=[p1, p2],
                force=True,
                agy_path=None,
                cache_manager=cm,
                cache_ttl=300.0,
                data_root=Path(tmp) / "data",
                runner=None,
            )

            # 3. Boundary test: adjust cache timestamp to 301 seconds ago -> must refresh
            stale_time = datetime.now(timezone.utc) - timedelta(seconds=301)
            cm.set_usage("p1", {"status": "success"}, SAMPLE_REAL_RESPONSE, now=stale_time)
            out3 = io.StringIO()
            with mock.patch("agym.cli.CacheManager", return_value=cm):
                with mock.patch("agym.cli.run_picker", return_value="p1"):
                    with mock.patch("sys.stdout", out3):
                        code3 = cli.main(["select"])

            self.assertEqual(code3, 0)
            self.assertIn("Fetching fresh usage data...", out3.getvalue())

    @mock.patch("agym.cli.fetch_and_cache_usage")
    @mock.patch("agym.cli.run_agy")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.ProfileStore")
    def test_select_uses_configured_cache_ttl(
        self,
        Store: mock.Mock,
        resolve: mock.Mock,
        run_mock: mock.Mock,
        mock_fetch: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p = store.create("p1")
            store.set_usage_cache_ttl(45.0)
            Store.return_value = store
            resolve.return_value = Path("/mock/agy")
            run_mock.return_value = 0
            cm = CacheManager(cache_root=Path(tmp) / "cache")
            now = datetime.now(timezone.utc)
            cm.set_usage("p1", {"status": "success"}, SAMPLE_REAL_RESPONSE, now=now)
            mock_fetch.return_value = [_make_sample_usage("p1")]

            out = io.StringIO()
            with mock.patch("agym.cli.CacheManager", return_value=cm):
                with mock.patch("agym.cli.run_picker", return_value="p1"):
                    with mock.patch("sys.stdout", out):
                        code = cli.main(["select"])

            self.assertEqual(code, 0)
            mock_fetch.assert_called_with(
                profiles=[p],
                force=False,
                agy_path=None,
                cache_manager=cm,
                cache_ttl=45.0,
                data_root=Path(tmp) / "data",
                runner=None,
            )

    @mock.patch("agym.cli.fetch_and_cache_usage")
    @mock.patch("agym.cli.ProfileStore")
    def test_select_cancel_exits_cleanly(self, Store: mock.Mock, mock_fetch: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            store.create("p1")
            Store.return_value = store
            cm = CacheManager(cache_root=Path(tmp) / "cache")
            now = datetime.now(timezone.utc)
            cm.set_usage("p1", {"status": "success"}, SAMPLE_REAL_RESPONSE, now=now)
            mock_fetch.return_value = [_make_sample_usage("p1")]

            out = io.StringIO()
            with mock.patch("agym.cli.CacheManager", return_value=cm):
                with mock.patch("agym.cli.run_picker", return_value=None):
                    with mock.patch("sys.stdout", out):
                        code = cli.main(["select"])

            self.assertEqual(code, 0)
            self.assertNotIn("Opening agy", out.getvalue())

    @mock.patch("agym.cli.fetch_and_cache_usage")
    @mock.patch("agym.cli.run_agy")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.ProfileStore")
    def test_pick_alias(
        self,
        Store: mock.Mock,
        resolve: mock.Mock,
        run_mock: mock.Mock,
        mock_fetch: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p = store.create("p1")
            Store.return_value = store
            resolve.return_value = Path("/mock/agy")
            run_mock.return_value = 0
            cm = CacheManager(cache_root=Path(tmp) / "cache")
            now = datetime.now(timezone.utc)
            cm.set_usage("p1", {"status": "success"}, "{}", now=now)
            mock_fetch.return_value = [_make_sample_usage("p1")]

            out = io.StringIO()
            with mock.patch("agym.cli.CacheManager", return_value=cm):
                with mock.patch("agym.cli.run_picker", return_value="p1"):
                    with mock.patch("sys.stdout", out):
                        code = cli.main(["pick", "-p", "hello"])

            self.assertEqual(code, 0)
            self.assertIn("Opening agy with account 'p1'...", out.getvalue())
            run_mock.assert_called_with(
                Path("/mock/agy"), p, ["-p", "hello"], replace_process=True, data_root=store.data_root
            )


if __name__ == "__main__":
    unittest.main()
