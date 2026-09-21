from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agym import cli
from agym.profiles import Profile, ProfileStore
from agym.usage import (
    AccountUsage,
    ProgressiveUsageUI,
    UsageBucket,
    UsageGroup,
    account_usage_to_dict,
    fetch_account_usage_async,
    fetch_all_usage,
    format_account_usage,
    format_reset_time,
    parse_iso_datetime,
    parse_usage_response,
    run_usage,
    usage_payload_to_dict,
)

SAMPLE_REAL_RESPONSE = json.dumps(
    {
        "conversation_id": "",
        "status": "SUCCESS",
        "response": (
            "Gemini Models\tWeekly Limit Remaining\t99%\t2026-09-27T22:17:44Z\n"
            "Gemini Models\tFive Hour Limit Remaining\t96%\t2026-09-21T03:17:44Z\n"
            "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-09-27T23:00:20Z\n"
            "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-09-21T04:00:20Z\n"
        ),
        "duration_seconds": 0,
        "num_turns": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
        },
        "command": {
            "name": "usage",
            "data": {
                "description": (
                    "Within each group, models share a weekly limit and a 5-hour limit. "
                    "Quota is consumed proportionally to the cost of the tokens."
                ),
                "groups": [
                    {
                        "name": "Gemini Models",
                        "description": "Models within this group: Gemini Flash, Gemini Pro",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "name": "Weekly Limit Remaining",
                                "description": "You have used some of your weekly limit.",
                                "window": "weekly",
                                "remaining_fraction": 0.9928658604621887,
                                "reset_time": "2026-09-27T22:17:44Z",
                            },
                            {
                                "id": "gemini-5h",
                                "name": "Five Hour Limit Remaining",
                                "description": "You have used some of your 5-hour limit.",
                                "window": "5h",
                                "remaining_fraction": 0.8399444818496704,
                                "reset_time": "2026-09-21T03:17:44Z",
                            },
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
                        "buckets": [
                            {
                                "id": "3p-weekly",
                                "name": "Weekly Limit Remaining",
                                "window": "weekly",
                                "remaining_fraction": 1,
                                "reset_time": "2026-09-27T23:00:20Z",
                            },
                            {
                                "id": "3p-5h",
                                "name": "Five Hour Limit Remaining",
                                "window": "5h",
                                "remaining_fraction": 1,
                                "reset_time": "2026-09-21T04:00:20Z",
                            },
                        ],
                    },
                ],
            },
        },
    }
)


