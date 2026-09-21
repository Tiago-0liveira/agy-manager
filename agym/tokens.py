from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Sequence

from .cache import CacheManager, TTL_TOKENS_SECONDS, format_age, format_freshness_badge
from .launcher import build_profile_env
from .profiles import Profile
from .usage import ProgressiveUsageUI, _default_subprocess_runner, kill_active_subprocesses

# Palette definitions
COLOR_INPUT = "\033[38;5;39m"       # Blue / Bright Sky
COLOR_OUTPUT = "\033[38;5;48m"      # Emerald Green
COLOR_THINKING = "\033[38;5;177m"   # Purple / Magenta
COLOR_CACHE = "\033[38;5;214m"      # Amber / Warm Yellow
COLOR_DIM = "\033[90m"
COLOR_BOLD = "\033[1m"
COLOR_CYAN = "\033[36m"
COLOR_RED = "\033[91m"
COLOR_RESET = "\033[0m"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0

    @property
    def cache_efficiency(self) -> float:
        """Returns cache read efficiency percentage: cache_read / (input + cache_read)."""
        prompt_total = self.input_tokens + self.cache_read_tokens
        if prompt_total <= 0:
            return 0.0
        return (self.cache_read_tokens / prompt_total) * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "total_tokens": self.total_tokens,
            "cache_efficiency": round(self.cache_efficiency, 2),
        }

    @classmethod
    def from_dict(cls, data: Any) -> TokenUsage:
        if not isinstance(data, dict):
            return cls()
        inp = int(data.get("input_tokens", 0) or 0)
        out = int(data.get("output_tokens", 0) or 0)
        thk = int(data.get("thinking_tokens", 0) or 0)
        crd = int(data.get("cache_read_tokens", 0) or 0)
        tot = int(data.get("total_tokens", inp + out + thk + crd) or (inp + out + thk + crd))
        return cls(
            input_tokens=inp,
            output_tokens=out,
            thinking_tokens=thk,
            cache_read_tokens=crd,
            total_tokens=tot,
        )


@dataclass(frozen=True)
class AccountTokenUsage:
    account: str
    status: str  # "success" | "error"
    usage: TokenUsage = field(default_factory=TokenUsage)
    latest_turn: TokenUsage | None = None
    cached: bool = False
    age_seconds: float = 0.0
    cached_at: str | None = None
    error: str | None = None
    subscription_date: str | None = None
    snapshot_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "account": self.account,
            "status": self.status,
            "cached": self.cached,
            "age_seconds": round(self.age_seconds, 1),
            "cached_at": self.cached_at,
            "usage": self.usage.to_dict(),
            "snapshot_count": self.snapshot_count,
        }
        if self.latest_turn is not None:
            result["latest_turn"] = self.latest_turn.to_dict()
        if self.error:
            result["error"] = self.error
        if self.subscription_date:
            result["subscription_date"] = self.subscription_date
        return result


def format_token_count(n: int) -> str:
    """Formats raw token integers into concise human-readable strings (e.g., 12.4k, 1.2M, 850)."""
    if n < 0:
        n = 0
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        val = n / 1_000.0
        return f"{val:.1f}k"
    if n < 1_000_000_000:
        val = n / 1_000_000.0
        return f"{val:.1f}M"
    val = n / 1_000_000_000.0
    return f"{val:.1f}B"


def parse_token_usage_payload(payload_dict: dict[str, Any]) -> TokenUsage:
    """Extracts TokenUsage from agy JSON response payload."""
    raw_usage = payload_dict.get("usage")
    if not isinstance(raw_usage, dict):
        return TokenUsage()
    return TokenUsage.from_dict(raw_usage)


def _parse_protobuf_fields(data: bytes) -> dict[int, list[int | bytes]]:
    """Decodes raw protobuf tag-value pairs into a field_number -> list[value] mapping."""
    i = 0
    fields: dict[int, list[int | bytes]] = {}
    n = len(data)
    while i < n:
        tag_byte = data[i]
        i += 1
        wire_type = tag_byte & 7
        field_num = tag_byte >> 3
        if wire_type == 0:  # varint
            val = 0
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            sub = data[i : i + length]
            i += length
            fields.setdefault(field_num, []).append(sub)
        elif wire_type == 1:  # 64-bit
            i += 8
        elif wire_type == 5:  # 32-bit
            i += 4
        else:
            break
    return fields


