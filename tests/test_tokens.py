from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agym import cli
from agym.cache import CacheManager
from agym.profiles import Profile, ProfileStore
from agym.tokens import (
    AccountTokenUsage,
    TokenUsage,
    fetch_account_tokens_async,
    fetch_all_tokens,
    format_token_count,
    parse_token_usage_payload,
    render_comparison_bar,
    render_composition_breakdown_table,
    render_stacked_bar,
    render_summary_card,
    render_token_dashboard,
    run_tokens,
    scan_profile_conversations,
    scan_profile_session_tokens,
)

SAMPLE_RESPONSE_WITH_TOKENS = json.dumps(
    {
        "status": "SUCCESS",
        "usage": {
            "input_tokens": 1250,
            "output_tokens": 420,
            "thinking_tokens": 180,
            "cache_read_tokens": 650,
            "total_tokens": 1670,
        },
        "command": {
            "name": "usage",
            "data": {
                "groups": [],
            },
        },
    }
)


class TokenUsageModelTests(unittest.TestCase):
    def test_token_usage_defaults_and_efficiency(self) -> None:
        empty = TokenUsage()
        self.assertEqual(empty.total_tokens, 0)
        self.assertEqual(empty.cache_efficiency, 0.0)

        usage = TokenUsage(
            input_tokens=1500,
            output_tokens=300,
            thinking_tokens=100,
            cache_read_tokens=500,
        )
        self.assertEqual(usage.total_tokens, 1800)
        # cache_read / input = 500 / 1500 = 33.33%
        self.assertAlmostEqual(usage.cache_efficiency, 100.0 / 3.0)

        as_dict = usage.to_dict()
        self.assertEqual(as_dict["input_tokens"], 1500)
        self.assertEqual(as_dict["cache_efficiency"], 33.33)

        reloaded = TokenUsage.from_dict(as_dict)
        self.assertEqual(reloaded, usage)

    def test_account_token_usage_to_dict(self) -> None:
        usage = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)
        account_usage = AccountTokenUsage(
            account="personal",
            status="success",
            usage=usage,
            cached=True,
            age_seconds=42.3,
            cached_at="2026-09-21T02:00:00Z",
            snapshot_count=3,
        )
        d = account_usage.to_dict()
        self.assertEqual(d["account"], "personal")
        self.assertEqual(d["status"], "success")
        self.assertTrue(d["cached"])
        self.assertEqual(d["age_seconds"], 42.3)
        self.assertEqual(d["cached_at"], "2026-09-21T02:00:00Z")
        self.assertEqual(d["usage"]["total_tokens"], 150)
        self.assertEqual(d["snapshot_count"], 3)


class FormattingTests(unittest.TestCase):
    def test_format_token_count(self) -> None:
        self.assertEqual(format_token_count(0), "0")
        self.assertEqual(format_token_count(850), "850")
        self.assertEqual(format_token_count(1000), "1.0k")
        self.assertEqual(format_token_count(12400), "12.4k")
        self.assertEqual(format_token_count(1200000), "1.2M")
        self.assertEqual(format_token_count(2500000000), "2.5B")