class UsageParserTests(unittest.TestCase):
    def test_parse_real_sample_response(self) -> None:
        usage = parse_usage_response(SAMPLE_REAL_RESPONSE, account="personal")
        self.assertEqual(usage.account, "personal")
        self.assertEqual(usage.status, "success")
        self.assertIsNone(usage.error)
        self.assertEqual(len(usage.groups), 2)

        g1 = usage.groups[0]
        self.assertEqual(g1.name, "Gemini Models")
        self.assertEqual(len(g1.buckets), 2)

        weekly = g1.buckets[0]
        self.assertEqual(weekly.id, "gemini-weekly")
        self.assertEqual(weekly.window, "weekly")
        self.assertEqual(weekly.percentage, 99)
        self.assertEqual(weekly.reset_time_raw, "2026-09-27T22:17:44Z")
        self.assertEqual(weekly.reset_time, datetime(2026, 9, 27, 22, 17, 44, tzinfo=timezone.utc))

        five_h = g1.buckets[1]
        self.assertEqual(five_h.id, "gemini-5h")
        self.assertEqual(five_h.window, "5h")
        self.assertEqual(five_h.percentage, 84)

        g2 = usage.groups[1]
        self.assertEqual(g2.name, "Claude and GPT models")
        self.assertEqual(len(g2.buckets), 2)
        self.assertEqual(g2.buckets[0].percentage, 100)
        self.assertEqual(g2.buckets[1].percentage, 100)

    def test_percentage_conversion(self) -> None:
        b0 = UsageBucket(id="0", name="0", window="5h", remaining_fraction=0.0, reset_time=None)
        b1 = UsageBucket(id="1", name="1", window="5h", remaining_fraction=0.8399, reset_time=None)
        b2 = UsageBucket(id="2", name="2", window="5h", remaining_fraction=1.0, reset_time=None)
        b_over = UsageBucket(id="3", name="3", window="5h", remaining_fraction=1.5, reset_time=None)
        b_neg = UsageBucket(id="4", name="4", window="5h", remaining_fraction=-0.5, reset_time=None)

        self.assertEqual(b0.percentage, 0)
        self.assertEqual(b1.percentage, 84)
        self.assertEqual(b2.percentage, 100)
        self.assertEqual(b_over.percentage, 100)
        self.assertEqual(b_neg.percentage, 0)

    def test_arbitrary_extra_groups_and_windows(self) -> None:
        payload = json.dumps(
            {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Llama Open Models",
                                "description": "Llama 3 70B",
                                "buckets": [
                                    {
                                        "id": "llama-daily",
                                        "name": "Daily Quota",
                                        "window": "daily",
                                        "remaining_fraction": 0.5,
                                        "reset_time": "2026-09-22T00:00:00Z",
                                        "extra_field": "ignored",
                                    },
                                    {
                                        "id": "llama-monthly",
                                        "name": "Monthly Quota",
                                        "window": "monthly",
                                        "remaining_fraction": 0.25,
                                    },
                                ],
                                "unknown_key": 1234,
                            }
                        ],
                        "unknown_container": True,
                    },
                },
            }
        )
        usage = parse_usage_response(payload, account="custom")
        self.assertEqual(usage.status, "success")
        self.assertEqual(len(usage.groups), 1)
        self.assertEqual(usage.groups[0].name, "Llama Open Models")
        self.assertEqual(len(usage.groups[0].buckets), 2)
        self.assertEqual(usage.groups[0].buckets[0].window, "daily")
        self.assertEqual(usage.groups[0].buckets[0].percentage, 50)
        self.assertEqual(usage.groups[0].buckets[1].window, "monthly")
        self.assertEqual(usage.groups[0].buckets[1].percentage, 25)

    def test_missing_command_groups_buckets(self) -> None:
        u1 = parse_usage_response(json.dumps({"status": "SUCCESS"}), "a1")
        self.assertEqual(u1.status, "error")
        self.assertIn("missing or invalid usage command", u1.error or "")

        u2 = parse_usage_response(
            json.dumps({"status": "SUCCESS", "command": {"name": "not_usage"}}), "a2"
        )
        self.assertEqual(u2.status, "error")
        self.assertIn("missing or invalid usage command", u2.error or "")

        u3 = parse_usage_response(
            json.dumps({"status": "SUCCESS", "command": {"name": "usage"}}), "a3"
        )
        self.assertEqual(u3.status, "error")
        self.assertIn("missing command data", u3.error or "")

        u4 = parse_usage_response(
            json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": {}}}), "a4"
        )
        self.assertEqual(u4.status, "error")
        self.assertIn("missing groups list", u4.error or "")

    def test_status_not_success(self) -> None:
        payload = json.dumps({"status": "ERROR", "error": "rate limit exceeded"})
        usage = parse_usage_response(payload, "a")
        self.assertEqual(usage.status, "error")
        self.assertIn("agy returned status: ERROR", usage.error or "")

    def test_malformed_json(self) -> None:
        u1 = parse_usage_response("not json at all", "a")
        self.assertEqual(u1.status, "error")
        self.assertIn("malformed JSON", u1.error or "")

        u2 = parse_usage_response("", "a")
        self.assertEqual(u2.status, "error")
        self.assertIn("empty response", u2.error or "")

        u3 = parse_usage_response("[]", "a")
        self.assertEqual(u3.status, "error")
        self.assertIn("not an object", u3.error or "")


class ResetTimeFormattingTests(unittest.TestCase):
    def test_iso_parsing_with_z(self) -> None:
        dt = parse_iso_datetime("2026-09-27T22:17:44Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt, datetime(2026, 9, 27, 22, 17, 44, tzinfo=timezone.utc))

        dt_offset = parse_iso_datetime("2026-09-27T22:17:44+00:00")
        self.assertEqual(dt_offset, datetime(2026, 9, 27, 22, 17, 44, tzinfo=timezone.utc))

        self.assertIsNone(parse_iso_datetime("invalid-iso-string"))
        self.assertIsNone(parse_iso_datetime(None))

    def test_formatting_future_durations(self) -> None:
        now = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)

        # 6d 22h
        dt1 = now + timedelta(days=6, hours=22, minutes=10)
        self.assertEqual(format_reset_time(dt1, now=now), "resets in 6d 22h")

        # 6d (hours == 0)
        dt2 = now + timedelta(days=6, minutes=10)
        self.assertEqual(format_reset_time(dt2, now=now), "resets in 6d")

        # 3h 15m
        dt3 = now + timedelta(hours=3, minutes=15)
        self.assertEqual(format_reset_time(dt3, now=now), "resets in 3h 15m")

        # 3h (minutes == 0)
        dt4 = now + timedelta(hours=3)
        self.assertEqual(format_reset_time(dt4, now=now), "resets in 3h")

        # 45m
        dt5 = now + timedelta(minutes=45, seconds=20)
        self.assertEqual(format_reset_time(dt5, now=now), "resets in 45m")

        # <1m
        dt6 = now + timedelta(seconds=45)
        self.assertEqual(format_reset_time(dt6, now=now), "resets in <1m")

    def test_expired_timestamps(self) -> None:
        now = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)
        past = now - timedelta(seconds=1)
        self.assertEqual(format_reset_time(past, now=now), "expired")
        self.assertEqual(format_reset_time(now, now=now), "expired")

    def test_none_or_malformed_timestamp(self) -> None:
        self.assertEqual(format_reset_time(None), "unknown")


class ConcurrencyAndProgressivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_barrier_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p1 = store.create("account-1")
            p2 = store.create("account-2")
            p3 = store.create("account-3")

            barrier = asyncio.Barrier(3)
            entered_homes: list[str] = []

            async def barrier_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                entered_homes.append(env["HOME"])
                # If tasks were run sequentially, barrier.wait() would time out because
                # subsequent tasks wouldn't enter until this task finishes.
                await asyncio.wait_for(barrier.wait(), timeout=2.0)
                return 0, SAMPLE_REAL_RESPONSE, ""

            usages = await fetch_all_usage(
                Path("/mock/agy"),
                [p1, p2, p3],
                concurrency_limit=3,
                timeout=5.0,
                runner=barrier_runner,
            )

            self.assertEqual(len(entered_homes), 3)
            self.assertEqual(len(usages), 3)
            self.assertTrue(all(u.status == "success" for u in usages))

    async def test_out_of_order_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p1 = store.create("account-1")
            p2 = store.create("account-2")
            p3 = store.create("account-3")

            arrival_order: list[str] = []

            # Delays: account-3 fastest, account-1 medium, account-2 slowest
            delays = {
                "account-1": 0.03,
                "account-2": 0.06,
                "account-3": 0.01,
            }

            async def delayed_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                account = Path(env["HOME"]).parent.name
                await asyncio.sleep(delays[account])
                return 0, SAMPLE_REAL_RESPONSE, ""

            def on_progress(u: AccountUsage) -> None:
                arrival_order.append(u.account)

            usages = await fetch_all_usage(
                Path("/mock/agy"),
                [p1, p2, p3],
                concurrency_limit=3,
                timeout=5.0,
                on_progress=on_progress,
                runner=delayed_runner,
            )

            # Arrival callback order should reflect async completion order
            self.assertEqual(arrival_order, ["account-3", "account-1", "account-2"])
            # But the returned list order must match deterministic input profile order
            self.assertEqual([u.account for u in usages], ["account-1", "account-2", "account-3"])

    async def test_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p1 = store.create("acc-success")
            p2 = store.create("acc-exit-err")
            p3 = store.create("acc-json-err")

            async def partial_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                account = Path(env["HOME"]).parent.name
                if account == "acc-success":
                    return 0, SAMPLE_REAL_RESPONSE, ""
                if account == "acc-exit-err":
                    return 1, "", "session expired"
                return 0, "bad json response", ""

            usages = await fetch_all_usage(
                Path("/mock/agy"),
                [p1, p2, p3],
                concurrency_limit=3,
                runner=partial_runner,
            )

            self.assertEqual(len(usages), 3)
            # acc-success
            self.assertEqual(usages[0].account, "acc-success")
            self.assertEqual(usages[0].status, "success")
            self.assertEqual(len(usages[0].groups), 2)

            # acc-exit-err
            self.assertEqual(usages[1].account, "acc-exit-err")
            self.assertEqual(usages[1].status, "error")
            self.assertIn("session expired", usages[1].error or "")

            # acc-json-err
            self.assertEqual(usages[2].account, "acc-json-err")
            self.assertEqual(usages[2].status, "error")
            self.assertIn("malformed JSON", usages[2].error or "")

    async def test_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p1 = store.create("acc-normal")
            p2 = store.create("acc-hanging")

            async def hanging_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                account = Path(env["HOME"]).parent.name
                if account == "acc-hanging":
                    await asyncio.sleep(timeout + 2.0)
                return 0, SAMPLE_REAL_RESPONSE, ""

            usages = await fetch_all_usage(
                Path("/mock/agy"),
                [p1, p2],
                concurrency_limit=2,
                timeout=0.1,
                runner=hanging_runner,
            )

            self.assertEqual(usages[0].status, "success")
            self.assertEqual(usages[1].status, "error")
            self.assertIn("timed out after", usages[1].error or "")

    async def test_no_profiles(self) -> None:
        buf = io.StringIO()
        res = await run_usage(Path("/mock/agy"), [], json_mode=False, stdout=buf)
        self.assertEqual(res, [])
        self.assertIn("No profiles configured. Run 'agym setup <profile>' first.", buf.getvalue())

        buf_json = io.StringIO()
        res_json = await run_usage(Path("/mock/agy"), [], json_mode=True, stdout=buf_json)
        self.assertEqual(res_json, [])
        parsed = json.loads(buf_json.getvalue())
        self.assertEqual(parsed, {"accounts": []})