def scan_profile_conversations(
    profile_home: Path,
) -> tuple[TokenUsage, int, TokenUsage | None]:
    """Scans Antigravity conversation SQLite databases and transcript logs for a profile.

    Returns (cumulative_usage, turn_count, latest_turn).
    """
    gemini_dir = profile_home / ".gemini" / "antigravity-cli"
    if not gemini_dir.exists():
        return TokenUsage(), 0, None

    total_in = 0
    total_out = 0
    total_thk = 0
    total_crd = 0
    total_tot = 0
    turns = 0
    latest_turn: TokenUsage | None = None

    # 1. Primary: Scan Antigravity conversation SQLite databases (~/.gemini/antigravity-cli/conversations/*.db)
    conv_dir = gemini_dir / "conversations"
    if conv_dir.exists():
        try:
            db_files = sorted(conv_dir.glob("*.db"), key=lambda p: p.stat().st_mtime)
            for db_path in db_files:
                try:
                    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
                    cursor = conn.cursor()
                    cursor.execute("SELECT idx, metadata FROM steps WHERE step_type = 15 ORDER BY idx ASC;")
                    for _, meta in cursor.fetchall():
                        if not meta or not isinstance(meta, bytes):
                            continue
                        step_meta = _parse_protobuf_fields(meta)
                        usage_stats_entries = step_meta.get(9, [])
                        for raw_stat in usage_stats_entries:
                            if not isinstance(raw_stat, bytes):
                                continue
                            stat_fields = _parse_protobuf_fields(raw_stat)
                            inp = int(stat_fields.get(2, [0])[0]) if 2 in stat_fields else 0
                            thk = int(stat_fields.get(9, [0])[0]) if 9 in stat_fields else 0
                            out = int(stat_fields.get(10, [0])[0]) if 10 in stat_fields else max(0, int(stat_fields.get(3, [0])[0]) - thk)
                            crd = int(stat_fields.get(5, [0])[0]) if 5 in stat_fields else 0
                            tot = inp + out + thk + crd

                            total_in += inp
                            total_out += out
                            total_thk += thk
                            total_crd += crd
                            total_tot += tot
                            turns += 1

                            latest_turn = TokenUsage(
                                input_tokens=inp,
                                output_tokens=out,
                                thinking_tokens=thk,
                                cache_read_tokens=crd,
                                total_tokens=tot,
                            )
                    conn.close()
                except (sqlite3.Error, OSError):
                    continue
        except OSError:
            pass

    # 2. Fallback: Scan brain transcripts (for test fixtures or text logs)
    if turns == 0:
        brain_dir = gemini_dir / "brain"
        if brain_dir.exists():
            try:
                for log_file in sorted(brain_dir.glob("*/.system_generated/logs/transcript*.jsonl")):
                    try:
                        with log_file.open("r", encoding="utf-8") as handle:
                            for line in handle:
                                if not line.strip():
                                    continue
                                try:
                                    record = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                u = record.get("usage")
                                if isinstance(u, dict):
                                    inp_val = int(u.get("input_tokens", 0) or 0)
                                    out_val = int(u.get("output_tokens", 0) or 0)
                                    thk_val = int(u.get("thinking_tokens", 0) or 0)
                                    crd_val = int(u.get("cache_read_tokens", 0) or 0)
                                    tot_val = int(u.get("total_tokens", inp_val + out_val + thk_val + crd_val) or (inp_val + out_val + thk_val + crd_val))
                                    total_in += inp_val
                                    total_out += out_val
                                    total_thk += thk_val
                                    total_crd += crd_val
                                    total_tot += tot_val
                                    turns += 1
                                    latest_turn = TokenUsage(
                                        input_tokens=inp_val,
                                        output_tokens=out_val,
                                        thinking_tokens=thk_val,
                                        cache_read_tokens=crd_val,
                                        total_tokens=tot_val,
                                    )
                    except OSError:
                        continue
            except OSError:
                pass

    if total_tot == 0 and (total_in + total_out + total_thk + total_crd) > 0:
        total_tot = total_in + total_out + total_thk + total_crd

    cumulative = TokenUsage(
        input_tokens=total_in,
        output_tokens=total_out,
        thinking_tokens=total_thk,
        cache_read_tokens=total_crd,
        total_tokens=total_tot,
    )
    return cumulative, turns, latest_turn


