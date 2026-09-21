from __future__ import annotations

import math
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from .cache import format_age
from .profiles import Profile
from .subscription import calculate_subscription_health, format_subscription_cells

FRACTIONAL_BLOCKS = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]
SPARKLINE_GLYPHS = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

# 6 distinct color ranks based on remaining percentage
COLOR_RANKS: list[tuple[float, str]] = [
    (90.0, "\033[38;5;48m"),   # Rank 1: Bright Emerald Green (>= 90%)
    (75.0, "\033[38;5;40m"),   # Rank 2: Green (75% - 89.99%)
    (50.0, "\033[38;5;184m"),  # Rank 3: Yellow-Green (50% - 74.99%)
    (25.0, "\033[38;5;214m"),  # Rank 4: Amber / Warm Yellow (25% - 49.99%)
    (10.0, "\033[38;5;208m"),  # Rank 5: Orange (10% - 24.99%)
    (0.0,  "\033[38;5;196m"),  # Rank 6: Red (< 10%)
]


def get_color_for_percentage(pct: float) -> str:
    """Returns the ANSI color code for a quota percentage using 6 distinct ranks."""
    for threshold, color_code in COLOR_RANKS:
        if pct >= threshold:
            return color_code
    return COLOR_RANKS[-1][1]


def format_smooth_bar(
    fraction: float,
    width: int = 10,
    *,
    use_color: bool = True,
    fill_char: str = "█",
    empty_char: str = "░",
) -> str:
    """Formats a high-resolution progress bar using 1/8th sub-block Unicode characters."""
    clamped = max(0.0, min(1.0, fraction))
    total_eighths = round(clamped * width * 8)
    full_blocks = total_eighths // 8
    rem_eighths = total_eighths % 8
    empty_blocks = max(0, width - full_blocks - (1 if rem_eighths > 0 else 0))

    filled_str = fill_char * full_blocks
    partial_str = FRACTIONAL_BLOCKS[rem_eighths] if rem_eighths > 0 else ""
    empty_str = empty_char * empty_blocks

    if not use_color:
        return f"[{filled_str}{partial_str}{empty_str}]"

    pct = clamped * 100.0
    color = get_color_for_percentage(pct)
    dim = "\033[90m"
    reset = "\033[0m"
    return f"{dim}[{reset}{color}{filled_str}{partial_str}{reset}{dim}{empty_str}]{reset}"


def format_sparkline_glyph(fraction: float, *, use_color: bool = True) -> str:
    """Returns a single vertical block glyph representing the level from   to █."""
    clamped = max(0.0, min(1.0, fraction))
    idx = min(8, int(round(clamped * 8)))
    glyph = SPARKLINE_GLYPHS[idx]
    if not use_color:
        return glyph
    color = get_color_for_percentage(clamped * 100.0)
    return f"{color}{glyph}\033[0m"


def format_micro_bar(
    fraction: float,
    width: int = 5,
    *,
    use_color: bool = True,
) -> str:
    """Returns a compact micro-bar like ▰▰▰▱▱."""
    clamped = max(0.0, min(1.0, fraction))
    filled = round(clamped * width)
    filled = max(0, min(width, filled))
    empty = width - filled
    filled_str = "▰" * filled
    empty_str = "▱" * empty
    if not use_color:
        return f"{filled_str}{empty_str}"
    color = get_color_for_percentage(clamped * 100.0)
    dim = "\033[90m"
    reset = "\033[0m"
    return f"{color}{filled_str}{reset}{dim}{empty_str}{reset}"


def display_width(s: str) -> int:
    """Computes terminal display column width, stripping ANSI escapes and accounting for wide characters."""
    import re
    clean = re.sub(r"\033\[[0-9;]*m", "", s)
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in clean)


