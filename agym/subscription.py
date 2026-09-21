from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from typing import Callable

# ANSI color codes matching agym.usage.COLOR_RANKS
COLOR_BRIGHT_GREEN = "\033[38;5;48m"   # Rank 5: Very safe (>= 6 months)
COLOR_GREEN = "\033[38;5;40m"          # Rank 4: Safe (3 to 6 months)
COLOR_YELLOW = "\033[38;5;184m"        # Rank 3: Moderate (1 to 3 months)
COLOR_ORANGE = "\033[38;5;208m"        # Rank 2: Warning / Expiring soon (< 1 month)
COLOR_RED = "\033[38;5;196m"           # Rank 1: Critical (<= 7 days) / Expired
COLOR_DIM = "\033[90m"                 # Unknown / Brackets / Neutral
COLOR_RESET = "\033[0m"


class SubscriptionError(ValueError):
    """Raised when an invalid subscription date string is supplied."""
    pass


def add_months(d: date, n: int) -> date:
    """Adds n calendar months to date d, clamping the day to month length."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, max_day))


def diff_months_days(start: date, end: date) -> tuple[int, int]:
    """Returns (months, days) difference between start and end where end >= start."""
    if end < start:
        return 0, -(start - end).days

    # Approximate month difference
    m = (end.year - start.year) * 12 + (end.month - start.month)
    if add_months(start, m) > end:
        m -= 1
    while add_months(start, m + 1) <= end:
        m += 1

    remaining_days = (end - add_months(start, m)).days
    return m, remaining_days


def parse_subscription_date(raw: str | None) -> date | None:
    """Parses a date string into a datetime.date object.

    Supported formats:
      - DD/MM/YYYY
      - DD-MM-YYYY
      - YYYY-MM-DD (canonical ISO)
      - YYYY/MM/DD

    Rejects impossible calendar dates (e.g. 31/02/2027) and incomplete years.
    Returns None if raw is None or empty.
    """
    if raw is None:
        return None
    val = raw.strip()
    if not val:
        return None

    # Check for 4-digit year pattern
    formats = [
        ("%d/%m/%Y", r"^\d{1,2}/\d{1,2}/\d{4}$"),
        ("%d-%m-%Y", r"^\d{1,2}-\d{1,2}-\d{4}$"),
        ("%Y-%m-%d", r"^\d{4}-\d{1,2}-\d{1,2}$"),
        ("%Y/%m/%d", r"^\d{4}/\d{1,2}/\d{1,2}$"),
    ]

    matched_format = None
    for fmt, pattern in formats:
        if re.match(pattern, val):
            matched_format = fmt
            break

    if matched_format is None:
        raise SubscriptionError(
            f"invalid date format: '{val}'. Expected DD/MM/YYYY (e.g. 14/03/2027) or YYYY-MM-DD"
        )

    try:
        parsed = datetime.strptime(val, matched_format).date()
    except ValueError as exc:
        raise SubscriptionError(f"invalid calendar date '{val}': {exc}") from exc

    if parsed.year < 1970 or parsed.year > 2100:
        raise SubscriptionError(f"year out of reasonable range (1970-2100): {parsed.year}")

    return parsed


def format_user_date(d: date | str | None) -> str:
    """Formats a date object or ISO string into human-friendly DD/MM/YYYY."""
    if d is None:
        return "unknown"
    if isinstance(d, str):
        parsed = parse_subscription_date(d)
        if parsed is None:
            return "unknown"
        d = parsed
    return d.strftime("%d/%m/%Y")


def format_iso_date(d: date | str | None) -> str | None:
    """Formats a date object or date string into canonical ISO YYYY-MM-DD."""
    if d is None:
        return None
    if isinstance(d, str):
        parsed = parse_subscription_date(d)
        if parsed is None:
            return None
        d = parsed
    return d.isoformat()


@dataclass(frozen=True)
class SubscriptionHealth:
    date: date | None
    days_remaining: int | None
    status: str  # "very_safe" | "safe" | "moderate" | "warning" | "critical" | "expired" | "unknown"
    rank: int | None  # 5, 4, 3, 2, 1, or None
    human_remaining: str  # e.g. "8mo 12d remaining", "18d remaining", "expires today", "expired 12d ago", "unknown"
    color_code: str
    bar_blocks: int


def calculate_subscription_health(
    sub_date: str | date | None,
    now: date | None = None,
) -> SubscriptionHealth:
    """Calculates subscription health rank, timing, and visual styling.

    Thresholds:
      - Unknown: No date set -> Rank None, 0 blocks, Dim color
      - Expired: Date in past -> Rank 1, 0 blocks, Red
      - Critical: Date is today -> Rank 1, 1 block, Red
      - Critical: 1 to 7 days remaining -> Rank 1, 2 blocks, Red
      - Warning: 8 days to < 1 month remaining -> Rank 2, 4 blocks, Orange
      - Moderate: 1 to < 3 months remaining -> Rank 3, 6 blocks, Yellow
      - Safe: 3 to < 6 months remaining -> Rank 4, 8 blocks, Green
      - Very Safe: >= 6 months remaining -> Rank 5, 10 blocks, Bright Emerald Green
    """
    if sub_date is None or (isinstance(sub_date, str) and not sub_date.strip()):
        return SubscriptionHealth(
            date=None,
            days_remaining=None,
            status="unknown",
            rank=None,
            human_remaining="unknown",
            color_code=COLOR_DIM,
            bar_blocks=0,
        )

    if isinstance(sub_date, str):
        d = parse_subscription_date(sub_date)
        if d is None:
            return SubscriptionHealth(
                date=None,
                days_remaining=None,
                status="unknown",
                rank=None,
                human_remaining="unknown",
                color_code=COLOR_DIM,
                bar_blocks=0,
            )
    else:
        d = sub_date

    if now is None:
        now = datetime.now().astimezone().date()

    if d < now:
        past_days = (now - d).days
        human = f"expired {past_days}d ago"
        return SubscriptionHealth(
            date=d,
            days_remaining=-past_days,
            status="expired",
            rank=1,
            human_remaining=human,
            color_code=COLOR_RED,
            bar_blocks=0,
        )

    if d == now:
        return SubscriptionHealth(
            date=d,
            days_remaining=0,
            status="critical",
            rank=1,
            human_remaining="expires today",
            color_code=COLOR_RED,
            bar_blocks=1,
        )

    delta_days = (d - now).days
    m, days = diff_months_days(now, d)

    if m >= 1:
        human_dur = f"{m}mo {days}d remaining" if days > 0 else f"{m}mo remaining"
    else:
        human_dur = f"{delta_days}d remaining"

    if m >= 6:
        return SubscriptionHealth(
            date=d,
            days_remaining=delta_days,
            status="very_safe",
            rank=5,
            human_remaining=human_dur,
            color_code=COLOR_BRIGHT_GREEN,
            bar_blocks=10,
        )
    if m >= 3:
        return SubscriptionHealth(
            date=d,
            days_remaining=delta_days,
            status="safe",
            rank=4,
            human_remaining=human_dur,
            color_code=COLOR_GREEN,
            bar_blocks=8,
        )
    if m >= 1:
        return SubscriptionHealth(
            date=d,
            days_remaining=delta_days,
            status="moderate",
            rank=3,
            human_remaining=human_dur,
            color_code=COLOR_YELLOW,
            bar_blocks=6,
        )

    # Less than 1 month
    if delta_days > 7:
        return SubscriptionHealth(
            date=d,
            days_remaining=delta_days,
            status="warning",
            rank=2,
            human_remaining=human_dur,
            color_code=COLOR_ORANGE,
            bar_blocks=4,
        )

    # 1 to 7 days
    return SubscriptionHealth(
        date=d,
        days_remaining=delta_days,
        status="critical",
        rank=1,
        human_remaining=human_dur,
        color_code=COLOR_RED,
        bar_blocks=2,
    )


def format_subscription_bar(
    bar_blocks: int,
    color_code: str,
    width: int = 10,
    *,
    use_color: bool = True,
) -> str:
    """Formats a visual health bar with brackets."""
    blocks = max(0, min(width, bar_blocks))
    filled = "█" * blocks
    empty = "░" * (width - blocks)

    if not use_color:
        return f"[{filled}{empty}]"

    return f"{COLOR_DIM}[{COLOR_RESET}{color_code}{filled}{COLOR_RESET}{COLOR_DIM}{empty}]{COLOR_RESET}"


def format_subscription_cells(
    health: SubscriptionHealth,
    width: int = 27,
    *,
    use_color: bool = True,
    now: date | None = None,
) -> tuple[str, str]:
    """Generates (row_1, row_2) cell content for the usage table (width chars each)."""
    bar_width = 10
    bar = format_subscription_bar(health.bar_blocks, health.color_code, width=bar_width, use_color=use_color)
    bar_plain_len = bar_width + 2  # "[██████████]" is 12 chars

    if health.status == "unknown":
        label_rem = "unknown"
        if use_color:
            row_1_text = f"{bar} {COLOR_DIM}{label_rem:<{width - bar_plain_len - 1}}{COLOR_RESET}"
            row_2_text = f"{COLOR_DIM}{'(date not set)':<{width}}{COLOR_RESET}"
        else:
            row_1_text = f"{bar} {label_rem:<{width - bar_plain_len - 1}}"
            row_2_text = f"{'(date not set)':<{width}}"
        return row_1_text, row_2_text

    if health.status == "expired":
        past_days = abs(health.days_remaining or 0)
        label_rem = f"expired {past_days}d"
        label_date = f"Expired: {format_user_date(health.date)}"
        if use_color:
            row_1_text = f"{bar} {COLOR_RED}{label_rem:<{width - bar_plain_len - 1}}{COLOR_RESET}"
            row_2_text = f"{COLOR_RED}{label_date:<{width}}{COLOR_RESET}"
        else:
            row_1_text = f"{bar} {label_rem:<{width - bar_plain_len - 1}}"
            row_2_text = f"{label_date:<{width}}"
        return row_1_text, row_2_text

    # Active / future / today
    if health.days_remaining == 0:
        label_rem = "today"
        label_date = f"Expires: {format_user_date(health.date)}"
    else:
        # Format compact duration for row 1
        d = health.date
        if now is None:
            now = datetime.now().astimezone().date()
        m, days = diff_months_days(now, d)
        if m >= 1:
            label_rem = f"{m}mo {days}d" if days > 0 else f"{m}mo"
        else:
            label_rem = f"{health.days_remaining}d"
        label_date = f"Renews: {format_user_date(health.date)}"

    if use_color:
        color = health.color_code
        rem_colored = f"{color}{label_rem}{COLOR_RESET}"
        # Calculate padding based on raw string length
        padding = width - bar_plain_len - 1 - len(label_rem)
        row_1_text = f"{bar} {rem_colored}{' ' * max(0, padding)}"
        date_colored = f"\033[36m{label_date}{COLOR_RESET}"
        row_2_text = f"{date_colored}{' ' * max(0, width - len(label_date))}"
    else:
        row_1_text = f"{bar} {label_rem:<{width - bar_plain_len - 1}}"
        row_2_text = f"{label_date:<{width}}"

    return row_1_text, row_2_text


def format_compact_sub(
    health: SubscriptionHealth | None,
    now: date | None = None,
) -> str:
    """Formats subscription remaining duration to at most 3 characters.

    Examples:
        - 1 month: '1mo'
        - 6 months: '6mo'
        - 18 months: '18m'
        - 14 days: '14d'
        - 1 day: '1d'
        - Today / hours remaining: '20h'
        - Expired: 'exp'
        - Unset / unknown: '-'
    """
    if health is None or health.status == "unknown" or health.date is None:
        return "-"
    if health.status == "expired" or (health.days_remaining is not None and health.days_remaining < 0):
        return "exp"
    if health.days_remaining == 0:
        now_dt = datetime.now(timezone.utc)
        end_of_day = datetime(now_dt.year, now_dt.month, now_dt.day, 23, 59, 59, tzinfo=timezone.utc)
        rem_hours = max(1, int((end_of_day - now_dt).total_seconds() // 3600))
        return f"{rem_hours}h"
    if health.days_remaining is not None:
        if health.days_remaining < 30:
            return f"{health.days_remaining}d"
        if now is None:
            now = datetime.now().astimezone().date()
        m, _ = diff_months_days(now, health.date)
        if m < 1:
            m = 1
        if m < 10:
            return f"{m}mo"
        return f"{m}m"
    return "-"


def format_colored_compact_sub(
    health: SubscriptionHealth | None,
    *,
    use_color: bool = True,
    now: date | None = None,
) -> str:
    """Returns the compact subscription string colored according to its health rank."""
    compact = format_compact_sub(health, now=now)
    if not use_color or health is None:
        return compact
    color = health.color_code
    return f"{color}{compact}{COLOR_RESET}"


def prompt_subscription_date(
    existing: str | None = None,
    input_fn: Callable[[str], str] = input,
) -> str | None:
    """Interactively prompts user for subscription date with validation.

    If existing is provided:
      - Enter keeps current value.
      - 'clear', 'none', 'remove' clears it to None.
      - New valid date updates it.

    If existing is None:
      - Enter skips, returning None.
      - New valid date returns canonical ISO YYYY-MM-DD.
    """
    if existing:
        user_disp = format_user_date(existing)
        health = calculate_subscription_health(existing)
        print(f"Current subscription date: {user_disp} ({health.human_remaining})")
        prompt_msg = "Subscription renewal/expiration date [DD/MM/YYYY] (Enter to keep, 'clear' to remove): "
    else:
        prompt_msg = "Subscription renewal/expiration date [DD/MM/YYYY] (optional, Enter to skip): "

    while True:
        try:
            line = input_fn(prompt_msg).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise

        if not line:
            # Enter pressed: keep existing or skip
            return format_iso_date(existing)

        if line.lower() in {"clear", "none", "remove", "null", "-"}:
            return None

        try:
            parsed = parse_subscription_date(line)
            return format_iso_date(parsed)
        except SubscriptionError as exc:
            print(f"agym: {exc}")
            print("Please enter a valid date in DD/MM/YYYY format (e.g. 14/03/2027), or press Enter.")