def scan_profile_session_tokens(profile_home: Path) -> TokenUsage:
    """Scans profile directory for recorded conversation transcripts or session databases.

    Aggregates any token fields discovered across past sessions.
    """
    cumulative, _, _ = scan_profile_conversations(profile_home)
    return cumulative


async def fetch_account_tokens_async(
    agy_path: Path,
    profile: Profile,
    semaphore: asyncio.Semaphore,
    cache_manager: CacheManager,
    timeout: float = 30.0,
    force_refresh: bool = False,
    runner: Callable[..., Coroutine[Any, Any, tuple[int, str, str]]] | None = None,
) -> AccountTokenUsage:
    """Fetches or calculates token usage for a single profile."""
    # Check cache first if not forcing refresh
    if not force_refresh:
        cached_entry = cache_manager.get_tokens(profile.name, max_age=TTL_TOKENS_SECONDS)
        if cached_entry is not None:
            data, age_secs, cached_at = cached_entry
            cum_dict = data.get("cumulative", {})
            latest_turn_dict = data.get("latest_snapshot")
            return AccountTokenUsage(
                account=profile.name,
                status="success",
                usage=TokenUsage.from_dict(cum_dict),
                latest_turn=TokenUsage.from_dict(latest_turn_dict) if latest_turn_dict else None,
                cached=True,
                age_seconds=age_secs,
                cached_at=cached_at,
                subscription_date=profile.subscription_date,
                snapshot_count=int(cum_dict.get("snapshot_count", 0)),
            )

    async with semaphore:
        if not profile.home.exists():
            return AccountTokenUsage(
                account=profile.name,
                status="error",
                error=f"profile home directory does not exist: {profile.home}",
                subscription_date=profile.subscription_date,
            )

        turn_usage = TokenUsage()
        if runner is not None:
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
                code, out, err = await asyncio.wait_for(runner(argv, env, timeout), timeout=timeout)
            except asyncio.TimeoutError:
                timeout_str = f"{int(timeout)}s" if timeout.is_integer() else f"{timeout}s"
                return AccountTokenUsage(
                    account=profile.name,
                    status="error",
                    error=f"timed out after {timeout_str}",
                    subscription_date=profile.subscription_date,
                )
            except Exception as exc:
                return AccountTokenUsage(
                    account=profile.name,
                    status="error",
                    error=f"failed to execute agy: {exc}",
                    subscription_date=profile.subscription_date,
                )

            if code != 0:
                err_msg = err.strip() or out.strip()
                first_line = err_msg.splitlines()[0] if err_msg else ""
                detail = f" ({first_line})" if first_line else ""
                return AccountTokenUsage(
                    account=profile.name,
                    status="error",
                    error=f"agy exited with status {code}{detail}",
                    subscription_date=profile.subscription_date,
                )

            try:
                payload = json.loads(out)
                if isinstance(payload, dict):
                    turn_usage = parse_token_usage_payload(payload)
                    cache_manager.set_usage(profile.name, payload, out)
            except (json.JSONDecodeError, ValueError):
                pass

        # Scan conversation SQLite databases (and session logs fallback)
        scanned_usage, scanned_turns, latest_turn = scan_profile_conversations(profile.home)

        now = datetime.now(timezone.utc)
        if scanned_usage.total_tokens > 0:
            cum_data = {
                "input_tokens": scanned_usage.input_tokens,
                "output_tokens": scanned_usage.output_tokens,
                "thinking_tokens": scanned_usage.thinking_tokens,
                "cache_read_tokens": scanned_usage.cache_read_tokens,
                "total_tokens": scanned_usage.total_tokens,
                "snapshot_count": scanned_turns,
            }
            latest_dict = latest_turn.to_dict() if latest_turn else None
            cache_manager.set_tokens(profile.name, cum_data, latest_snapshot=latest_dict, now=now)
            return AccountTokenUsage(
                account=profile.name,
                status="success",
                usage=scanned_usage,
                latest_turn=latest_turn,
                cached=False,
                age_seconds=0.0,
                cached_at=now.isoformat(),
                subscription_date=profile.subscription_date,
                snapshot_count=scanned_turns,
            )
        elif turn_usage.total_tokens > 0:
            snapshot_dict = {
                "input_tokens": turn_usage.input_tokens,
                "output_tokens": turn_usage.output_tokens,
                "thinking_tokens": turn_usage.thinking_tokens,
                "cache_read_tokens": turn_usage.cache_read_tokens,
                "total_tokens": turn_usage.total_tokens,
            }
            updated_ledger = cache_manager.record_token_snapshot(profile.name, snapshot_dict, now=now)
            cum_data = updated_ledger.get("cumulative", {})
            return AccountTokenUsage(
                account=profile.name,
                status="success",
                usage=TokenUsage.from_dict(cum_data),
                latest_turn=turn_usage,
                cached=False,
                age_seconds=0.0,
                cached_at=updated_ledger.get("cached_at"),
                subscription_date=profile.subscription_date,
                snapshot_count=int(cum_data.get("snapshot_count", 0)),
            )
        else:
            cum_data = {
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
                "snapshot_count": 0,
            }
            cache_manager.set_tokens(profile.name, cum_data, now=now)
            return AccountTokenUsage(
                account=profile.name,
                status="success",
                usage=TokenUsage(),
                cached=False,
                age_seconds=0.0,
                cached_at=now.isoformat(),
                subscription_date=profile.subscription_date,
                snapshot_count=0,
            )