class OutputRenderingTests(unittest.TestCase):
    def test_json_output_mode(self) -> None:
        u_success = parse_usage_response(SAMPLE_REAL_RESPONSE, "personal")
        u_error = AccountUsage(
            account="work",
            status="error",
            error="profile home directory does not exist: /some/path",
        )

        payload = usage_payload_to_dict([u_success, u_error])
        raw_json = json.dumps(payload, indent=2)
        parsed = json.loads(raw_json)

        self.assertIn("accounts", parsed)
        self.assertEqual(len(parsed["accounts"]), 2)

        p1 = parsed["accounts"][0]
        self.assertEqual(p1["account"], "personal")
        self.assertEqual(p1["status"], "success")
        self.assertEqual(len(p1["groups"]), 2)
        self.assertEqual(p1["groups"][0]["name"], "Gemini Models")
        b0 = p1["groups"][0]["buckets"][0]
        self.assertIn("percentage", b0)
        self.assertIn("remaining_fraction", b0)
        self.assertIn("reset_time", b0)

        p2 = parsed["accounts"][1]
        self.assertEqual(p2["account"], "work")
        self.assertEqual(p2["status"], "error")
        self.assertIn("profile home directory does not exist", p2["error"])

    def test_non_tty_rendering(self) -> None:
        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "personal")
        u2 = AccountUsage(account="work", status="error", error="agy exited with status 1 (session expired)")

        p1 = Profile(name="personal", home=Path("/h1"), created_at="")
        p2 = Profile(name="work", home=Path("/h2"), created_at="")

        buf = io.StringIO()
        ui = ProgressiveUsageUI([p1, p2], is_tty=False, stdout=buf)

        ui.on_progress(u1)
        ui.on_progress(u2)
        ui.finish([u1, u2])

        output = buf.getvalue()
        # Non-TTY should NOT contain ANSI escapes
        self.assertNotIn("\033[", output)
        self.assertNotIn("\033[?25l", output)

        # Progress updates
        self.assertIn("[1/2] personal: completed", output)
        self.assertIn("[2/2] work: failed (session expired)", output)

        # Final formatted report table
        self.assertIn("Antigravity Usage", output)
        self.assertIn("Gemini", output)
        self.assertIn("Claude & GPT", output)
        self.assertIn("personal", output)
        self.assertIn("5h: [████████░░]  84%", output)
        self.assertIn("Wk: [██████████]  99%", output)
        self.assertIn("work", output)
        self.assertIn("Failed: agy exited with status 1 (session expired)", output)

    def test_tty_rendering_produces_ansi_and_restores_cursor(self) -> None:
        p1 = Profile(name="personal", home=Path("/h1"), created_at="")
        buf = io.StringIO()
        ui = ProgressiveUsageUI([p1], is_tty=True, stdout=buf)

        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "personal")
        ui.render_tty("⠋")
        ui.on_progress(u1)
        ui.finish([u1])

        output = buf.getvalue()
        # TTY uses ANSI cursor positioning and clear codes
        self.assertIn("\033[2K", output)
        self.assertIn("\033[?25h", output)
        self.assertIn("personal", output)
        self.assertIn("Gemini", output)

    def test_progressive_ui_safe_write_handles_encoding_error(self) -> None:
        p1 = Profile(name="personal", home=Path("/h1"), created_at="")

        class StrictAsciiWriter:
            def __init__(self) -> None:
                self.encoding = "ascii"
                self.written: list[str] = []

            def write(self, s: str) -> None:
                # Raise UnicodeEncodeError if non-ascii chars passed
                s.encode("ascii")
                self.written.append(s)

            def flush(self) -> None:
                pass

        writer = StrictAsciiWriter()
        ui = ProgressiveUsageUI([p1], is_tty=False, stdout=writer)
        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "personal")
        # Should not raise UnicodeEncodeError even when writing Unicode box characters
        ui.on_progress(u1)
        ui.finish([u1])
        self.assertTrue(len(writer.written) > 0)