@dataclass(frozen=True)
class FleetTelemetry:
    total_accounts: int
    gemini_avg_pct: float
    claude_avg_pct: float
    ready_count: int      # >= 70%
    consuming_count: int  # 20% - 69%
    depleted_count: int   # < 20%
    next_reset_acc: str | None
    next_reset_time_str: str | None
    next_reset_dt: datetime | None


def compute_fleet_telemetry(
    completed_map: dict[str, Any],
    extract_bucket_fn: Any,
) -> FleetTelemetry:
    """Calculates aggregate fleet health, average quotas, and earliest reset."""
    total = len(completed_map)
    if total == 0:
        return FleetTelemetry(
            total_accounts=0,
            gemini_avg_pct=0.0,
            claude_avg_pct=0.0,
            ready_count=0,
            consuming_count=0,
            depleted_count=0,
            next_reset_acc=None,
            next_reset_time_str=None,
            next_reset_dt=None,
        )

    g_pcts: list[float] = []
    c_pcts: list[float] = []
    ready = 0
    consuming = 0
    depleted = 0
    earliest_reset: tuple[datetime, str] | None = None

    now = datetime.now(timezone.utc)

    for acc, usage in completed_map.items():
        if usage.status != "success" and not (usage.status == "quiescent" and usage.groups):
            depleted += 1
            continue

        b_g = extract_bucket_fn(usage, "gemini", "5h")
        b_c = extract_bucket_fn(usage, "claude", "5h")

        g_pct = b_g.percentage if b_g else 100.0
        c_pct = b_c.percentage if b_c else 100.0
        g_pcts.append(g_pct)
        c_pcts.append(c_pct)

        min_pct = min(g_pct, c_pct)
        if min_pct >= 70.0:
            ready += 1
        elif min_pct >= 20.0:
            consuming += 1
        else:
            depleted += 1

        # Check reset time
        for b in [b_g, b_c]:
            if b and b.reset_time and b.remaining_fraction < 0.5:
                dt = b.reset_time
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt > now:
                    if earliest_reset is None or dt < earliest_reset[0]:
                        earliest_reset = (dt, acc)

    g_avg = sum(g_pcts) / len(g_pcts) if g_pcts else 0.0
    c_avg = sum(c_pcts) / len(c_pcts) if c_pcts else 0.0

    reset_str = None
    reset_acc = None
    reset_dt = None
    if earliest_reset:
        reset_dt, reset_acc = earliest_reset
        delta = reset_dt - now
        secs = int(delta.total_seconds())
        if secs <= 0:
            reset_str = "now"
        else:
            m = secs // 60
            h = m // 60
            m = m % 60
            reset_str = f"{h}h{m:02d}m" if h > 0 else f"{m}m"

    return FleetTelemetry(
        total_accounts=total,
        gemini_avg_pct=g_avg,
        claude_avg_pct=c_avg,
        ready_count=ready,
        consuming_count=consuming,
        depleted_count=depleted,
        next_reset_acc=reset_acc,
        next_reset_time_str=reset_str,
        next_reset_dt=reset_dt,
    )