async def fetch_all_tokens(
    agy_path: Path,
    profiles: Sequence[Profile],
    cache_manager: CacheManager,
    concurrency_limit: int = 8,
    timeout: float = 30.0,
    force_refresh: bool = False,
    on_progress: Callable[[AccountTokenUsage], None] | None = None,
    runner: Callable[..., Coroutine[Any, Any, tuple[int, str, str]]] | None = None,
) -> list[AccountTokenUsage]:
    if not profiles:
        return []

    sem_limit = max(1, min(len(profiles), concurrency_limit))
    semaphore = asyncio.Semaphore(sem_limit)

    async def _worker(profile: Profile) -> AccountTokenUsage:
        res = await fetch_account_tokens_async(
            agy_path,
            profile,
            semaphore,
            cache_manager,
            timeout=timeout,
            force_refresh=force_refresh,
            runner=runner,
        )
        if on_progress is not None:
            on_progress(res)
        return res

    tasks = [asyncio.create_task(_worker(p)) for p in profiles]
    try:
        return await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        kill_active_subprocesses()
        for t in tasks:
            if not t.done():
                t.cancel()
        raise


# --- Terminal Visualization Engine ---

def render_stacked_bar(
    usage: TokenUsage,
    width: int = 24,
    *,
    use_color: bool = True,
) -> str:
    """Renders a stacked progress bar showing Input / Output / Thinking / Cache distribution."""
    total = usage.total_tokens
    if total <= 0 or width <= 0:
        char = "░" if use_color else "-"
        border = f"{COLOR_DIM}[{COLOR_RESET}" if use_color else "["
        end = f"{COLOR_DIM}]{COLOR_RESET}" if use_color else "]"
        return f"{border}{char * width}{end}"

    # Calculate proportional segments
    raw_in = (usage.input_tokens / total) * width
    raw_out = (usage.output_tokens / total) * width
    raw_thk = (usage.thinking_tokens / total) * width
    raw_crd = (usage.cache_read_tokens / total) * width

    # Allocate integer characters preserving min 1 if non-zero
    counts = [int(raw_in), int(raw_out), int(raw_thk), int(raw_crd)]
    remainders = [
        raw_in - counts[0],
        raw_out - counts[1],
        raw_thk - counts[2],
        raw_crd - counts[3],
    ]

    diff = width - sum(counts)
    if diff > 0:
        # Give remaining slots to largest remainders
        sorted_indices = sorted(range(4), key=lambda i: remainders[i], reverse=True)
        for i in range(diff):
            counts[sorted_indices[i % 4]] += 1

    char = "█" if use_color else "#"
    in_str = char * counts[0]
    out_str = char * counts[1]
    thk_str = char * counts[2]
    crd_str = char * counts[3]

    if not use_color:
        return f"[{in_str}{out_str}{thk_str}{crd_str}]"

    dim = COLOR_DIM
    reset = COLOR_RESET
    return (
        f"{dim}[{reset}"
        f"{COLOR_INPUT}{in_str}{reset}"
        f"{COLOR_OUTPUT}{out_str}{reset}"
        f"{COLOR_THINKING}{thk_str}{reset}"
        f"{COLOR_CACHE}{crd_str}{reset}"
        f"{dim}]{reset}"
    )