class TableAndBarStylingTests(unittest.TestCase):
    def test_short_reset_time_abbreviations(self) -> None:
        now = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)

        # 6d 21h -> 6d
        res_6d_plus = format_reset_time(now + timedelta(days=6, hours=21), now=now, short=True)
        self.assertEqual(res_6d_plus, "6d")
        self.assertLessEqual(len(res_6d_plus), 4)

        # 6d 0h -> 6d
        res_6d = format_reset_time(now + timedelta(days=6), now=now, short=True)
        self.assertEqual(res_6d, "6d")
        self.assertLessEqual(len(res_6d), 4)

        # >= 3h: 4h 17m -> 4h
        res_4h_plus = format_reset_time(now + timedelta(hours=4, minutes=17), now=now, short=True)
        self.assertEqual(res_4h_plus, "4h")
        self.assertLessEqual(len(res_4h_plus), 4)

        # >= 3h: 3h 0m -> 3h
        res_3h = format_reset_time(now + timedelta(hours=3), now=now, short=True)
        self.assertEqual(res_3h, "3h")
        self.assertLessEqual(len(res_3h), 4)

        # < 3h: shorter format (2h 59m -> 2h)
        res_2h59m = format_reset_time(now + timedelta(hours=2, minutes=59), now=now, short=True)
        self.assertEqual(res_2h59m, "2h")
        self.assertLessEqual(len(res_2h59m), 4)

        # < 3h: shorter format (1h 15m -> 1h)
        res_1h15m = format_reset_time(now + timedelta(hours=1, minutes=15), now=now, short=True)
        self.assertEqual(res_1h15m, "1h")
        self.assertLessEqual(len(res_1h15m), 4)

        # < 3h: shorter format (1h 5m -> 1h)
        res_1h05m = format_reset_time(now + timedelta(hours=1, minutes=5), now=now, short=True)
        self.assertEqual(res_1h05m, "1h")
        self.assertLessEqual(len(res_1h05m), 4)

        # less than 1 hour: exact minutes (45m)
        res_45m = format_reset_time(now + timedelta(minutes=45, seconds=30), now=now, short=True)
        self.assertEqual(res_45m, "45m")
        self.assertLessEqual(len(res_45m), 4)

        # less than 1 hour: exact minutes (9m)
        res_9m = format_reset_time(now + timedelta(minutes=9), now=now, short=True)
        self.assertEqual(res_9m, "9m")
        self.assertLessEqual(len(res_9m), 4)

        # less than 1 minute: <1m
        res_lt1m = format_reset_time(now + timedelta(seconds=25), now=now, short=True)
        self.assertEqual(res_lt1m, "<1m")
        self.assertLessEqual(len(res_lt1m), 4)

        # expired / past: now
        res_past = format_reset_time(now - timedelta(seconds=10), now=now, short=True)
        self.assertEqual(res_past, "now")
        self.assertLessEqual(len(res_past), 4)

        # None: -
        res_none = format_reset_time(None, short=True)
        self.assertEqual(res_none, "-")
        self.assertLessEqual(len(res_none), 4)

    def test_at_least_6_color_ranks(self) -> None:
        from agym.usage import COLOR_RANKS, get_color_for_percentage

        self.assertGreaterEqual(len(COLOR_RANKS), 6)

        c1 = get_color_for_percentage(95.0)  # Rank 1: >= 90
        c2 = get_color_for_percentage(80.0)  # Rank 2: 75-89.9
        c3 = get_color_for_percentage(65.0)  # Rank 3: 50-74.9
        c4 = get_color_for_percentage(35.0)  # Rank 4: 25-49.9
        c5 = get_color_for_percentage(15.0)  # Rank 5: 10-24.9
        c6 = get_color_for_percentage(5.0)   # Rank 6: < 10

        colors = [c1, c2, c3, c4, c5, c6]
        self.assertEqual(len(set(colors)), 6, "Expected 6 unique color ranks")

    def test_colored_and_plain_bar_formatting(self) -> None:
        from agym.usage import format_colored_bar

        # Plain bar for 77.33% (fraction 0.7733, width=10 -> 8 filled, 2 empty)
        plain = format_colored_bar(0.7733, width=10, use_color=False)
        self.assertEqual(plain, "[████████░░]")

        # 100%
        full = format_colored_bar(1.0, width=10, use_color=False)
        self.assertEqual(full, "[██████████]")

        # 0%
        empty = format_colored_bar(0.0, width=10, use_color=False)
        self.assertEqual(empty, "[░░░░░░░░░░]")

        # Colored bar includes ANSI escape sequences
        colored = format_colored_bar(0.7733, width=10, use_color=True)
        self.assertIn("\033[", colored)
        self.assertIn("████████", colored)
        self.assertIn("░░", colored)