def render_fleet_summary_banner(
    telemetry: FleetTelemetry,
    *,
    width: int = 80,
    use_color: bool = True,
) -> list[str]:
    """Renders a sleek top banner summarizing fleet capacity and reset timing."""
    if telemetry.total_accounts == 0:
        return []

    inner_width = max(60, width - 4)
    bar_width = max(10, min(24, inner_width - 45))

    g_bar = format_smooth_bar(telemetry.gemini_avg_pct / 100.0, width=bar_width, use_color=use_color)
    c_bar = format_smooth_bar(telemetry.claude_avg_pct / 100.0, width=bar_width, use_color=use_color)

    dim = "\033[90m" if use_color else ""
    reset = "\033[0m" if use_color else ""
    bold = "\033[1m" if use_color else ""
    green = "\033[38;5;48m" if use_color else ""
    yellow = "\033[38;5;184m" if use_color else ""
    red = "\033[38;5;196m" if use_color else ""
    cyan = "\033[36m" if use_color else ""

    top_border = f"{dim}╭─{reset} {bold}Fleet Capacity ({telemetry.total_accounts} Accounts){reset} {dim}{'─' * max(0, inner_width - 24 - len(str(telemetry.total_accounts)))}╮{reset}"
    bottom_border = f"{dim}╰{'─' * (inner_width + 2)}╯{reset}"

    stats_part = f"{green}● {telemetry.ready_count} Ready{reset}  {yellow}▲ {telemetry.consuming_count} Active{reset}  {red}✖ {telemetry.depleted_count} Low{reset}"
    g_line_content = f"Gemini Pool: {g_bar} {telemetry.gemini_avg_pct:4.0f}% avg   {stats_part}"
    dw_g = display_width(g_line_content)
    pad_g = " " * max(0, inner_width - dw_g)
    line_1 = f"{dim}│{reset} {g_line_content}{pad_g} {dim}│{reset}"

    c_line_content = f"Claude Pool: {c_bar} {telemetry.claude_avg_pct:4.0f}% avg"
    if telemetry.next_reset_acc and telemetry.next_reset_time_str:
        reset_info = f"Next Reset: {cyan}{telemetry.next_reset_acc}{reset} in {cyan}{telemetry.next_reset_time_str}{reset}"
        c_line_content += f"   {reset_info}"
    dw_c = display_width(c_line_content)
    pad_c = " " * max(0, inner_width - dw_c)
    line_2 = f"{dim}│{reset} {c_line_content}{pad_c} {dim}│{reset}"

    return [top_border, line_1, line_2, bottom_border]