def render_comparison_bar(
    value: int,
    max_value: int,
    width: int = 24,
    *,
    use_color: bool = True,
    color: str = COLOR_INPUT,
) -> str:
    """Renders a standard proportional horizontal bar."""
    if max_value <= 0 or value <= 0:
        fill_count = 0
    else:
        fill_count = round((value / max_value) * width)
        fill_count = max(0, min(width, fill_count))
    empty_count = width - fill_count

    filled_char = "█" if use_color else "#"
    empty_char = "░" if use_color else "-"

    if not use_color:
        return f"[{filled_char * fill_count}{empty_char * empty_count}]"

    dim = COLOR_DIM
    reset = COLOR_RESET
    return f"{color}{filled_char * fill_count}{reset}{dim}{empty_char * empty_count}{reset}"


def render_summary_card(
    accounts: Sequence[AccountTokenUsage],
    *,
    use_color: bool = True,
    card_width: int = 76,
) -> list[str]:
    """Renders the top summary statistics card."""
    successful = [a for a in accounts if a.status == "success"]
    total_tokens = sum(a.usage.total_tokens for a in successful)
    total_in = sum(a.usage.input_tokens for a in successful)
    total_out = sum(a.usage.output_tokens for a in successful)
    total_thk = sum(a.usage.thinking_tokens for a in successful)
    total_crd = sum(a.usage.cache_read_tokens for a in successful)

    top_account = max(successful, key=lambda a: a.usage.total_tokens, default=None)
    avg_tokens = (total_tokens / len(successful)) if successful else 0.0

    prompt_total = total_in + total_crd
    cache_eff = ((total_crd / prompt_total) * 100.0) if prompt_total > 0 else 0.0

    cached_count = sum(1 for a in accounts if a.cached)
    live_count = len(accounts) - cached_count

    # Content lines
    title = " Fleet Token Summary "
    top_border = f"┌─{title}" + ("─" * (card_width - len(title) - 2)) + "┐"
    bottom_border = "└" + ("─" * (card_width - 1)) + "┘"

    # Line 1: Total Tokens
    tot_str = format_token_count(total_tokens)
    in_str = format_token_count(total_in)
    out_str = format_token_count(total_out)
    thk_str = format_token_count(total_thk)
    l1_text = f"Total Tokens:     {tot_str} (In: {in_str}  Out: {out_str}  Think: {thk_str})"

    # Line 2: Top Consumer
    if top_account and total_tokens > 0:
        top_pct = (top_account.usage.total_tokens / total_tokens) * 100.0
        top_str = f"{top_account.account} ({format_token_count(top_account.usage.total_tokens)} · {top_pct:.1f}% of fleet)"
    else:
        top_str = "None (0 tokens recorded)"
    l2_text = f"Top Consumer:     {top_str}"

    # Line 3: Average & Cache Efficiency
    avg_str = format_token_count(int(avg_tokens))
    l3_text = f"Fleet Average:    {avg_str} / profile    Cache Hit Ratio: {cache_eff:.1f}% ({format_token_count(total_crd)})"

    # Line 4: Freshness / Cache status
    if cached_count == 0:
        fresh_text = "Freshly scanned"
    elif live_count == 0:
        oldest_age = max((a.age_seconds for a in accounts), default=0.0)
        fresh_text = f"Cached ({format_age(oldest_age)})"
    else:
        oldest_age = max((a.age_seconds for a in accounts if a.cached), default=0.0)
        fresh_text = f"{cached_count} cached ({format_age(oldest_age)}), {live_count} scanned"
    l4_text = f"Data Status:      {fresh_text}"

    raw_lines = [l1_text, l2_text, l3_text, l4_text]
    min_needed = max(len(l) for l in raw_lines) + 4
    actual_width = max(card_width, min_needed)
    inner_width = actual_width - 3  # "│ " + content + " │"
    top_border = f"┌─{title}" + ("─" * (actual_width - len(title) - 2)) + "┐"
    bottom_border = "└" + ("─" * (actual_width - 1)) + "┘"

    formatted_card: list[str] = []
    if use_color:
        formatted_card.append(f"{COLOR_DIM}{top_border}{COLOR_RESET}")
        for line in raw_lines:
            # Highlight labels
            colon_idx = line.find(":")
            if colon_idx != -1:
                label = line[: colon_idx + 1]
                val = line[colon_idx + 1 :]
                content = f"{COLOR_BOLD}{label}{COLOR_RESET}{val}"
                pad = inner_width - len(line)
                pad_str = " " * max(0, pad)
                formatted_card.append(f"{COLOR_DIM}│{COLOR_RESET} {content}{pad_str} {COLOR_DIM}│{COLOR_RESET}")
            else:
                pad = inner_width - len(line)
                pad_str = " " * max(0, pad)
                formatted_card.append(f"{COLOR_DIM}│{COLOR_RESET} {line}{pad_str} {COLOR_DIM}│{COLOR_RESET}")
        formatted_card.append(f"{COLOR_DIM}{bottom_border}{COLOR_RESET}")
    else:
        formatted_card.append(top_border)
        for line in raw_lines:
            pad = inner_width - len(line)
            pad_str = " " * max(0, pad)
            formatted_card.append(f"│ {line}{pad_str} │")
        formatted_card.append(bottom_border)

    return formatted_card