class LogScannerTests(unittest.TestCase):
    def test_parse_token_usage_payload(self) -> None:
        payload = json.loads(SAMPLE_RESPONSE_WITH_TOKENS)
        tu = parse_token_usage_payload(payload)
        self.assertEqual(tu.input_tokens, 1250)
        self.assertEqual(tu.output_tokens, 420)
        self.assertEqual(tu.thinking_tokens, 180)
        self.assertEqual(tu.cache_read_tokens, 650)
        self.assertEqual(tu.total_tokens, 1670)

        # Empty or missing usage
        self.assertEqual(parse_token_usage_payload({}).total_tokens, 0)

    def test_scan_profile_session_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # Empty home
            empty_tokens = scan_profile_session_tokens(home)
            self.assertEqual(empty_tokens.total_tokens, 0)

            # Create mock transcript
            logs_dir = home / ".gemini" / "antigravity-cli" / "brain" / "conv1" / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            transcript_file = logs_dir / "transcript.jsonl"
            lines = [
                json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "hello"}),
                json.dumps({
                    "step_index": 1,
                    "type": "MODEL_RESPONSE",
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 100,
                        "thinking_tokens": 50,
                        "cache_read_tokens": 50,
                        "total_tokens": 300,
                    },
                }),
                json.dumps({
                    "step_index": 2,
                    "type": "MODEL_RESPONSE",
                    "usage": {
                        "input_tokens": 300,
                        "output_tokens": 150,
                        "thinking_tokens": 50,
                        "cache_read_tokens": 100,
                        "total_tokens": 450,
                    },
                }),
            ]
            transcript_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            scanned = scan_profile_session_tokens(home)
            self.assertEqual(scanned.input_tokens, 500)
            self.assertEqual(scanned.output_tokens, 250)
            self.assertEqual(scanned.thinking_tokens, 100)
            self.assertEqual(scanned.cache_read_tokens, 150)
            self.assertEqual(scanned.total_tokens, 750)

    def test_scan_profile_conversations_sqlite(self) -> None:
        def make_varint(fn: int, val: int) -> bytes:
            tag = (fn << 3) | 0
            res = [tag]
            while val > 0x7F:
                res.append((val & 0x7F) | 0x80)
                val >>= 7
            res.append(val)
            return bytes(res)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conv_dir = home / ".gemini" / "antigravity-cli" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            db_path = conv_dir / "conv1.db"

            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB);")

            # Turn 1: 1200 in, 300 out (100 thk, 200 resp), 500 crd
            stats1 = (
                make_varint(2, 1200)
                + make_varint(3, 300)
                + make_varint(5, 500)
                + make_varint(9, 100)
                + make_varint(10, 200)
            )
            blob1 = bytes([(9 << 3) | 2, len(stats1)]) + stats1

            # Turn 2: 800 in, 150 out (50 thk, 100 resp), 300 crd
            stats2 = (
                make_varint(2, 800)
                + make_varint(3, 150)
                + make_varint(5, 300)
                + make_varint(9, 50)
                + make_varint(10, 100)
            )
            blob2 = bytes([(9 << 3) | 2, len(stats2)]) + stats2

            c.execute("INSERT INTO steps VALUES (0, 14, NULL);")  # user input
            c.execute("INSERT INTO steps VALUES (1, 15, ?);", (blob1,))
            c.execute("INSERT INTO steps VALUES (2, 132, NULL);")  # tool call
            c.execute("INSERT INTO steps VALUES (3, 15, ?);", (blob2,))
            conn.commit()
            conn.close()

            usage, turns, latest = scan_profile_conversations(home)
            self.assertEqual(turns, 2)
            self.assertEqual(usage.input_tokens, 2000)
            self.assertEqual(usage.output_tokens, 450)  # 300 + 150
            self.assertEqual(usage.thinking_tokens, 150)  # 100 + 50
            self.assertEqual(usage.cache_read_tokens, 800)  # 500 + 300
            self.assertEqual(usage.total_tokens, 2450)  # 2000 + 450

            self.assertIsNotNone(latest)
            self.assertEqual(latest.input_tokens, 800)
            self.assertEqual(latest.output_tokens, 150)
            self.assertEqual(latest.thinking_tokens, 50)
            self.assertEqual(latest.cache_read_tokens, 300)
            self.assertEqual(latest.total_tokens, 950)

            # Also check scan_profile_session_tokens backwards compatibility
            compat = scan_profile_session_tokens(home)
            self.assertEqual(compat, usage)


class FetchAccountTokensAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_live_then_cache_hit_then_force_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir(parents=True, exist_ok=True)
            profile = Profile(name="personal", home=home, created_at="")
            cache_manager = CacheManager(cache_root=tmp_path / "cache")
            sem = asyncio.Semaphore(1)

            calls = 0

            async def mock_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                nonlocal calls
                calls += 1
                return 0, SAMPLE_RESPONSE_WITH_TOKENS, ""

            # 1. First fetch: live execution
            res1 = await fetch_account_tokens_async(
                Path("/fake/agy"),
                profile,
                sem,
                cache_manager,
                runner=mock_runner,
            )
            self.assertEqual(calls, 1)
            self.assertEqual(res1.status, "success")
            self.assertFalse(res1.cached)
            self.assertEqual(res1.usage.total_tokens, 1670)
            self.assertEqual(res1.snapshot_count, 1)

            # 2. Second fetch: should hit cache and NOT invoke mock_runner!
            res2 = await fetch_account_tokens_async(
                Path("/fake/agy"),
                profile,
                sem,
                cache_manager,
                force_refresh=False,
                runner=mock_runner,
            )
            self.assertEqual(calls, 1)  # unchanged!
            self.assertTrue(res2.cached)
            self.assertEqual(res2.usage.total_tokens, 1670)

            # 3. Third fetch: force_refresh=True should bypass cache and invoke runner
            res3 = await fetch_account_tokens_async(
                Path("/fake/agy"),
                profile,
                sem,
                cache_manager,
                force_refresh=True,
                runner=mock_runner,
            )
            self.assertEqual(calls, 2)  # called runner again!
            self.assertFalse(res3.cached)
            # Re-running force refresh does not duplicate identical snapshots
            self.assertEqual(res3.usage.total_tokens, 1670)
            self.assertEqual(res3.snapshot_count, 1)

    async def test_fetch_error_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir(parents=True, exist_ok=True)
            profile = Profile(name="failing", home=home, created_at="")
            cache_manager = CacheManager(cache_root=tmp_path / "cache")
            sem = asyncio.Semaphore(1)

            async def failing_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                return 1, "", "session authentication failed"

            res = await fetch_account_tokens_async(
                Path("/fake/agy"),
                profile,
                sem,
                cache_manager,
                runner=failing_runner,
            )
            self.assertEqual(res.status, "error")
            self.assertIn("session authentication failed", res.error or "")

            # Non-existent home
            missing_profile = Profile(name="missing", home=tmp_path / "nonexistent", created_at="")
            res_missing = await fetch_account_tokens_async(
                Path("/fake/agy"),
                missing_profile,
                sem,
                cache_manager,
            )
            self.assertEqual(res_missing.status, "error")
            self.assertIn("does not exist", res_missing.error or "")

    async def test_fetch_from_sqlite_database_and_cache(self) -> None:
        def make_varint(fn: int, val: int) -> bytes:
            tag = (fn << 3) | 0
            res = [tag]
            while val > 0x7F:
                res.append((val & 0x7F) | 0x80)
                val >>= 7
            res.append(val)
            return bytes(res)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            conv_dir = home / ".gemini" / "antigravity-cli" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            db_path = conv_dir / "test_session.db"

            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB);")
            stats = (
                make_varint(2, 5000)
                + make_varint(3, 400)
                + make_varint(5, 20000)
                + make_varint(9, 150)
                + make_varint(10, 250)
            )
            blob = bytes([(9 << 3) | 2, len(stats)]) + stats
            c.execute("INSERT INTO steps VALUES (1, 15, ?);", (blob,))
            conn.commit()
            conn.close()

            profile = Profile(name="dev", home=home, created_at="")
            cache_manager = CacheManager(cache_root=tmp_path / "cache")
            sem = asyncio.Semaphore(1)

            # 1. Live fetch scans sqlite db and caches results
            res1 = await fetch_account_tokens_async(
                Path("/fake/agy"),
                profile,
                sem,
                cache_manager,
            )
            self.assertEqual(res1.status, "success")
            self.assertFalse(res1.cached)
            self.assertEqual(res1.usage.input_tokens, 5000)
            self.assertEqual(res1.usage.output_tokens, 400)
            self.assertEqual(res1.usage.thinking_tokens, 150)
            self.assertEqual(res1.usage.cache_read_tokens, 20000)
            self.assertEqual(res1.usage.total_tokens, 5400)
            self.assertEqual(res1.snapshot_count, 1)

            # 2. Second fetch serves from cache (< 10 min)
            res2 = await fetch_account_tokens_async(
                Path("/fake/agy"),
                profile,
                sem,
                cache_manager,
                force_refresh=False,
            )
            self.assertTrue(res2.cached)
            self.assertEqual(res2.usage.total_tokens, 5400)

            # 3. Force refresh re-scans sqlite db without duplicating counts
            res3 = await fetch_account_tokens_async(
                Path("/fake/agy"),
                profile,
                sem,
                cache_manager,
                force_refresh=True,
            )
            self.assertFalse(res3.cached)
            self.assertEqual(res3.usage.total_tokens, 5400)
            self.assertEqual(res3.snapshot_count, 1)