def render_usage_grid_lines(
    profiles: Sequence[Profile],
    completed_map: dict[str, Any],
    extract_bucket_fn: Any,
    format_short_reset_fn: Any,
    *,
    use_color: bool = True,
    term_width: int | None = None,
) -> list[str]:
    """Renders Option 2: Multi-column card grid, supporting 2-3 cards side-by-side."""
    if term_width is None:
        term_width, _ = shutil.get_terminal_size((100, 24))

    card_width = 32  # width of each card box
    # compute number of columns (at least 1, max 4)
    cols = max(1, min(4, term_width // (card_width + 1)))

    dim = "\033[90m" if use_color else ""
    reset = "\033[0m" if use_color else ""
    cyan = "\033[36m" if use_color else ""
    red = "\033[91m" if use_color else ""

    cards: list[list[str]] = []
    for p in profiles:
        sub_health = calculate_subscription_health(p.subscription_date)
        sub_str = sub_health.human_remaining.split()[0] if sub_health.human_remaining != "unknown" else "-"

        acc_name = p.name
        if len(acc_name) > 13:
            acc_name = acc_name[:11] + ".."

        # Top border: ┌─ name ── 18mo ─┐
        sub_tag = f" {sub_str} " if sub_str != "-" else ""
        name_tag = f" {acc_name} "
        dash_len = max(1, card_width - 4 - len(name_tag) - len(sub_tag))
        top_line = f"┌─{name_tag}{'─' * dash_len}{sub_tag}─┐"

        if p.name not in completed_map:
            c_lines = [
                top_line,
                f"│ {'Loading...':<{card_width - 4}} │",
                f"│ {'':<{card_width - 4}} │",
                f"│ {'':<{card_width - 4}} │",
                f"│ {'':<{card_width - 4}} │",
                f"└{'─' * (card_width - 2)}┘",
            ]
            cards.append(c_lines)
            continue

        usage = completed_map[p.name]
        if usage.status != "success" and not (usage.status == "quiescent" and usage.groups):
            err_msg = usage.error or "failed"
            if len(err_msg) > card_width - 6:
                err_msg = err_msg[: card_width - 9] + "..."
            c_lines = [
                top_line,
                f"│ {red}✗ {err_msg:<{card_width - 6}}{reset} │",
                f"│ {'':<{card_width - 4}} │",
                f"│ {'':<{card_width - 4}} │",
                f"│ {'':<{card_width - 4}} │",
                f"└{'─' * (card_width - 2)}┘",
            ]
            cards.append(c_lines)
            continue

        b_g5 = extract_bucket_fn(usage, "gemini", "5h")
        b_gw = extract_bucket_fn(usage, "gemini", "week")
        b_c5 = extract_bucket_fn(usage, "claude", "5h")
        b_cw = extract_bucket_fn(usage, "claude", "week")

        g5_bar = format_smooth_bar(b_g5.remaining_fraction if b_g5 else 1.0, width=7, use_color=use_color)
        gw_bar = format_smooth_bar(b_gw.remaining_fraction if b_gw else 1.0, width=7, use_color=use_color)
        c5_bar = format_smooth_bar(b_c5.remaining_fraction if b_c5 else 1.0, width=7, use_color=use_color)
        cw_bar = format_smooth_bar(b_cw.remaining_fraction if b_cw else 1.0, width=7, use_color=use_color)

        g5_pct = f"{b_g5.percentage if b_g5 else 100:3d}%"
        gw_pct = f"{b_gw.percentage if b_gw else 100:3d}%"
        c5_pct = f"{b_c5.percentage if b_c5 else 100:3d}%"
        cw_pct = f"{b_cw.percentage if b_cw else 100:3d}%"

        g5_rst = format_short_reset_fn(b_g5.reset_time) if b_g5 else ""
        c5_rst = format_short_reset_fn(b_c5.reset_time) if b_c5 else ""

        rst_tag = f" {cyan}{g5_rst:>4}{reset}" if g5_rst and g5_rst != "-" else "     "

        # 4 content rows
        r1 = f"│ Gemini 5h: {g5_bar} {g5_pct}{rst_tag} │"
        r2 = f"│        Wk: {gw_bar} {gw_pct}       │"
        r3 = f"│ Claude 5h: {c5_bar} {c5_pct}       │"
        r4 = f"│        Wk: {cw_bar} {cw_pct}       │"
        bot = f"└{'─' * (card_width - 2)}┘"

        cards.append([top_line, r1, r2, r3, r4, bot])

    # Assemble cards into rows of `cols`
    output_lines: list[str] = []
    for i in range(0, len(cards), cols):
        chunk = cards[i : i + cols]
        for line_idx in range(6):  # each card has 6 lines
            row_parts = [card[line_idx] for card in chunk]
            output_lines.append(" ".join(row_parts))
        if i + cols < len(cards):
            output_lines.append("")  # small blank separator between card rows

    return output_lines


def render_usage_matrix_lines(
    profiles: Sequence[Profile],
    completed_map: dict[str, Any],
    extract_bucket_fn: Any,
    *,
    use_color: bool = True,
    term_width: int | None = None,
) -> list[str]:
    """Renders Option 2 (Matrix): Ultra-dense heatmap matrix for dozens to 100+ accounts."""
    if term_width is None:
        term_width, _ = shutil.get_terminal_size((100, 24))

    dim = "\033[90m" if use_color else ""
    reset = "\033[0m" if use_color else ""
    bold = "\033[1m" if use_color else ""

    # Each matrix cell: " acc_name [G: 64% █▍| C:100% ██] " -> ~30 chars
    cell_width = 30
    cols = max(1, min(4, term_width // cell_width))

    cells: list[str] = []
    for p in profiles:
        acc_name = p.name
        if len(acc_name) > 10:
            acc_name = acc_name[:9] + "…"

        if p.name not in completed_map:
            cells.append(f"{acc_name:<10} [Loading...]")
            continue

        usage = completed_map[p.name]
        if usage.status != "success" and not (usage.status == "quiescent" and usage.groups):
            cells.append(f"{acc_name:<10} \033[91m[Error/Failed]\033[0m" if use_color else f"{acc_name:<10} [Error/Failed]")
            continue

        b_g = extract_bucket_fn(usage, "gemini", "5h")
        b_c = extract_bucket_fn(usage, "claude", "5h")

        g_pct = b_g.percentage if b_g else 100
        c_pct = b_c.percentage if b_c else 100

        g_glyph = format_sparkline_glyph(g_pct / 100.0, use_color=use_color)
        c_glyph = format_sparkline_glyph(c_pct / 100.0, use_color=use_color)

        color_g = get_color_for_percentage(float(g_pct)) if use_color else ""
        color_c = get_color_for_percentage(float(c_pct)) if use_color else ""

        cell_str = f"{acc_name:<10} {dim}[{reset}G:{color_g}{g_pct:3d}%{reset}{g_glyph} {dim}│{reset} C:{color_c}{c_pct:3d}%{reset}{c_glyph}{dim}]{reset}"
        cells.append(cell_str)

    # Box wrapping the matrix
    box_width = cols * cell_width + 4
    top_border = f"{dim}┌──{reset} {bold}Fleet Heatmap Matrix ({len(profiles)} Accounts){reset} {dim}{'─' * max(0, box_width - 34 - len(str(len(profiles))))}┐{reset}"
    bottom_border = f"{dim}└──{'─' * max(0, box_width - 4)}┘{reset}"

    lines = [top_border]
    for i in range(0, len(cells), cols):
        chunk = cells[i : i + cols]
        row_str = "  ".join(chunk)
        dw = display_width(row_str)
        pad = " " * max(0, box_width - 4 - dw)
        lines.append(f"{dim}│{reset} {row_str}{pad} {dim}│{reset}")

    # Add legend at bottom
    legend = f"Legend: {get_color_for_percentage(95)}█ >=90%{reset}  {get_color_for_percentage(80)}█ 75-89%{reset}  {get_color_for_percentage(60)}█ 50-74%{reset}  {get_color_for_percentage(30)}█ 25-49%{reset}  {get_color_for_percentage(5)}█ <25%{reset}"
    dw_leg = display_width(legend)
    pad_leg = " " * max(0, box_width - 4 - dw_leg)
    lines.append(f"{dim}│{reset} {legend}{pad_leg} {dim}│{reset}")
    lines.append(bottom_border)

    return lines


def render_usage_telemetry_lines(
    profiles: Sequence[Profile],
    completed_map: dict[str, Any],
    extract_bucket_fn: Any,
    format_short_reset_fn: Any,
    *,
    use_color: bool = True,
    term_width: int | None = None,
) -> list[str]:
    """Renders Option 3: Executive Telemetry View with smart operational tiers and sparklines."""
    if term_width is None:
        term_width, _ = shutil.get_terminal_size((100, 24))

    dim = "\033[90m" if use_color else ""
    reset = "\033[0m" if use_color else ""
    bold = "\033[1m" if use_color else ""
    green = "\033[38;5;48m" if use_color else ""
    yellow = "\033[38;5;184m" if use_color else ""
    red = "\033[38;5;196m" if use_color else ""
    cyan = "\033[36m" if use_color else ""

    # Group into tiers:
    # 🟢 Ready (>70%)
    # 🟡 Consuming (20% - 69%)
    # 🔴 Cooling / Depleted (<20% or error)
    ready_list: list[tuple[Profile, Any]] = []
    consuming_list: list[tuple[Profile, Any]] = []
    cooling_list: list[tuple[Profile, Any]] = []

    for p in profiles:
        usage = completed_map.get(p.name)
        if not usage or (usage.status != "success" and not (usage.status == "quiescent" and usage.groups)):
            cooling_list.append((p, usage))
            continue

        b_g = extract_bucket_fn(usage, "gemini", "5h")
        pct = b_g.percentage if b_g else 100
        if pct >= 70:
            ready_list.append((p, usage))
        elif pct >= 20:
            consuming_list.append((p, usage))
        else:
            cooling_list.append((p, usage))

    lines: list[str] = []

    def format_account_telemetry_row(p: Profile, usage: Any) -> str:
        acc_name = f"{p.name:<12}"
        if not usage:
            return f"   {acc_name} {dim}Loading...{reset}"
        if usage.status != "success" and not (usage.status == "quiescent" and usage.groups):
            err = usage.error or "failed"
            return f"   {acc_name} {red}✗ Failed: {err}{reset}"

        b_g5 = extract_bucket_fn(usage, "gemini", "5h")
        b_gw = extract_bucket_fn(usage, "gemini", "week")
        b_c5 = extract_bucket_fn(usage, "claude", "5h")

        g5_pct = b_g5.percentage if b_g5 else 100
        gw_pct = b_gw.percentage if b_gw else 100
        c5_pct = b_c5.percentage if b_c5 else 100

        g5_bar = format_smooth_bar(g5_pct / 100.0, width=8, use_color=use_color)
        c5_bar = format_smooth_bar(c5_pct / 100.0, width=8, use_color=use_color)

        rst_g5 = format_short_reset_fn(b_g5.reset_time) if b_g5 else ""
        reset_tag = f" {cyan}(resets {rst_g5}){reset}" if rst_g5 and rst_g5 != "-" and g5_pct < 50 else ""

        sub_health = calculate_subscription_health(p.subscription_date)
        sub_str = sub_health.human_remaining

        status_tag = ""
        if getattr(usage, "quiescent", False) or usage.status == "quiescent":
            status_tag = f" {dim}[Idle]{reset}"
        elif usage.cached:
            status_tag = f" {dim}[Cached {format_age(usage.age_seconds)}]{reset}"

        color_g = get_color_for_percentage(float(g5_pct)) if use_color else ""
        color_c = get_color_for_percentage(float(c5_pct)) if use_color else ""

        return (
            f"   {bold}{acc_name}{reset} "
            f"Gemini {g5_bar} {color_g}{g5_pct:3d}%{reset}  "
            f"Claude {c5_bar} {color_c}{c5_pct:3d}%{reset}  "
            f"{dim}Wk:{reset} {gw_pct:3d}%  "
            f"{dim}Sub:{reset} {sub_str:<10}"
            f"{reset_tag}{status_tag}"
        )

    # 🟢 Ready Tier
    if ready_list:
        lines.append(f"{green}🟢 READY TO USE{reset} {dim}(Capacity >= 70% · {len(ready_list)} profiles){reset}")
        for p, u in ready_list:
            lines.append(format_account_telemetry_row(p, u))
        lines.append("")

    # 🟡 Active / Consuming Tier
    if consuming_list:
        lines.append(f"{yellow}🟡 ACTIVE / CONSUMING{reset} {dim}(Capacity 20% - 69% · {len(consuming_list)} profiles){reset}")
        for p, u in consuming_list:
            lines.append(format_account_telemetry_row(p, u))
        lines.append("")

    # 🔴 Cooling / Depleted Tier
    if cooling_list:
        lines.append(f"{red}🔴 DEPLETED / RECOVERING{reset} {dim}(Capacity < 20% or Error · {len(cooling_list)} profiles){reset}")
        for p, u in cooling_list:
            lines.append(format_account_telemetry_row(p, u))
        lines.append("")

    # Smart recommendation
    best_candidate = None
    if ready_list:
        # Pick highest gemini capacity
        best_p = max(
            ready_list,
            key=lambda item: (
                extract_bucket_fn(item[1], "gemini", "5h").percentage if (item[1] and extract_bucket_fn(item[1], "gemini", "5h")) else 0
            ),
        )[0]
        best_candidate = best_p.name

    if best_candidate:
        lines.append(f"💡 {bold}Recommendation:{reset} Profile '{cyan}{best_candidate}{reset}' has prime capacity ready for active work.")

    return lines