def render_composition_breakdown_table(
    accounts: Sequence[AccountTokenUsage],
    *,
    use_color: bool = True,
) -> list[str]:
    """Renders a compact, beautifully aligned Unicode table showing token breakdown per profile."""
    successful = [a for a in accounts if a.status == "success"]
    name_w = max(10, max([len(a.account) for a in accounts], default=10))
    col_w = 7

    dim = COLOR_DIM if use_color else ""
    reset = COLOR_RESET if use_color else ""
    bold = COLOR_BOLD if use_color else ""
    col_in = COLOR_INPUT if use_color else ""
    col_out = COLOR_OUTPUT if use_color else ""
    col_thk = COLOR_THINKING if use_color else ""
    col_crd = COLOR_CACHE if use_color else ""
    col_cyan = COLOR_CYAN if use_color else ""
    col_red = COLOR_RED if use_color else ""

    h_name = "─" * name_w
    h_col = "─" * col_w
    cols_top = "─┬─".join([h_col] * 6)
    cols_mid = "─┼─".join([h_col] * 6)
    cols_bot = "─┴─".join([h_col] * 6)

    top = f"{dim}┌─{h_name}─┬─{cols_top}─┐{reset}"
    mid = f"{dim}├─{h_name}─┼─{cols_mid}─┤{reset}"
    bot = f"{dim}└─{h_name}─┴─{cols_bot}─┘{reset}"

    sep = f" {dim}│{reset} "
    left_b = f"{dim}│{reset} "
    right_b = f" {dim}│{reset}"

    title = f"{bold}Token Composition Breakdown:{reset}"
    lines = [title, top]

    p_lbl = "Profile"
    tot_lbl = "Total"
    in_lbl = "Input"
    out_lbl = "Output"
    thk_lbl = "Think"
    crd_lbl = "Cache"
    hit_lbl = "Hit %"

    hdr = (
        f"{left_b}{bold}{p_lbl:<{name_w}}{reset}{sep}"
        f"{bold}{tot_lbl:>{col_w}}{reset}{sep}"
        f"{col_in}{in_lbl:>{col_w}}{reset}{sep}"
        f"{col_out}{out_lbl:>{col_w}}{reset}{sep}"
        f"{col_thk}{thk_lbl:>{col_w}}{reset}{sep}"
        f"{col_crd}{crd_lbl:>{col_w}}{reset}{sep}"
        f"{col_cyan}{hit_lbl:>{col_w}}{reset}{right_b}"
    )
    lines.append(hdr)
    lines.append(mid)

    inner_err_w = 6 * col_w + 5 * 3

    for a in accounts:
        if a.status != "success":
            err_msg = f"✗ Failed: {a.error or 'unknown error'}"
            if len(err_msg) > inner_err_w:
                err_msg = err_msg[: inner_err_w - 3] + "..."
            err_disp = f"{col_red}{err_msg:<{inner_err_w}}{reset}"
            lines.append(f"{left_b}{a.account:<{name_w}}{sep}{err_disp}{right_b}")
            continue

        tot_s = format_token_count(a.usage.total_tokens)
        inp_s = format_token_count(a.usage.input_tokens)
        out_s = format_token_count(a.usage.output_tokens)
        thk_s = format_token_count(a.usage.thinking_tokens)
        crd_s = format_token_count(a.usage.cache_read_tokens)
        hit_s = f"{a.usage.cache_efficiency:.1f}%"

        row = (
            f"{left_b}{a.account:<{name_w}}{sep}"
            f"{bold}{tot_s:>{col_w}}{reset}{sep}"
            f"{col_in}{inp_s:>{col_w}}{reset}{sep}"
            f"{col_out}{out_s:>{col_w}}{reset}{sep}"
            f"{col_thk}{thk_s:>{col_w}}{reset}{sep}"
            f"{col_crd}{crd_s:>{col_w}}{reset}{sep}"
            f"{col_cyan}{hit_s:>{col_w}}{reset}{right_b}"
        )
        lines.append(row)

    if len(accounts) > 1:
        lines.append(mid)
        fl_tot = sum(a.usage.total_tokens for a in successful)
        fl_inp = sum(a.usage.input_tokens for a in successful)
        fl_out = sum(a.usage.output_tokens for a in successful)
        fl_thk = sum(a.usage.thinking_tokens for a in successful)
        fl_crd = sum(a.usage.cache_read_tokens for a in successful)
        p_tot = fl_inp + fl_crd
        fl_hit = f"{(fl_crd / p_tot * 100.0):.1f}%" if p_tot > 0 else "0.0%"

        fl_lbl = "Fleet Total"
        tot_row = (
            f"{left_b}{bold}{fl_lbl:<{name_w}}{reset}{sep}"
            f"{bold}{format_token_count(fl_tot):>{col_w}}{reset}{sep}"
            f"{col_in}{format_token_count(fl_inp):>{col_w}}{reset}{sep}"
            f"{col_out}{format_token_count(fl_out):>{col_w}}{reset}{sep}"
            f"{col_thk}{format_token_count(fl_thk):>{col_w}}{reset}{sep}"
            f"{col_crd}{format_token_count(fl_crd):>{col_w}}{reset}{sep}"
            f"{col_cyan}{fl_hit:>{col_w}}{reset}{right_b}"
        )
        lines.append(tot_row)

    lines.append(bot)
    return lines


