from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Sequence

from .cache import CacheManager, TTL_USAGE_SECONDS, format_age, format_freshness_badge
from .launcher import build_profile_env, resolve_agy
from .profiles import Profile, ProfileStore
from .subscription import calculate_subscription_health, format_subscription_cells
from .wincred import profile_credential_context

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# 6 distinct color ranks based on remaining percentage
COLOR_RANKS: list[tuple[float, str]] = [
    (90.0, "\033[38;5;48m"),   # Rank 1: Bright Emerald Green (>= 90%)
    (75.0, "\033[38;5;40m"),   # Rank 2: Green (75% - 89.99%)
    (50.0, "\033[38;5;184m"),  # Rank 3: Yellow-Green (50% - 74.99%)
    (25.0, "\033[38;5;214m"),  # Rank 4: Amber / Warm Yellow (25% - 49.99%)
    (10.0, "\033[38;5;208m"),  # Rank 5: Orange (10% - 24.99%)
    (0.0,  "\033[38;5;196m"),  # Rank 6: Red (< 10%)
]

QUOTA_COLUMNS: list[tuple[str, str]] = [
    ("gemini", "Gemini"),
    ("claude", "Claude & GPT"),
]

TABLE_COLUMNS: list[tuple[str, str]] = [
    ("gemini", "Gemini"),
    ("claude", "Claude & GPT"),
    ("subscription", "Subscription"),
]


@dataclass(frozen=True)
class UsageBucket:
    id: str
    name: str
    window: str
    remaining_fraction: float
    reset_time: datetime | None
    reset_time_raw: str | None = None
    description: str | None = None

    @property
    def percentage(self) -> int:
        return max(0, min(100, round(self.remaining_fraction * 100)))


@dataclass(frozen=True)
class UsageGroup:
    name: str
    description: str | None
    buckets: list[UsageBucket] = field(default_factory=list)


@dataclass(frozen=True)
class AccountUsage:
    account: str
    status: str  # "success" | "error"
    groups: list[UsageGroup] = field(default_factory=list)
    error: str | None = None
    subscription_date: str | None = None
    cached: bool = False
    age_seconds: float = 0.0
    cached_at: str | None = None


def parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if cleaned.endswith("Z") or cleaned.endswith("z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def format_short_reset_time(dt: datetime | None, now: datetime | None = None) -> str:
    """Formats reset time into an abbreviated string.

    Examples:
        - 6d 21h -> "6d+"
        - 6d 0h  -> "6d"
        - 4h 17m -> "4h+"
        - 3h 0m  -> "3h"
        - 2h 59m -> "2h59m" (less than 3h shows minutes too)
        - 1h 15m -> "1h15m" (less than 3h shows minutes too)
        - 1h 5m  -> "1h05m" (less than 3h shows minutes too)
        - 45m    -> "45m"
        - 9m     -> "9m"
        - <1m    -> "<1m"
        - <= 0   -> "now"
        - None   -> "-"
    """
    if dt is None:
        return "-"
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    delta = dt - now
    total_secs = int(delta.total_seconds())
    if total_secs <= 0:
        return "now"

    days = total_secs // 86400
    rem = total_secs % 86400
    hours = rem // 3600
    rem = rem % 3600
    minutes = rem // 60

    if days > 0:
        return f"{days}d+" if hours > 0 else f"{days}d"
    if hours >= 3:
        return f"{hours}h+" if minutes > 0 else f"{hours}h"
    if hours > 0:
        return f"{hours}h{minutes:02d}m" if minutes > 0 else f"{hours}h"
    if minutes > 0:
        return f"{minutes}m"
    return "<1m"


def format_reset_time(
    dt: datetime | None,
    now: datetime | None = None,
    *,
    short: bool = False,
) -> str:
    if short:
        return format_short_reset_time(dt, now=now)

    if dt is None:
        return "unknown"
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    delta = dt - now
    total_secs = int(delta.total_seconds())
    if total_secs <= 0:
        return "expired"

    days = total_secs // 86400
    rem = total_secs % 86400
    hours = rem // 3600
    rem = rem % 3600
    minutes = rem // 60

    if days > 0 and hours > 0:
        return f"resets in {days}d {hours}h"
    if days > 0 and hours == 0:
        return f"resets in {days}d"
    if hours > 0 and minutes > 0:
        return f"resets in {hours}h {minutes}m"
    if hours > 0 and minutes == 0:
        return f"resets in {hours}h"
    if minutes > 0:
        return f"resets in {minutes}m"
    return "resets in <1m"


def get_color_for_percentage(pct: float) -> str:
    """Returns the ANSI color code for a quota percentage using 6 distinct ranks."""
    for threshold, color_code in COLOR_RANKS:
        if pct >= threshold:
            return color_code
    return COLOR_RANKS[-1][1]


def format_colored_bar(
    fraction: float,
    width: int = 10,
    *,
    use_color: bool = True,
) -> str:
    clamped = max(0.0, min(1.0, fraction))
    filled_count = round(clamped * width)
    filled_count = max(0, min(width, filled_count))
    empty_count = width - filled_count

    filled_str = "█" * filled_count
    empty_str = "░" * empty_count

    if not use_color:
        return f"[{filled_str}{empty_str}]"

    pct = clamped * 100.0
    color = get_color_for_percentage(pct)
    dim = "\033[90m"
    reset = "\033[0m"
    return f"{dim}[{reset}{color}{filled_str}{reset}{dim}{empty_str}]{reset}"


def parse_usage_response(
    raw_output: str,
    account: str,
    subscription_date: str | None = None,
    cached: bool = False,
    age_seconds: float = 0.0,
    cached_at: str | None = None,
) -> AccountUsage:
    if not raw_output or not raw_output.strip():
        return AccountUsage(
            account=account,
            status="error",
            error="empty response from agy",
            subscription_date=subscription_date,
            cached=cached,
            age_seconds=age_seconds,
            cached_at=cached_at,
        )

    try:
        payload = json.loads(raw_output)
    except (json.JSONDecodeError, ValueError) as exc:
        return AccountUsage(
            account=account,
            status="error",
            error=f"malformed JSON response from agy: {exc}",
            subscription_date=subscription_date,
            cached=cached,
            age_seconds=age_seconds,
            cached_at=cached_at,
        )

    if not isinstance(payload, dict):
        return AccountUsage(
            account=account,
            status="error",
            error="malformed JSON response from agy (not an object)",
            subscription_date=subscription_date,
            cached=cached,
            age_seconds=age_seconds,
            cached_at=cached_at,
        )

    status = payload.get("status")
    if status != "SUCCESS":
        return AccountUsage(
            account=account,
            status="error",
            error=f"agy returned status: {status}",
            subscription_date=subscription_date,
            cached=cached,
            age_seconds=age_seconds,
            cached_at=cached_at,
        )

    command = payload.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        return AccountUsage(
            account=account,
            status="error",
            error="missing or invalid usage command in agy response",
            subscription_date=subscription_date,
            cached=cached,
            age_seconds=age_seconds,
            cached_at=cached_at,
        )

    data = command.get("data")
    if not isinstance(data, dict):
        return AccountUsage(
            account=account,
            status="error",
            error="missing command data in agy response",
            subscription_date=subscription_date,
            cached=cached,
            age_seconds=age_seconds,
            cached_at=cached_at,
        )

    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        return AccountUsage(
            account=account,
            status="error",
            error="missing groups list in usage data",
            subscription_date=subscription_date,
            cached=cached,
            age_seconds=age_seconds,
            cached_at=cached_at,
        )

    groups: list[UsageGroup] = []
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        g_name = str(g.get("name", "Unnamed Group"))
        g_desc = g.get("description")
        if g_desc is not None:
            g_desc = str(g_desc)

        buckets: list[UsageBucket] = []
        raw_buckets = g.get("buckets", [])
        if isinstance(raw_buckets, list):
            for b in raw_buckets:
                if not isinstance(b, dict):
                    continue
                b_id = str(b.get("id", ""))
                b_name = str(b.get("name", b_id))
                b_window = str(b.get("window", "unknown"))

                raw_frac = b.get("remaining_fraction", 0.0)
                try:
                    frac = float(raw_frac)
                except (ValueError, TypeError):
                    frac = 0.0
                clamped_frac = max(0.0, min(1.0, frac))

                raw_reset = b.get("reset_time")
                reset_time_raw = str(raw_reset) if raw_reset is not None else None
                reset_time = parse_iso_datetime(reset_time_raw)

                b_desc = b.get("description")
                if b_desc is not None:
                    b_desc = str(b_desc)

                buckets.append(
                    UsageBucket(
                        id=b_id,
                        name=b_name,
                        window=b_window,
                        remaining_fraction=clamped_frac,
                        reset_time=reset_time,
                        reset_time_raw=reset_time_raw,
                        description=b_desc,
                    )
                )

        groups.append(UsageGroup(name=g_name, description=g_desc, buckets=buckets))

    return AccountUsage(
        account=account,
        status="success",
        groups=groups,
        subscription_date=subscription_date,
        cached=cached,
        age_seconds=age_seconds,
        cached_at=cached_at,
    )


def account_usage_to_dict(usage: AccountUsage) -> dict[str, Any]:
    if usage.subscription_date:
        health = calculate_subscription_health(usage.subscription_date)
        sub_dict: dict[str, Any] = {
            "date": usage.subscription_date,
            "days_remaining": health.days_remaining,
            "human_remaining": health.human_remaining,
            "rank": health.rank,
            "status": health.status,
        }
    else:
        sub_dict = {
            "date": None,
            "days_remaining": None,
            "human_remaining": "unknown",
            "rank": None,
            "status": "unknown",
        }

    if usage.status != "success":
        return {
            "account": usage.account,
            "status": "error",
            "cached": usage.cached,
            "age_seconds": round(usage.age_seconds, 1),
            "cached_at": usage.cached_at,
            "error": usage.error or "unknown error",
            "subscription": sub_dict,
        }

    groups_data: list[dict[str, Any]] = []
    for g in usage.groups:
        buckets_data: list[dict[str, Any]] = []
        for b in g.buckets:
            bucket_dict: dict[str, Any] = {
                "id": b.id,
                "name": b.name,
                "window": b.window,
                "remaining_fraction": b.remaining_fraction,
                "percentage": b.percentage,
                "reset_time": b.reset_time_raw
                or (b.reset_time.strftime("%Y-%m-%dT%H:%M:%SZ") if b.reset_time else None),
            }
            if b.description is not None:
                bucket_dict["description"] = b.description
            buckets_data.append(bucket_dict)

        group_dict: dict[str, Any] = {
            "name": g.name,
            "description": g.description,
            "buckets": buckets_data,
        }
        groups_data.append(group_dict)

    return {
        "account": usage.account,
        "status": "success",
        "cached": usage.cached,
        "age_seconds": round(usage.age_seconds, 1),
        "cached_at": usage.cached_at,
        "subscription": sub_dict,
        "groups": groups_data,
    }


def usage_payload_to_dict(usages: Sequence[AccountUsage]) -> dict[str, Any]:
    return {
        "accounts": [account_usage_to_dict(u) for u in usages]
    }


def extract_quota_bucket(usage: AccountUsage, model_family: str, window_type: str) -> UsageBucket | None:
    # 1. Match by group name first
    for group in usage.groups:
        g_name = group.name.lower()
        if model_family == "gemini":
            if "gemini" not in g_name:
                continue
        elif model_family == "claude":
            if "gemini" in g_name and "claude" not in g_name:
                continue

        for bucket in group.buckets:
            w = bucket.window.lower()
            b_id = bucket.id.lower()
            if window_type == "5h":
                if "5h" in w or "5h" in b_id:
                    return bucket
            elif window_type == "week":
                if "week" in w or "week" in b_id or "7d" in w:
                    return bucket

    # 2. Fallback check by bucket id
    for group in usage.groups:
        for bucket in group.buckets:
            b_id = bucket.id.lower()
            w = bucket.window.lower()
            if model_family == "gemini":
                if "gemini" in b_id:
                    if window_type == "5h" and ("5h" in w or "5h" in b_id):
                        return bucket
                    if window_type == "week" and ("week" in w or "week" in b_id):
                        return bucket
            elif model_family == "claude":
                if "3p" in b_id or "claude" in b_id or "gpt" in b_id:
                    if window_type == "5h" and ("5h" in w or "5h" in b_id):
                        return bucket
                    if window_type == "week" and ("week" in w or "week" in b_id):
                        return bucket

    return None


def format_quota_cell(
    bucket: UsageBucket | None,
    prefix: str = "",
    bar_width: int = 10,
    *,
    use_color: bool = True,
    target_width: int = 27,
) -> str:
    content_width = target_width - len(prefix)
    if bucket is None:
        dim = "\033[90m" if use_color else ""
        reset = "\033[0m" if use_color else ""
        prefix_disp = f"{dim}{prefix}{reset}" if prefix else ""
        return f"{prefix_disp}{'-':^{content_width}}"

    bar = format_colored_bar(bucket.remaining_fraction, width=bar_width, use_color=use_color)
    pct = bucket.percentage
    pct_str = f"{pct:3d}%"
    reset_str = format_short_reset_time(bucket.reset_time)
    reset_fmt = f"{reset_str:>5}"

    if use_color:
        dim = "\033[90m"
        reset = "\033[0m"
        color = get_color_for_percentage(float(pct))
        prefix_disp = f"{dim}{prefix}{reset}" if prefix else ""
        pct_display = f"{color}{pct_str}\033[0m"
        reset_display = f"\033[36m{reset_fmt}\033[0m"
    else:
        prefix_disp = prefix
        pct_display = pct_str
        reset_display = reset_fmt

    return f"{prefix_disp}{bar} {pct_display} {reset_display}"


def render_usage_table_lines(
    profiles: Sequence[Profile],
    completed_map: dict[str, AccountUsage],
    *,
    spinner_char: str | None = None,
    use_color: bool = True,
    bar_width: int = 10,
) -> list[str]:
    # cell_width: prefix (4 chars: "5h: " / "Wk: ") + bar (12) + 1 + pct (4) + 1 + reset (5) = 27
    cell_width = bar_width + 17
    spinner_pad = 2 if spinner_char else 0
    acc_col_width = max(10, max([len(p.name) + spinner_pad for p in profiles], default=10))

    quota_area_width = cell_width * len(QUOTA_COLUMNS) + 3 * (len(QUOTA_COLUMNS) - 1)

    top_border = (
        "┌─"
        + ("─" * acc_col_width)
        + "─┬─"
        + "─┬─".join("─" * cell_width for _ in TABLE_COLUMNS)
        + "─┐"
    )

    header_cols = [f"{title:^{cell_width}}" for _, title in TABLE_COLUMNS]
    header_row = f"│ {'Account':<{acc_col_width}} │ " + " │ ".join(header_cols) + " │"

    divider = (
        "├─"
        + ("─" * acc_col_width)
        + "─┼─"
        + "─┼─".join("─" * cell_width for _ in TABLE_COLUMNS)
        + "─┤"
    )

    bottom_border = (
        "└─"
        + ("─" * acc_col_width)
        + "─┴─"
        + "─┴─".join("─" * cell_width for _ in TABLE_COLUMNS)
        + "─┘"
    )

    lines = [top_border, header_row, divider]

    for idx, p in enumerate(profiles):
        sub_health = calculate_subscription_health(p.subscription_date)
        sub_cell_1, sub_cell_2 = format_subscription_cells(
            sub_health, width=cell_width, use_color=use_color
        )

        if p.name in completed_map:
            usage = completed_map[p.name]
            if usage.status == "success":
                # Row 1: 5h limit
                cells_5h = [
                    format_quota_cell(
                        extract_quota_bucket(usage, fam, "5h"),
                        prefix="5h: ",
                        bar_width=bar_width,
                        use_color=use_color,
                        target_width=cell_width,
                    )
                    for fam, _ in QUOTA_COLUMNS
                ]
                # Row 2: Week limit (below the 5h)
                cells_wk = [
                    format_quota_cell(
                        extract_quota_bucket(usage, fam, "week"),
                        prefix="Wk: ",
                        bar_width=bar_width,
                        use_color=use_color,
                        target_width=cell_width,
                    )
                    for fam, _ in QUOTA_COLUMNS
                ]
                acc_sub = f"· {format_age(usage.age_seconds)}" if usage.cached else ""
                if len(acc_sub) > acc_col_width:
                    acc_sub = acc_sub[:acc_col_width]
                acc_sub_disp = f"\033[90m{acc_sub:<{acc_col_width}}\033[0m" if (use_color and acc_sub) else f"{acc_sub:<{acc_col_width}}"
                row_1 = f"│ {usage.account:<{acc_col_width}} │ " + " │ ".join(cells_5h + [sub_cell_1]) + " │"
                row_2 = f"│ {acc_sub_disp} │ " + " │ ".join(cells_wk + [sub_cell_2]) + " │"
                lines.append(row_1)
                lines.append(row_2)
            else:
                err_msg = usage.error or "unknown error"
                failed_text = f"✗ Failed: {err_msg}"
                if len(failed_text) > quota_area_width:
                    failed_text = failed_text[: quota_area_width - 3] + "..."
                if use_color:
                    failed_display = f"\033[91m{failed_text:<{quota_area_width}}\033[0m"
                else:
                    failed_display = f"{failed_text:<{quota_area_width}}"
                row_1 = f"│ {usage.account:<{acc_col_width}} │ {failed_display} │ {sub_cell_1} │"
                row_2 = f"│ {'':<{acc_col_width}} │ {'':<{quota_area_width}} │ {sub_cell_2} │"
                lines.append(row_1)
                lines.append(row_2)
        else:
            loading_text = "Loading..."
            if use_color:
                loading_display = f"\033[90m{loading_text:<{quota_area_width}}\033[0m"
            else:
                loading_display = f"{loading_text:<{quota_area_width}}"
            acc_str = f"{spinner_char} {p.name}" if spinner_char else p.name
            row_1 = f"│ {acc_str:<{acc_col_width}} │ {loading_display} │ {sub_cell_1} │"
            row_2 = f"│ {'':<{acc_col_width}} │ {'':<{quota_area_width}} │ {sub_cell_2} │"
            lines.append(row_1)
            lines.append(row_2)

        # "only seperate after the week"
        if idx < len(profiles) - 1:
            lines.append(divider)

    lines.append(bottom_border)
    return lines


def format_account_usage(usage: AccountUsage) -> list[str]:
    """Preserved for backwards compatibility with detailed list formatting."""
    lines: list[str] = []
    if usage.status != "success":
        lines.append(f"✗ {usage.account}")
        lines.append(f"    Failed: {usage.error or 'unknown error'}")
        return lines

    lines.append(f"✓ {usage.account}")
    if not usage.groups:
        lines.append("    (no quota data available)")
        return lines

    for group in usage.groups:
        lines.append(f"    {group.name}")
        bucket_strs: list[str] = []
        for b in group.buckets:
            reset_str = format_short_reset_time(b.reset_time)
            pct = b.remaining_fraction * 100.0
            bucket_strs.append(f"{b.window}: {pct:5.1f}% ({reset_str})")

        if not bucket_strs:
            lines.append("      (no buckets)")
        else:
            for i in range(0, len(bucket_strs), 2):
                chunk = bucket_strs[i : i + 2]
                lines.append("      " + "        ".join(chunk))

    return lines


_active_subprocesses: set[asyncio.subprocess.Process] = set()


def kill_active_subprocesses() -> None:
    for proc in list(_active_subprocesses):
        try:
            proc.kill()
        except OSError:
            pass


async def _default_subprocess_runner(
    argv: list[str],
    env: dict[str, str],
    timeout: float,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _active_subprocesses.add(proc)
    try:
        stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout_data.decode("utf-8", errors="replace")
        err = stderr_data.decode("utf-8", errors="replace")
        return proc.returncode, out, err
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except OSError:
            pass
        raise
    finally:
        _active_subprocesses.discard(proc)


async def fetch_account_usage_async(
    agy_path: Path,
    profile: Profile,
    semaphore: asyncio.Semaphore,
    timeout: float = 30.0,
    force_refresh: bool = False,
    cache_manager: CacheManager | None = None,
    runner: Callable[..., Coroutine[Any, Any, tuple[int, str, str]]] | None = None,
) -> AccountUsage:
    if not force_refresh and cache_manager is not None:
        cached_entry = cache_manager.get_usage(profile.name, max_age=TTL_USAGE_SECONDS)
        if cached_entry is not None:
            parsed_data, raw_out, age_secs, cached_at = cached_entry
            cached_usage = parse_usage_response(
                raw_out,
                account=profile.name,
                subscription_date=profile.subscription_date,
                cached=True,
                age_seconds=age_secs,
                cached_at=cached_at,
            )
            if cached_usage.status == "success":
                return cached_usage

    async with semaphore:
        if not profile.home.exists():
            return AccountUsage(
                account=profile.name,
                status="error",
                error=f"profile home directory does not exist: {profile.home}",
                subscription_date=profile.subscription_date,
            )

        env = build_profile_env(profile.home)
        argv = [
            str(agy_path),
            "--dangerously-skip-permissions",
            "-p",
            "/usage",
            "--output-format",
            "json",
        ]

        try:
            if runner is not None:
                code, out, err = await asyncio.wait_for(runner(argv, env, timeout), timeout=timeout)
            else:
                with profile_credential_context(profile.home):
                    code, out, err = await asyncio.wait_for(_default_subprocess_runner(argv, env, timeout), timeout=timeout)
        except asyncio.TimeoutError:
            timeout_str = f"{int(timeout)}s" if timeout.is_integer() else f"{timeout}s"
            return AccountUsage(
                account=profile.name,
                status="error",
                error=f"timed out after {timeout_str}",
                subscription_date=profile.subscription_date,
            )
        except Exception as exc:
            return AccountUsage(
                account=profile.name,
                status="error",
                error=f"failed to execute agy: {exc}",
                subscription_date=profile.subscription_date,
            )

        if code != 0:
            err_msg = err.strip() or out.strip()
            first_line = err_msg.splitlines()[0] if err_msg else ""
            detail = f" ({first_line})" if first_line else ""
            return AccountUsage(
                account=profile.name,
                status="error",
                error=f"agy exited with status {code}{detail}",
                subscription_date=profile.subscription_date,
            )

        usage = parse_usage_response(
            out,
            account=profile.name,
            subscription_date=profile.subscription_date,
            cached=False,
            age_seconds=0.0,
        )
        if usage.status == "success" and cache_manager is not None:
            cache_manager.set_usage(profile.name, account_usage_to_dict(usage), out)
        return usage


async def fetch_all_usage(
    agy_path: Path,
    profiles: Sequence[Profile],
    concurrency_limit: int = 8,
    timeout: float = 30.0,
    force_refresh: bool = False,
    cache_manager: CacheManager | None = None,
    on_progress: Callable[[AccountUsage], None] | None = None,
    runner: Callable[..., Coroutine[Any, Any, tuple[int, str, str]]] | None = None,
) -> list[AccountUsage]:
    if not profiles:
        return []

    sem_limit = max(1, min(len(profiles), concurrency_limit))
    semaphore = asyncio.Semaphore(sem_limit)

    async def _worker(profile: Profile) -> AccountUsage:
        usage = await fetch_account_usage_async(
            agy_path,
            profile,
            semaphore,
            timeout=timeout,
            force_refresh=force_refresh,
            cache_manager=cache_manager,
            runner=runner,
        )
        if on_progress is not None:
            on_progress(usage)
        return usage

    tasks = [asyncio.create_task(_worker(p)) for p in profiles]
    try:
        return await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        kill_active_subprocesses()
        for t in tasks:
            if not t.done():
                t.cancel()
        raise


class ProgressiveUsageUI:
    def __init__(
        self,
        profiles: Sequence[Profile],
        is_tty: bool | None = None,
        stdout: Any = None,
    ) -> None:
        self.profiles = list(profiles)
        self.stdout = sys.stdout if stdout is None else stdout
        self.is_tty = sys.stdout.isatty() if is_tty is None else is_tty
        self.completed: dict[str, AccountUsage] = {}
        self.spinner_idx = 0
        self.last_lines_count = 0
        self.completed_count = 0
        self.total = len(self.profiles)
        self._stop_event = asyncio.Event()

    def on_progress(self, usage: AccountUsage) -> None:
        self.completed[usage.account] = usage
        self.completed_count += 1
        if not self.is_tty:
            if usage.status == "success":
                self.stdout.write(f"[{self.completed_count}/{self.total}] {usage.account}: completed\n")
            else:
                err_msg = usage.error or "unknown error"
                m = re.search(r"\((.*?)\)", err_msg)
                short_err = m.group(1) if m else err_msg
                self.stdout.write(f"[{self.completed_count}/{self.total}] {usage.account}: failed ({short_err})\n")
            self.stdout.flush()

    def _render_title(self) -> str:
        cached_count = sum(1 for u in self.completed.values() if u.cached)
        if cached_count == len(self.completed) and cached_count > 0:
            max_age = max((u.age_seconds for u in self.completed.values()), default=0.0)
            return f"Antigravity Usage (Cached {format_age(max_age)})"
        elif cached_count > 0:
            return f"Antigravity Usage ({cached_count}/{len(self.completed)} cached)"
        return "Antigravity Usage"

    def _render_tty_frame(self, spinner_char: str) -> list[str]:
        lines: list[str] = [self._render_title(), ""]
        table_lines = render_usage_table_lines(
            self.profiles,
            self.completed,
            spinner_char=spinner_char,
            use_color=True,
        )
        lines.extend(table_lines)
        return lines

    def render_tty(self, spinner_char: str) -> None:
        lines = self._render_tty_frame(spinner_char)
        out: list[str] = []
        if self.last_lines_count > 0:
            out.append(f"\033[{self.last_lines_count}A\r")
        for line in lines:
            out.append(f"\033[2K{line}\n")
        self.stdout.write("".join(out))
        self.stdout.flush()
        self.last_lines_count = len(lines)

    async def spinner_loop(self) -> None:
        if not self.is_tty:
            return
        # Hide terminal cursor
        self.stdout.write("\033[?25l")
        self.stdout.flush()
        try:
            while not self._stop_event.is_set():
                spinner_char = SPINNER_FRAMES[self.spinner_idx % len(SPINNER_FRAMES)]
                self.spinner_idx += 1
                self.render_tty(spinner_char)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.08)
                    break
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stdout.write("\033[?25h")
            self.stdout.flush()

    def finish(self, usages: Sequence[AccountUsage]) -> None:
        self._stop_event.set()
        completed_map = {u.account: u for u in usages}
        table_lines = render_usage_table_lines(
            self.profiles,
            completed_map,
            spinner_char=None,
            use_color=self.is_tty,
        )
        title = self._render_title()
        if self.is_tty:
            lines = [title, ""] + table_lines
            out: list[str] = []
            if self.last_lines_count > 0:
                out.append(f"\033[{self.last_lines_count}A\r")
            for line in lines:
                out.append(f"\033[2K{line}\n")
            out.append("\033[?25h")
            self.stdout.write("".join(out))
            self.stdout.flush()
        else:
            lines = [title, ""] + table_lines + [""]
            self.stdout.write("\n".join(lines))
            self.stdout.flush()


async def run_usage(
    agy_path: Path,
    profiles: Sequence[Profile],
    *,
    json_mode: bool = False,
    refresh: bool = False,
    timeout: float = 30.0,
    concurrency_limit: int = 8,
    cache_manager: CacheManager | None = None,
    is_tty: bool | None = None,
    stdout: Any = None,
    runner: Callable[..., Coroutine[Any, Any, tuple[int, str, str]]] | None = None,
) -> list[AccountUsage]:
    out = sys.stdout if stdout is None else stdout
    cm = cache_manager if cache_manager is not None else CacheManager()
    if not profiles:
        if json_mode:
            out.write(json.dumps({"accounts": []}, indent=2) + "\n")
            out.flush()
        else:
            out.write("No profiles configured. Run 'agym setup <profile>' first.\n")
            out.flush()
        return []

    if json_mode:
        usages = await fetch_all_usage(
            agy_path,
            profiles,
            concurrency_limit=concurrency_limit,
            timeout=timeout,
            force_refresh=refresh,
            cache_manager=cm,
            runner=runner,
        )
        payload = usage_payload_to_dict(usages)
        out.write(json.dumps(payload, indent=2) + "\n")
        out.flush()
        return usages

    # Check if any profile requires live query
    need_live_query = refresh
    if not refresh:
        for p in profiles:
            if cm.get_usage(p.name, max_age=TTL_USAGE_SECONDS) is None:
                need_live_query = True
                break

    tty_mode = sys.stdout.isatty() if is_tty is None else is_tty
    if need_live_query and tty_mode:
        ui = ProgressiveUsageUI(profiles, is_tty=tty_mode, stdout=out)
        spinner_task = asyncio.create_task(ui.spinner_loop())
        usages_result: list[AccountUsage] = []
        try:
            usages_result = await fetch_all_usage(
                agy_path,
                profiles,
                concurrency_limit=concurrency_limit,
                timeout=timeout,
                force_refresh=refresh,
                cache_manager=cm,
                on_progress=ui.on_progress,
                runner=runner,
            )
        finally:
            ui.finish(usages_result)
            await spinner_task
        return usages_result

    ui = ProgressiveUsageUI(profiles, is_tty=tty_mode, stdout=out)
    usages_result = await fetch_all_usage(
        agy_path,
        profiles,
        concurrency_limit=concurrency_limit,
        timeout=timeout,
        force_refresh=refresh,
        cache_manager=cm,
        on_progress=ui.on_progress if not tty_mode else None,
        runner=runner,
    )
    ui.finish(usages_result)
    return usages_result