class CliUsageTests(unittest.TestCase):
    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_usage", new_callable=mock.AsyncMock)
    def test_cli_usage_invokes_run_usage(
        self,
        mock_run_usage: mock.AsyncMock,
        mock_resolve: mock.Mock,
        Store: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p1 = store.create("personal")
            p2 = store.create("work")
            Store.return_value = store
            mock_resolve.return_value = Path("/real/agy")

            code = cli.main(["usage", "--json", "--timeout", "15"])
            self.assertEqual(code, 0)
            mock_resolve.assert_called_once_with()
            mock_run_usage.assert_called_once_with(
                Path("/real/agy"),
                [p1, p2],
                json_mode=True,
                timeout=15.0,
            )

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_usage", new_callable=mock.AsyncMock)
    def test_cli_usage_filter_profiles(
        self,
        mock_run_usage: mock.AsyncMock,
        mock_resolve: mock.Mock,
        Store: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            p1 = store.create("personal")
            store.create("work")
            Store.return_value = store
            mock_resolve.return_value = Path("/real/agy")

            code = cli.main(["usage", "personal"])
            self.assertEqual(code, 0)
            mock_run_usage.assert_called_once_with(
                Path("/real/agy"),
                [p1],
                json_mode=False,
                timeout=30.0,
            )

    @mock.patch("agym.cli.ProfileStore")
    def test_cli_usage_nonexistent_profile(self, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            Store.return_value = store

            code = cli.main(["usage", "nonexistent"])
            self.assertEqual(code, 2)


class SubscriptionUsageIntegrationTests(unittest.TestCase):
    def test_table_rendering_with_subscription_column(self) -> None:
        from agym.usage import render_usage_table_lines

        p1 = Profile(name="p-safe", home=Path("/h1"), created_at="", subscription_date="2027-03-21")
        p2 = Profile(name="p-exp", home=Path("/h2"), created_at="", subscription_date="2026-09-09")
        p3 = Profile(name="p-unk", home=Path("/h3"), created_at="", subscription_date=None)

        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "p-safe", subscription_date="2027-03-21")
        u2 = AccountUsage(
            account="p-exp",
            status="error",
            error="session expired",
            subscription_date="2026-09-09",
        )
        u3 = parse_usage_response(SAMPLE_REAL_RESPONSE, "p-unk", subscription_date=None)

        completed = {"p-safe": u1, "p-exp": u2, "p-unk": u3}
        lines = render_usage_table_lines([p1, p2, p3], completed, use_color=False)
        rendered = "\n".join(lines)

        # Header has Subscription
        self.assertIn("Subscription", rendered)
        # Safe profile has bar and renews date
        self.assertIn("[██████████]", rendered)
        self.assertIn("Renews: 21/03/2027", rendered)
        # Expired profile shows failed quota error AND subscription status
        self.assertIn("Failed: session expired", rendered)
        self.assertIn("Expired: 09/09/2026", rendered)
        # Unknown profile shows neutral unknown
        self.assertIn("(date not set)", rendered)

    def test_json_payload_includes_subscription(self) -> None:
        u_sub = parse_usage_response(SAMPLE_REAL_RESPONSE, "p-sub", subscription_date="2027-03-21")
        u_none = parse_usage_response(SAMPLE_REAL_RESPONSE, "p-none", subscription_date=None)

        payload = usage_payload_to_dict([u_sub, u_none])
        acc1 = payload["accounts"][0]
        self.assertEqual(acc1["account"], "p-sub")
        self.assertIn("subscription", acc1)
        self.assertEqual(acc1["subscription"]["date"], "2027-03-21")
        self.assertIsNotNone(acc1["subscription"]["days_remaining"])
        self.assertIn("remaining", acc1["subscription"]["human_remaining"])
        self.assertIsNotNone(acc1["subscription"]["rank"])

        acc2 = payload["accounts"][1]
        self.assertEqual(acc2["account"], "p-none")
        self.assertIn("subscription", acc2)
        self.assertIsNone(acc2["subscription"]["date"])
        self.assertIsNone(acc2["subscription"]["days_remaining"])
        self.assertEqual(acc2["subscription"]["status"], "unknown")


class CacheUsageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_caching_and_force_refresh(self) -> None:
        from agym.cache import CacheManager

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir(parents=True, exist_ok=True)
            p = Profile(name="personal", home=home, created_at="")
            cache_mgr = CacheManager(cache_root=tmp_path / "cache")
            sem = asyncio.Semaphore(1)

            call_count = 0

            async def mock_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
                nonlocal call_count
                call_count += 1
                return 0, SAMPLE_REAL_RESPONSE, ""

            # 1. First fetch: live execution, populates cache
            u1 = await fetch_account_usage_async(
                Path("/fake/agy"),
                p,
                sem,
                cache_manager=cache_mgr,
                runner=mock_runner,
            )
            self.assertEqual(call_count, 1)
            self.assertFalse(u1.cached)
            self.assertEqual(u1.status, "success")

            # 2. Second fetch within 60s: served from cache, runner NOT called
            u2 = await fetch_account_usage_async(
                Path("/fake/agy"),
                p,
                sem,
                force_refresh=False,
                cache_manager=cache_mgr,
                runner=mock_runner,
            )
            self.assertEqual(call_count, 1)
            self.assertTrue(u2.cached)
            self.assertGreaterEqual(u2.age_seconds, 0.0)

            # 3. Third fetch with force_refresh=True: runner called again
            u3 = await fetch_account_usage_async(
                Path("/fake/agy"),
                p,
                sem,
                force_refresh=True,
                cache_manager=cache_mgr,
                runner=mock_runner,
            )
            self.assertEqual(call_count, 2)
            self.assertFalse(u3.cached)

    def test_json_payload_includes_cache_freshness(self) -> None:
        u_cached = parse_usage_response(
            SAMPLE_REAL_RESPONSE,
            "p-cached",
            cached=True,
            age_seconds=42.5,
            cached_at="2026-09-21T02:00:00Z",
        )
        u_live = parse_usage_response(
            SAMPLE_REAL_RESPONSE,
            "p-live",
            cached=False,
            age_seconds=0.0,
        )
        payload = usage_payload_to_dict([u_cached, u_live])
        a1 = payload["accounts"][0]
        self.assertEqual(a1["account"], "p-cached")
        self.assertTrue(a1["cached"])
        self.assertEqual(a1["age_seconds"], 42.5)
        self.assertEqual(a1["cached_at"], "2026-09-21T02:00:00Z")

        a2 = payload["accounts"][1]
        self.assertEqual(a2["account"], "p-live")
        self.assertFalse(a2["cached"])
        self.assertEqual(a2["age_seconds"], 0.0)

    def test_table_rendering_no_cached_part(self) -> None:
        from agym.usage import render_usage_table_lines

        p1 = Profile(name="p-cached", home=Path("/h1"), created_at="")
        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "p-cached", cached=True, age_seconds=42.0)
        lines = render_usage_table_lines([p1], {"p-cached": u1}, use_color=False)
        rendered = "\n".join(lines)
        self.assertNotIn("42s ago", rendered)
        self.assertNotIn("Cached", rendered)

    def test_cli_usage_refresh_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = ProfileStore(config_root=tmp_path / "config", data_root=tmp_path / "data")
            store.create("personal")

            with mock.patch("agym.cli.ProfileStore", return_value=store), \
                 mock.patch("agym.cli.resolve_agy", return_value=Path("/fake/agy")), \
                 mock.patch("agym.cli.run_usage", return_value=[]) as mock_run:
                code = cli.main(["usage", "-f"])
                self.assertEqual(code, 0)
                mock_run.assert_called_once()
                self.assertTrue(mock_run.call_args.kwargs["refresh"])


class UsageGraphsTests(unittest.TestCase):
    def test_smooth_fractional_bar_formatting(self) -> None:
        from agym.usage_graphs import format_smooth_bar

        # 0% empty
        self.assertEqual(format_smooth_bar(0.0, 10, use_color=False), "[░░░░░░░░░░]")
        # 100% full
        self.assertEqual(format_smooth_bar(1.0, 10, use_color=False), "[██████████]")
        # 50% half
        self.assertEqual(format_smooth_bar(0.5, 10, use_color=False), "[█████░░░░░]")
        # 5% should show a fractional block (▌), not empty!
        self.assertEqual(format_smooth_bar(0.05, 10, use_color=False), "[▌░░░░░░░░░]")
        # 84% should show 8 full blocks + fractional ▍ + empty
        self.assertEqual(format_smooth_bar(0.84, 10, use_color=False), "[████████▍░]")

    def test_sparkline_and_micro_bar(self) -> None:
        from agym.usage_graphs import format_micro_bar, format_sparkline_glyph

        # Sparkline glyphs
        self.assertEqual(format_sparkline_glyph(0.0, use_color=False), " ")
        self.assertEqual(format_sparkline_glyph(1.0, use_color=False), "█")

        # Micro bar
        self.assertEqual(format_micro_bar(1.0, 5, use_color=False), "▰▰▰▰▰")
        self.assertEqual(format_micro_bar(0.0, 5, use_color=False), "▱▱▱▱▱")
        self.assertEqual(format_micro_bar(0.6, 5, use_color=False), "▰▰▰▱▱")

    def test_compute_fleet_telemetry(self) -> None:
        from agym.usage import extract_quota_bucket
        from agym.usage_graphs import compute_fleet_telemetry

        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "acc1")
        u2 = parse_usage_response(SAMPLE_REAL_RESPONSE, "acc2")
        u3 = AccountUsage(account="acc3", status="error", error="session expired")

        telemetry = compute_fleet_telemetry({"acc1": u1, "acc2": u2, "acc3": u3}, extract_quota_bucket)
        self.assertEqual(telemetry.total_accounts, 3)
        self.assertGreater(telemetry.gemini_avg_pct, 50.0)
        self.assertEqual(telemetry.claude_avg_pct, 100.0)
        self.assertEqual(telemetry.ready_count, 2)
        self.assertEqual(telemetry.depleted_count, 1)

    def test_render_fleet_summary_banner(self) -> None:
        from agym.usage import extract_quota_bucket
        from agym.usage_graphs import compute_fleet_telemetry, render_fleet_summary_banner

        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "acc1")
        telemetry = compute_fleet_telemetry({"acc1": u1}, extract_quota_bucket)
        lines = render_fleet_summary_banner(telemetry, width=80, use_color=False)
        self.assertGreater(len(lines), 2)
        banner_text = "\n".join(lines)
        self.assertIn("Fleet Capacity", banner_text)
        self.assertIn("Gemini Pool", banner_text)
        self.assertIn("Claude Pool", banner_text)

    def test_render_views(self) -> None:
        from agym.usage import render_usage_view_lines

        p1 = Profile(name="alpha", home=Path("/h1"), created_at="")
        p2 = Profile(name="beta", home=Path("/h2"), created_at="")
        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "alpha")
        u2 = parse_usage_response(SAMPLE_REAL_RESPONSE, "beta")
        completed = {"alpha": u1, "beta": u2}

        # 1. Grid view (Option 2)
        grid_lines = render_usage_view_lines(
            [p1, p2], completed, view="grid", use_color=False, term_width=100
        )
        grid_text = "\n".join(grid_lines)
        self.assertIn("alpha", grid_text)
        self.assertIn("beta", grid_text)
        self.assertIn("Gemini", grid_text)
        self.assertIn("Claude & GPT", grid_text)
        self.assertIn("Sub", grid_text)

        # 2. Matrix view (Option 2 - ultra dense)
        matrix_lines = render_usage_view_lines(
            [p1, p2], completed, view="matrix", use_color=False, term_width=100
        )
        matrix_text = "\n".join(matrix_lines)
        self.assertIn("Fleet Heatmap Matrix", matrix_text)
        self.assertIn("alpha", matrix_text)
        self.assertIn("Legend:", matrix_text)

        # 3. Telemetry view (Option 3 - executive tiers)
        tele_lines = render_usage_view_lines(
            [p1, p2], completed, view="telemetry", use_color=False, term_width=100
        )
        tele_text = "\n".join(tele_lines)
        self.assertIn("READY TO USE", tele_text)
        self.assertIn("Recommendation:", tele_text)

        # 4. Table view (Option 1)
        table_lines = render_usage_view_lines(
            [p1, p2], completed, view="table", use_color=False, term_width=100
        )
        table_text = "\n".join(table_lines)
        self.assertIn("Account", table_text)
        self.assertIn("Gemini", table_text)

    def test_profile_sorting(self) -> None:
        from agym.usage import sort_profiles

        p1 = Profile(name="zebra", home=Path("/h1"), created_at="")
        p2 = Profile(name="alpha", home=Path("/h2"), created_at="")
        u1 = parse_usage_response(SAMPLE_REAL_RESPONSE, "zebra")
        u2 = parse_usage_response(SAMPLE_REAL_RESPONSE, "alpha")
        completed = {"zebra": u1, "alpha": u2}

        sorted_by_name = sort_profiles([p1, p2], completed, sort_by="name")
        self.assertEqual([p.name for p in sorted_by_name], ["alpha", "zebra"])

        # Test sub sorting
        p_exp = Profile(name="exp_prof", home=Path("/h3"), created_at="", subscription_date="2026-09-25")
        p_safe = Profile(name="safe_prof", home=Path("/h4"), created_at="", subscription_date="2028-01-01")
        p_none = Profile(name="none_prof", home=Path("/h5"), created_at="", subscription_date=None)
        sorted_by_sub = sort_profiles([p_none, p_safe, p_exp], {}, sort_by="sub")
        self.assertEqual([p.name for p in sorted_by_sub], ["exp_prof", "safe_prof", "none_prof"])

    def test_format_compact_sub(self) -> None:
        from datetime import date
        from agym.subscription import calculate_subscription_health, format_compact_sub, format_colored_compact_sub

        now = date(2026, 9, 21)
        h_unknown = calculate_subscription_health(None, now=now)
        self.assertEqual(format_compact_sub(h_unknown, now=now), "-")

        h_exp = calculate_subscription_health("2020-01-01", now=now)
        self.assertEqual(format_compact_sub(h_exp, now=now), "exp")

        h_1d = calculate_subscription_health("2026-09-22", now=now)
        self.assertEqual(format_compact_sub(h_1d, now=now), "1d")

        h_14d = calculate_subscription_health("2026-10-05", now=now)
        self.assertEqual(format_compact_sub(h_14d, now=now), "14d")

        h_1mo = calculate_subscription_health("2026-10-25", now=now)
        self.assertEqual(format_compact_sub(h_1mo, now=now), "1mo")

        h_6mo = calculate_subscription_health("2027-03-21", now=now)
        self.assertEqual(format_compact_sub(h_6mo, now=now), "6mo")

        h_18m = calculate_subscription_health("2028-03-21", now=now)
        self.assertEqual(format_compact_sub(h_18m, now=now), "18m")

        # Verify <= 3 chars for all
        for h in (h_unknown, h_exp, h_1d, h_14d, h_1mo, h_6mo, h_18m):
            sub_str = format_compact_sub(h, now=now)
            self.assertLessEqual(len(sub_str), 3)

        # Colored version
        colored = format_colored_compact_sub(h_18m, use_color=True, now=now)
        self.assertIn("18m", colored)
        self.assertIn("\033[", colored)

    def test_cli_view_and_sort_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = ProfileStore(config_root=tmp_path / "config", data_root=tmp_path / "data")
            store.create("p1")

            with mock.patch("agym.cli.ProfileStore", return_value=store), \
                 mock.patch("agym.cli.resolve_agy", return_value=Path("/fake/agy")), \
                 mock.patch("agym.cli.run_usage", return_value=[]) as mock_run:
                code = cli.main(["usage", "--grid", "--sort", "quota", "--no-summary"])
                self.assertEqual(code, 0)
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args.kwargs["view"], "grid")
                self.assertEqual(mock_run.call_args.kwargs["sort_by"], "quota")
                self.assertFalse(mock_run.call_args.kwargs["include_summary"])

            with mock.patch("agym.cli.ProfileStore", return_value=store), \
                 mock.patch("agym.cli.resolve_agy", return_value=Path("/fake/agy")), \
                 mock.patch("agym.cli.run_usage", return_value=[]) as mock_run:
                code = cli.main(["usage", "-t"])
                self.assertEqual(code, 0)
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args.kwargs["view"], "telemetry")

            with mock.patch("agym.cli.ProfileStore", return_value=store), \
                 mock.patch("agym.cli.resolve_agy", return_value=Path("/fake/agy")), \
                 mock.patch("agym.cli.run_usage", return_value=[]) as mock_run:
                code = cli.main(["usage", "-m"])
                self.assertEqual(code, 0)
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args.kwargs["view"], "matrix")