def render_token_dashboard(
    accounts: Sequence[AccountTokenUsage],
    *,
    breakdown: bool = False,
    use_color: bool = True,
) -> list[str]:
    """Generates terminal output lines for agym tokens."""
    if not accounts:
        return ["No profiles configured."]

    # Determine dimensions
    term_cols = shutil.get_terminal_size((80, 24)).columns
    card_width = min(84, max(64, term_cols - 2))
    name_col_width = max(10, max([len(a.account) for a in accounts], default=10))

    lines: list[str] = []

    # Title
    title = "Antigravity Token Usage"
    if use_color:
        lines.append(f"{COLOR_BOLD}{title}{COLOR_RESET}")
    else:
        lines.append(title)
    lines.append("")

    # 1. Summary Card
    lines.extend(render_summary_card(accounts, use_color=use_color, card_width=card_width))
    lines.append("")

    # 2. Fleet Relative Comparison Chart
    lines.append(f"{COLOR_BOLD}Fleet Volume Comparison:{COLOR_RESET}" if use_color else "Fleet Volume Comparison:")
    max_tokens = max((a.usage.total_tokens for a in accounts if a.status == "success"), default=0)
    fleet_total = sum(a.usage.total_tokens for a in accounts if a.status == "success")

    bar_width = min(36, max(18, card_width - name_col_width - 32))

    for a in accounts:
        if a.status != "success":
            err_msg = a.error or "unknown error"
            err_disp = f"{COLOR_RED}✗ Failed: {err_msg}{COLOR_RESET}" if use_color else f"✗ Failed: {err_msg}"
            lines.append(f"  {a.account:<{name_col_width}}  {err_disp}")
            continue

        bar = render_comparison_bar(
            a.usage.total_tokens,
            max_tokens,
            width=bar_width,
            use_color=use_color,
            color=COLOR_INPUT,
        )
        tot_str = f"{format_token_count(a.usage.total_tokens):>7}"
        pct_str = f"{(a.usage.total_tokens / fleet_total * 100.0):.1f}%" if fleet_total > 0 else "0.0%"

        if use_color:
            tot_disp = f"{COLOR_BOLD}{tot_str}{COLOR_RESET}"
            pct_disp = f"{COLOR_DIM}({pct_str:>5}){COLOR_RESET}"
        else:
            tot_disp = tot_str
            pct_disp = f"({pct_str:>5})"

        lines.append(f"  {a.account:<{name_col_width}}  {bar}  {tot_disp} {pct_disp}")

    # 3. Composition Breakdown (only displayed when requested via --breakdown)
    if breakdown:
        lines.append("")
        lines.extend(render_composition_breakdown_table(accounts, use_color=use_color))

    return lines