class TerminalVisualizationTests(unittest.TestCase):
    def test_render_stacked_bar(self) -> None:
        usage = TokenUsage(
            input_tokens=500,
            output_tokens=250,
            thinking_tokens=100,
            cache_read_tokens=150,
            total_tokens=1000,
        )
        bar_colored = render_stacked_bar(usage, width=20, use_color=True)
        self.assertIn("\033[", bar_colored)
        self.assertIn("█", bar_colored)

        bar_plain = render_stacked_bar(usage, width=20, use_color=False)
        self.assertNotIn("\033[", bar_plain)
        self.assertIn("#", bar_plain)
        self.assertEqual(len(bar_plain), 22)  # [ + 20 chars + ]

        # Zero tokens bar
        empty_bar = render_stacked_bar(TokenUsage(), width=10, use_color=False)
        self.assertEqual(empty_bar, "[" + ("-" * 10) + "]")

    def test_render_comparison_bar(self) -> None:
        bar_half = render_comparison_bar(50, 100, width=10, use_color=False)
        self.assertEqual(bar_half, "[#####-----]")

        bar_zero = render_comparison_bar(0, 100, width=10, use_color=False)
        self.assertEqual(bar_zero, "[----------]")

    def test_render_summary_card_and_dashboard(self) -> None:
        u1 = AccountTokenUsage(
            account="personal",
            status="success",
            usage=TokenUsage(input_tokens=1000, output_tokens=400, total_tokens=1400),
            cached=False,
        )
        u2 = AccountTokenUsage(
            account="work",
            status="success",
            usage=TokenUsage(input_tokens=600, output_tokens=200, total_tokens=800),
            cached=True,
            age_seconds=45.0,
        )
        u3 = AccountTokenUsage(
            account="broken",
            status="error",
            error="connection refused",
        )

        card_lines = render_summary_card([u1, u2, u3], use_color=False, card_width=72)
        rendered_card = "\n".join(card_lines)
        self.assertIn("Fleet Token Summary", rendered_card)
        self.assertIn("Total Tokens:", rendered_card)
        self.assertIn("2.2k", rendered_card)  # 1400 + 800
        self.assertIn("personal", rendered_card)  # top consumer
        self.assertIn("1 cached (45s ago), 2 scanned", rendered_card)
        self.assertNotIn("live query", rendered_card)

        # Default dashboard: breakdown is hidden by default, no [Live] badge
        dashboard_lines = render_token_dashboard([u1, u2, u3], use_color=False)
        rendered_dashboard = "\n".join(dashboard_lines)
        self.assertIn("Antigravity Token Usage", rendered_dashboard)
        self.assertIn("Fleet Volume Comparison:", rendered_dashboard)
        self.assertNotIn("Token Composition Breakdown:", rendered_dashboard)
        self.assertNotIn("[Live]", rendered_dashboard)
        self.assertIn("✗ Failed: connection refused", rendered_dashboard)

        # Dashboard with breakdown=True: renders breakdown table
        breakdown_lines = render_token_dashboard([u1, u2, u3], breakdown=True, use_color=False)
        rendered_breakdown = "\n".join(breakdown_lines)
        self.assertIn("Antigravity Token Usage", rendered_breakdown)
        self.assertIn("Fleet Volume Comparison:", rendered_breakdown)
        self.assertIn("Token Composition Breakdown:", rendered_breakdown)
        self.assertIn("Profile", rendered_breakdown)
        self.assertIn("Total", rendered_breakdown)
        self.assertIn("Input", rendered_breakdown)
        self.assertIn("Output", rendered_breakdown)
        self.assertIn("Think", rendered_breakdown)
        self.assertIn("Cache", rendered_breakdown)
        self.assertIn("Hit %", rendered_breakdown)
        self.assertIn("Fleet Total", rendered_breakdown)

    def test_render_composition_breakdown_table(self) -> None:
        u1 = AccountTokenUsage(
            account="personal",
            status="success",
            usage=TokenUsage(
                input_tokens=3500,
                output_tokens=500,
                thinking_tokens=100,
                cache_read_tokens=2500,
                total_tokens=4000,
            ),
        )
        u2 = AccountTokenUsage(
            account="failing",
            status="error",
            error="disk quota exceeded",
        )
        # Plain table
        lines_plain = render_composition_breakdown_table([u1, u2], use_color=False)
        text_plain = "\n".join(lines_plain)
        self.assertIn("Token Composition Breakdown:", text_plain)
        self.assertIn("personal", text_plain)
        self.assertIn("4.0k", text_plain)
        self.assertIn("3.5k", text_plain)
        self.assertIn("500", text_plain)
        self.assertIn("100", text_plain)
        self.assertIn("2.5k", text_plain)
        self.assertIn("71.4%", text_plain)
        self.assertIn("✗ Failed: disk quota exceeded", text_plain)
        self.assertIn("Fleet Total", text_plain)

        # Colored table
        lines_colored = render_composition_breakdown_table([u1], use_color=True)
        text_colored = "\n".join(lines_colored)
        self.assertIn("\033[", text_colored)
        # Single profile: no Fleet Total row
        self.assertNotIn("Fleet Total", text_colored)


class CLITokensIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_tokens_json_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir(parents=True, exist_ok=True)
            p1 = Profile(name="p1", home=home, created_at="")
            cache_manager = CacheManager(cache_root=tmp_path / "cache")

            async def mock_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                return 0, SAMPLE_RESPONSE_WITH_TOKENS, ""

            buf = io.StringIO()
            usages = await run_tokens(
                Path("/fake/agy"),
                [p1],
                cache_manager,
                json_mode=True,
                stdout=buf,
                runner=mock_runner,
            )
            self.assertEqual(len(usages), 1)

            raw_json = buf.getvalue()
            parsed = json.loads(raw_json)
            self.assertIn("summary", parsed)
            self.assertIn("accounts", parsed)
            self.assertEqual(parsed["summary"]["total_tokens"], 1670)
            self.assertEqual(parsed["summary"]["top_consumer"], "p1")
            self.assertEqual(len(parsed["accounts"]), 1)
            self.assertEqual(parsed["accounts"][0]["account"], "p1")
            self.assertEqual(parsed["accounts"][0]["status"], "success")

    async def test_run_tokens_empty_profiles(self) -> None:
        buf = io.StringIO()
        cm = CacheManager()
        usages = await run_tokens(
            Path("/fake/agy"),
            [],
            cm,
            json_mode=False,
            stdout=buf,
        )
        self.assertEqual(usages, [])
        self.assertIn("No profiles configured", buf.getvalue())

    def test_cli_dispatch_tokens_and_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = ProfileStore(config_root=tmp_path / "config", data_root=tmp_path / "data")
            p = store.create("personal")

            with mock.patch("agym.cli.ProfileStore", return_value=store), \
                 mock.patch("agym.cli.resolve_agy", return_value=Path("/fake/agy")), \
                 mock.patch("agym.cli.run_tokens", return_value=[]) as mock_run:
                # agym tokens
                code1 = cli.main(["tokens", "--json"])
                self.assertEqual(code1, 0)
                mock_run.assert_called_once()
                self.assertTrue(mock_run.call_args.kwargs["json_mode"])
                self.assertFalse(mock_run.call_args.kwargs["breakdown"])

                # agym tokens --breakdown
                mock_run.reset_mock()
                code_b1 = cli.main(["tokens", "--breakdown"])
                self.assertEqual(code_b1, 0)
                mock_run.assert_called_once()
                self.assertTrue(mock_run.call_args.kwargs["breakdown"])

                # agym tokens -b
                mock_run.reset_mock()
                code_b2 = cli.main(["tokens", "-b"])
                self.assertEqual(code_b2, 0)
                mock_run.assert_called_once()
                self.assertTrue(mock_run.call_args.kwargs["breakdown"])

                # agym token-usage (alias)
                mock_run.reset_mock()
                code2 = cli.main(["token-usage", "--refresh"])
                self.assertEqual(code2, 0)
                mock_run.assert_called_once()
                self.assertTrue(mock_run.call_args.kwargs["refresh"])

                # agym token (alias)
                mock_run.reset_mock()
                code3 = cli.main(["token"])
                self.assertEqual(code3, 0)
                mock_run.assert_called_once()


class TokenViewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.u1 = AccountTokenUsage(
            account="alpha",
            status="success",
            usage=TokenUsage(
                input_tokens=10_000,
                output_tokens=2_000,
                thinking_tokens=1_000,
                cache_read_tokens=87_000,
                total_tokens=100_000,
            ),
        )
        self.u2 = AccountTokenUsage(
            account="beta",
            status="success",
            usage=TokenUsage(
                input_tokens=5_000,
                output_tokens=1_000,
                thinking_tokens=500,
                cache_read_tokens=43_500,
                total_tokens=50_000,
            ),
        )
        self.u3 = AccountTokenUsage(
            account="gamma",
            status="error",
            error="connection refused",
        )

    def test_render_tokens_table_view(self) -> None:
        from agym.tokens import render_tokens_table_view

        lines = render_tokens_table_view([self.u1, self.u2, self.u3], use_color=False)
        rendered = "\n".join(lines)
        self.assertIn("Fleet Token Telemetry", rendered)
        self.assertIn("alpha", rendered)
        self.assertIn("beta", rendered)
        self.assertIn("Volume Bar", rendered)
        self.assertIn("100.0k", rendered)
        self.assertIn("50.0k", rendered)
        self.assertIn("Fleet Total", rendered)
        self.assertIn("Hit %", rendered)
        self.assertIn("Sub", rendered)
        # Verify exactly 1 line for account alpha in the table rows
        alpha_rows = [l for l in lines if l.startswith("alpha")]
        self.assertEqual(len(alpha_rows), 1)
        # Volume % is profile_volume / total_accounts_volume (100k/150k = 67%)
        self.assertIn(" 67%", alpha_rows[0])

        beta_rows = [l for l in lines if l.startswith("beta")]
        self.assertEqual(len(beta_rows), 1)
        # Volume % is profile_volume / total_accounts_volume (50k/150k = 33%)
        self.assertIn(" 33%", beta_rows[0])

        # Breakdown table appended when breakdown=True
        lines_b = render_tokens_table_view([self.u1, self.u2], breakdown=True, use_color=False)
        rendered_b = "\n".join(lines_b)
        self.assertIn("Token Composition Breakdown:", rendered_b)
        self.assertIn("Input", rendered_b)
        self.assertIn("Output", rendered_b)

    def test_render_tokens_matrix_view(self) -> None:
        from agym.tokens import render_tokens_matrix_view

        lines = render_tokens_matrix_view([self.u1, self.u2, self.u3], use_color=False)
        rendered = "\n".join(lines)
        self.assertIn("Fleet Token Matrix", rendered)
        self.assertIn("alpha", rendered)
        self.assertIn("beta", rendered)

    def test_render_tokens_telemetry_view(self) -> None:
        from agym.tokens import render_tokens_telemetry_view

        lines = render_tokens_telemetry_view([self.u1, self.u2, self.u3], use_color=False)
        rendered = "\n".join(lines)
        self.assertIn("AGYM TOKEN FLEET ANALYTICS", rendered)
        self.assertIn("HEAVY DRIVERS", rendered)
        self.assertIn("Optimization Insight", rendered)

    def test_cli_tokens_view_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = ProfileStore(config_root=tmp_path / "config", data_root=tmp_path / "data")
            store.create("p1")

            with mock.patch("agym.cli.ProfileStore", return_value=store), \
                 mock.patch("agym.cli.resolve_agy", return_value=Path("/fake/agy")), \
                 mock.patch("agym.cli.run_tokens", return_value=[]) as mock_run:
                # Default view is table
                cli.main(["tokens"])
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args.kwargs["view"], "table")

                # -m sets matrix
                mock_run.reset_mock()
                cli.main(["tokens", "-m"])
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args.kwargs["view"], "matrix")

                # -t sets telemetry
                mock_run.reset_mock()
                cli.main(["tokens", "-t", "--sort", "volume"])
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args.kwargs["view"], "telemetry")
                self.assertEqual(mock_run.call_args.kwargs["sort_by"], "volume")

