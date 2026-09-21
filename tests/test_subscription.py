from __future__ import annotations

from datetime import date
import unittest

from agym.subscription import (
    COLOR_BRIGHT_GREEN,
    COLOR_DIM,
    COLOR_GREEN,
    COLOR_ORANGE,
    COLOR_RED,
    COLOR_YELLOW,
    SubscriptionError,
    add_months,
    calculate_subscription_health,
    diff_months_days,
    format_iso_date,
    format_subscription_cells,
    format_user_date,
    parse_subscription_date,
    prompt_subscription_date,
)


class SubscriptionParsingTests(unittest.TestCase):
    def test_parse_valid_dates(self) -> None:
        self.assertEqual(parse_subscription_date("14/03/2027"), date(2027, 3, 14))
        self.assertEqual(parse_subscription_date("14-03-2027"), date(2027, 3, 14))
        self.assertEqual(parse_subscription_date("2027-03-14"), date(2027, 3, 14))
        self.assertEqual(parse_subscription_date("2027/03/14"), date(2027, 3, 14))
        self.assertEqual(parse_subscription_date("  14/03/2027  "), date(2027, 3, 14))

    def test_parse_none_or_empty(self) -> None:
        self.assertIsNone(parse_subscription_date(None))
        self.assertIsNone(parse_subscription_date(""))
        self.assertIsNone(parse_subscription_date("   "))

    def test_parse_invalid_formats(self) -> None:
        for bad in ["not a date", "14/03", "14/03/27", "2027-3", "hello", "14-Mar-2027"]:
            with self.subTest(bad=bad), self.assertRaises(SubscriptionError):
                parse_subscription_date(bad)

    def test_parse_impossible_calendar_dates(self) -> None:
        for bad in ["31/02/2027", "29/02/2025", "31/04/2027", "32/01/2027", "00/01/2027"]:
            with self.subTest(bad=bad), self.assertRaises(SubscriptionError):
                parse_subscription_date(bad)

    def test_parse_leap_years(self) -> None:
        self.assertEqual(parse_subscription_date("29/02/2024"), date(2024, 2, 29))
        self.assertEqual(parse_subscription_date("29/02/2028"), date(2028, 2, 29))

    def test_format_conversions(self) -> None:
        self.assertEqual(format_user_date(date(2027, 3, 14)), "14/03/2027")
        self.assertEqual(format_user_date("2027-03-14"), "14/03/2027")
        self.assertEqual(format_user_date(None), "unknown")

        self.assertEqual(format_iso_date(date(2027, 3, 14)), "2027-03-14")
        self.assertEqual(format_iso_date("14/03/2027"), "2027-03-14")
        self.assertIsNone(format_iso_date(None))


class CalendarMathTests(unittest.TestCase):
    def test_add_months_regular(self) -> None:
        d = date(2026, 9, 21)
        self.assertEqual(add_months(d, 1), date(2026, 10, 21))
        self.assertEqual(add_months(d, 3), date(2026, 12, 21))
        self.assertEqual(add_months(d, 6), date(2027, 3, 21))
        self.assertEqual(add_months(d, 12), date(2027, 9, 21))

    def test_add_months_clamping_month_end(self) -> None:
        # Jan 31 + 1 month -> Feb 28 (in non-leap year)
        self.assertEqual(add_months(date(2027, 1, 31), 1), date(2027, 2, 28))
        # Jan 31 + 1 month -> Feb 29 (in leap year)
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        # Aug 31 + 1 month -> Sep 30
        self.assertEqual(add_months(date(2026, 8, 31), 1), date(2026, 9, 30))

    def test_diff_months_days(self) -> None:
        start = date(2026, 9, 21)
        self.assertEqual(diff_months_days(start, date(2026, 9, 21)), (0, 0))
        self.assertEqual(diff_months_days(start, date(2026, 10, 21)), (1, 0))
        self.assertEqual(diff_months_days(start, date(2026, 10, 26)), (1, 5))
        self.assertEqual(diff_months_days(start, date(2027, 3, 21)), (6, 0))
        self.assertEqual(diff_months_days(start, date(2027, 4, 2)), (6, 12))


class HealthCalculationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = date(2026, 9, 21)

    def test_unknown_subscription(self) -> None:
        h = calculate_subscription_health(None, now=self.now)
        self.assertIsNone(h.date)
        self.assertIsNone(h.days_remaining)
        self.assertEqual(h.status, "unknown")
        self.assertIsNone(h.rank)
        self.assertEqual(h.human_remaining, "unknown")
        self.assertEqual(h.color_code, COLOR_DIM)
        self.assertEqual(h.bar_blocks, 0)

    def test_rank_5_very_safe(self) -> None:
        # Exactly 6 months
        h1 = calculate_subscription_health("2027-03-21", now=self.now)
        self.assertEqual(h1.rank, 5)
        self.assertEqual(h1.status, "very_safe")
        self.assertEqual(h1.bar_blocks, 10)
        self.assertEqual(h1.color_code, COLOR_BRIGHT_GREEN)
        self.assertEqual(h1.human_remaining, "6mo remaining")

        # 8 months 12 days
        h2 = calculate_subscription_health("2027-06-02", now=self.now)
        self.assertEqual(h2.rank, 5)
        self.assertEqual(h2.status, "very_safe")
        self.assertEqual(h2.bar_blocks, 10)
        self.assertEqual(h2.human_remaining, "8mo 12d remaining")

    def test_rank_4_safe(self) -> None:
        # Exactly 3 months
        h1 = calculate_subscription_health("2026-12-21", now=self.now)
        self.assertEqual(h1.rank, 4)
        self.assertEqual(h1.status, "safe")
        self.assertEqual(h1.bar_blocks, 8)
        self.assertEqual(h1.color_code, COLOR_GREEN)
        self.assertEqual(h1.human_remaining, "3mo remaining")

        # 4 months 10 days
        h2 = calculate_subscription_health("2027-01-31", now=self.now)
        self.assertEqual(h2.rank, 4)
        self.assertEqual(h2.bar_blocks, 8)
        self.assertEqual(h2.human_remaining, "4mo 10d remaining")

    def test_rank_3_moderate(self) -> None:
        # Exactly 1 month
        h1 = calculate_subscription_health("2026-10-21", now=self.now)
        self.assertEqual(h1.rank, 3)
        self.assertEqual(h1.status, "moderate")
        self.assertEqual(h1.bar_blocks, 6)
        self.assertEqual(h1.color_code, COLOR_YELLOW)
        self.assertEqual(h1.human_remaining, "1mo remaining")

        # 2 months 15 days
        h2 = calculate_subscription_health("2026-12-06", now=self.now)
        self.assertEqual(h2.rank, 3)
        self.assertEqual(h2.bar_blocks, 6)
        self.assertEqual(h2.human_remaining, "2mo 15d remaining")

    def test_rank_2_warning(self) -> None:
        # 27 days
        target = date(2026, 10, 18)
        h1 = calculate_subscription_health(target, now=self.now)
        self.assertEqual(h1.rank, 2)
        self.assertEqual(h1.status, "warning")
        self.assertEqual(h1.bar_blocks, 4)
        self.assertEqual(h1.color_code, COLOR_ORANGE)
        self.assertEqual(h1.human_remaining, "27d remaining")

        # 8 days (boundary for rank 2)
        h2 = calculate_subscription_health(date(2026, 9, 29), now=self.now)
        self.assertEqual(h2.rank, 2)
        self.assertEqual(h2.bar_blocks, 4)
        self.assertEqual(h2.human_remaining, "8d remaining")

    def test_rank_1_critical(self) -> None:
        # 7 days (boundary for critical)
        h1 = calculate_subscription_health(date(2026, 9, 28), now=self.now)
        self.assertEqual(h1.rank, 1)
        self.assertEqual(h1.status, "critical")
        self.assertEqual(h1.bar_blocks, 2)
        self.assertEqual(h1.color_code, COLOR_RED)
        self.assertEqual(h1.human_remaining, "7d remaining")

        # 1 day
        h2 = calculate_subscription_health(date(2026, 9, 22), now=self.now)
        self.assertEqual(h2.rank, 1)
        self.assertEqual(h2.bar_blocks, 2)
        self.assertEqual(h2.human_remaining, "1d remaining")

    def test_expires_today(self) -> None:
        h = calculate_subscription_health(date(2026, 9, 21), now=self.now)
        self.assertEqual(h.rank, 1)
        self.assertEqual(h.status, "critical")
        self.assertEqual(h.bar_blocks, 1)
        self.assertEqual(h.color_code, COLOR_RED)
        self.assertEqual(h.human_remaining, "expires today")

    def test_expired_in_past(self) -> None:
        # Expired 12 days ago
        h = calculate_subscription_health(date(2026, 9, 9), now=self.now)
        self.assertEqual(h.rank, 1)
        self.assertEqual(h.status, "expired")
        self.assertEqual(h.bar_blocks, 0)
        self.assertEqual(h.color_code, COLOR_RED)
        self.assertEqual(h.human_remaining, "expired 12d ago")


class CellFormattingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = date(2026, 9, 21)

    def test_plain_cell_formatting(self) -> None:
        h_safe = calculate_subscription_health(date(2027, 3, 21), now=self.now)
        r1, r2 = format_subscription_cells(h_safe, width=27, use_color=False, now=self.now)
        self.assertIn("[██████████]", r1)
        self.assertIn("6mo", r1)
        self.assertEqual(r2.strip(), "Renews: 21/03/2027")

        h_unk = calculate_subscription_health(None, now=self.now)
        r1_u, r2_u = format_subscription_cells(h_unk, width=27, use_color=False, now=self.now)
        self.assertIn("[░░░░░░░░░░]", r1_u)
        self.assertIn("unknown", r1_u)
        self.assertEqual(r2_u.strip(), "(date not set)")

        h_exp = calculate_subscription_health(date(2026, 9, 9), now=self.now)
        r1_e, r2_e = format_subscription_cells(h_exp, width=27, use_color=False, now=self.now)
        self.assertIn("[░░░░░░░░░░]", r1_e)
        self.assertIn("expired 12d", r1_e)
        self.assertEqual(r2_e.strip(), "Expired: 09/09/2026")


class InteractivePromptTests(unittest.TestCase):
    def test_prompt_new_enter_skips(self) -> None:
        res = prompt_subscription_date(existing=None, input_fn=lambda _: "")
        self.assertIsNone(res)

    def test_prompt_new_valid_date(self) -> None:
        res = prompt_subscription_date(existing=None, input_fn=lambda _: "14/03/2027")
        self.assertEqual(res, "2027-03-14")

    def test_prompt_existing_enter_keeps(self) -> None:
        res = prompt_subscription_date(existing="2027-03-14", input_fn=lambda _: "")
        self.assertEqual(res, "2027-03-14")

    def test_prompt_existing_clear(self) -> None:
        for clr in ["clear", "none", "remove", "-"]:
            with self.subTest(clr=clr):
                res = prompt_subscription_date(existing="2027-03-14", input_fn=lambda _, c=clr: c)
                self.assertIsNone(res)

    def test_prompt_retry_on_invalid(self) -> None:
        inputs = iter(["31/02/2027", "invalid", "14/03/2027"])
        res = prompt_subscription_date(existing=None, input_fn=lambda _: next(inputs))
        self.assertEqual(res, "2027-03-14")