# --- Main Runner ---

async def run_tokens(
    agy_path: Path,
    profiles: Sequence[Profile],
    cache_manager: CacheManager | None = None,
    *,
    json_mode: bool = False,
    breakdown: bool = False,
    refresh: bool = False,
    timeout: float = 30.0,
    concurrency_limit: int = 8,
    is_tty: bool | None = None,
    stdout: Any = None,
    runner: Callable[..., Coroutine[Any, Any, tuple[int, str, str]]] | None = None,
) -> list[AccountTokenUsage]:
    """Executes token usage tracking across profiles and renders JSON or visual dashboard."""
    out = sys.stdout if stdout is None else stdout
    tty_mode = sys.stdout.isatty() if is_tty is None else is_tty
    cm = cache_manager if cache_manager is not None else CacheManager()

    if not profiles:
        if json_mode:
            out.write(json.dumps({"summary": {}, "accounts": []}, indent=2) + "\n")
            out.flush()
        else:
            out.write("No profiles configured. Run 'agym setup <profile>' first.\n")
            out.flush()
        return []

    # Check if all can be served from cache
    need_scan = refresh
    if not refresh:
        for p in profiles:
            if cm.get_tokens(p.name, max_age=TTL_TOKENS_SECONDS) is None:
                need_scan = True
                break

    if json_mode:
        usages = await fetch_all_tokens(
            agy_path,
            profiles,
            cm,
            concurrency_limit=concurrency_limit,
            timeout=timeout,
            force_refresh=refresh,
            runner=runner,
        )
        successful = [u for u in usages if u.status == "success"]
        tot_tok = sum(u.usage.total_tokens for u in successful)
        tot_in = sum(u.usage.input_tokens for u in successful)
        tot_out = sum(u.usage.output_tokens for u in successful)
        tot_thk = sum(u.usage.thinking_tokens for u in successful)
        tot_crd = sum(u.usage.cache_read_tokens for u in successful)
        top_u = max(successful, key=lambda u: u.usage.total_tokens, default=None)
        p_tot = tot_in + tot_crd

        payload = {
            "summary": {
                "total_tokens": tot_tok,
                "input_tokens": tot_in,
                "output_tokens": tot_out,
                "thinking_tokens": tot_thk,
                "cache_read_tokens": tot_crd,
                "cache_efficiency": round((tot_crd / p_tot * 100.0), 2) if p_tot > 0 else 0.0,
                "top_consumer": top_u.account if top_u else None,
                "average_tokens": round(tot_tok / len(successful), 1) if successful else 0.0,
                "account_count": len(usages),
            },
            "accounts": [u.to_dict() for u in usages],
        }
        out.write(json.dumps(payload, indent=2) + "\n")
        out.flush()
        return usages

    # Interactive progressive rendering if querying live with an external runner
    if need_scan and tty_mode and runner is not None:
        ui = ProgressiveUsageUI(profiles, is_tty=tty_mode, stdout=out)
        spinner_task = asyncio.create_task(ui.spinner_loop())
        usages_result: list[AccountTokenUsage] = []
        try:
            usages_result = await fetch_all_tokens(
                agy_path,
                profiles,
                cm,
                concurrency_limit=concurrency_limit,
                timeout=timeout,
                force_refresh=refresh,
                on_progress=lambda _: None,
                runner=runner,
            )
        finally:
            ui._stop_event.set()
            await spinner_task
            if ui.last_lines_count > 0:
                out.write(f"\033[{ui.last_lines_count}A\r")
                for _ in range(ui.last_lines_count):
                    out.write("\033[2K\n")
                out.write(f"\033[{ui.last_lines_count}A\r")
                out.flush()
    else:
        usages_result = await fetch_all_tokens(
            agy_path,
            profiles,
            cm,
            concurrency_limit=concurrency_limit,
            timeout=timeout,
            force_refresh=refresh,
            runner=runner,
        )

    dashboard_lines = render_token_dashboard(usages_result, breakdown=breakdown, use_color=tty_mode)
    out.write("\n".join(dashboard_lines) + "\n")
    out.flush()
    return usages_result
