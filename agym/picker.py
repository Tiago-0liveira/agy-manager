from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .cache import USAGE_CACHE_TTL_SECONDS
from .usage import AccountUsage, extract_quota_bucket, format_colored_bar, parse_iso_datetime


@dataclass(frozen=True)
class AccountPickerItem:
    account_name: str
    limit_5h: str
    reset_time: str
    raw_quota_sort_key: float
    raw_reset_timestamp: float | None
    remaining_fraction: float
    status: str
    error: str | None = None
    formatted_line: str = ""
    formatted_line_plain: str = ""


def _extract_5h_bucket(usage: AccountUsage) -> Any:
    """Finds the 5h quota bucket for an account, prioritizing Gemini then Claude/GPT."""
    b = extract_quota_bucket(usage, "gemini", "5h")
    if b is not None:
        return b
    b = extract_quota_bucket(usage, "claude", "5h")
    if b is not None:
        return b
    for group in usage.groups:
        for bucket in group.buckets:
            w = bucket.window.lower()
            b_id = bucket.id.lower()
            if "5h" in w or "5h" in b_id:
                return bucket
    return None


def _format_relative_duration(dt: datetime | None, now: datetime | None = None) -> str:
    """Formats time remaining until dt into relative duration string (e.g. 1h 45m, 32m, Ready)."""
    if dt is None:
        return "Ready"
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    delta = dt - now
    total_secs = int(delta.total_seconds())
    if total_secs <= 0:
        return "Ready"

    days = total_secs // 86400
    rem = total_secs % 86400
    hours = rem // 3600
    rem = rem % 3600
    mins = rem // 60

    if days > 0 and hours > 0:
        return f"{days}d {hours}h"
    if days > 0:
        return f"{days}d"
    if hours > 0 and mins > 0:
        return f"{hours}h {mins}m"
    if hours > 0:
        return f"{hours}h"
    if mins > 0:
        return f"{mins}m"
    return "<1m"


def _get_color_for_fraction(fraction: float, status: str = "success") -> str:
    """Returns ANSI color code based on quota availability (>50% green, 15-50% yellow, <15% red, dim for unknown)."""
    if status == "error":
        return "\033[31m"  # Red for errors
    if status == "unknown" or fraction < 0.0:
        return "\033[90m"  # Dim grey for unknown
    pct = fraction * 100.0
    if pct > 50.0:
        return "\033[32m"  # Green
    if pct >= 15.0:
        return "\033[33m"  # Yellow
    return "\033[31m"      # Red


def prepare_accounts_for_picker(
    raw_usage_data: Sequence[AccountUsage | dict[str, Any]] | dict[str, Any],
    *,
    now: datetime | None = None,
    use_color: bool = True,
) -> list[AccountPickerItem]:
    """Parses, color-codes, and sorts accounts strictly for the interactive picker.

    Extracts strictly:
      - Account / profile name
      - 5-hour limit / quota status
      - Time remaining until reset

    Sorting rules:
      1. Primary: Remaining 5-hour quota descending (highest available quota at the top)
      2. Secondary: Earliest reset time ascending (accounts resetting soonest appear higher)
      3. Tertiary: Alphabetical by account name (case-insensitive)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    if isinstance(raw_usage_data, dict):
        if "accounts" in raw_usage_data and isinstance(raw_usage_data["accounts"], list):
            items_raw = raw_usage_data["accounts"]
        else:
            items_raw = list(raw_usage_data.values())
    else:
        items_raw = list(raw_usage_data)

    items: list[AccountPickerItem] = []

    for elem in items_raw:
        if isinstance(elem, AccountUsage):
            name = elem.account
            status = elem.status
            err = elem.error
            bucket = _extract_5h_bucket(elem) if status == "success" else None
        elif isinstance(elem, dict):
            name = str(elem.get("account", elem.get("name", "unknown")))
            status = str(elem.get("status", "success"))
            err = elem.get("error")
            bucket = None
            if status == "success":
                raw_groups = elem.get("groups", [])
                for g in raw_groups:
                    if not isinstance(g, dict):
                        continue
                    g_name = str(g.get("name", "")).lower()
                    for b in g.get("buckets", []):
                        if not isinstance(b, dict):
                            continue
                        w = str(b.get("window", "")).lower()
                        b_id = str(b.get("id", "")).lower()
                        if "5h" in w or "5h" in b_id:
                            # Reconstruct minimal bucket structure
                            raw_frac = b.get("remaining_fraction", 0.0)
                            try:
                                f_val = float(raw_frac)
                            except (ValueError, TypeError):
                                f_val = 0.0
                            reset_raw = b.get("reset_time")
                            reset_dt = parse_iso_datetime(str(reset_raw)) if reset_raw else None
                            bucket = type(
                                "TempBucket",
                                (),
                                {
                                    "remaining_fraction": f_val,
                                    "reset_time": reset_dt,
                                    "percentage": round(f_val * 100),
                                },
                            )()
                            break
                    if bucket is not None:
                        break
        else:
            continue

        if status == "success" and bucket is not None:
            fraction = max(0.0, min(1.0, float(bucket.remaining_fraction)))
            pct = round(fraction * 100)
            raw_quota_sort_key = fraction
            reset_dt = bucket.reset_time
            if fraction >= 1.0 or reset_dt is None:
                reset_time_str = "Ready"
                raw_reset_timestamp = None
            else:
                raw_reset_timestamp = reset_dt.timestamp()
                reset_time_str = _format_relative_duration(reset_dt, now=now)

            limit_5h_text = f"{pct:3d}%"
            limit_5h_colored = f"{format_colored_bar(fraction, width=5, use_color=True)} {limit_5h_text}"
            limit_5h_plain = f"{format_colored_bar(fraction, width=5, use_color=False)} {limit_5h_text}"
        elif status == "unknown" or (status == "success" and bucket is None):
            status = "unknown"
            fraction = -1.0
            raw_quota_sort_key = -1.0
            raw_reset_timestamp = float("inf")
            reset_time_str = "-"
            limit_5h_text = "Unknown"
            limit_5h_colored = f"\033[90m{'Unknown':<12}\033[0m"
            limit_5h_plain = f"{'Unknown':<12}"
        else:
            fraction = -2.0
            raw_quota_sort_key = -2.0
            raw_reset_timestamp = float("inf")
            reset_time_str = "-"
            limit_5h_text = "Error"
            limit_5h_colored = f"\033[31m{'Error':<12}\033[0m"
            limit_5h_plain = f"{'Error':<12}"

        color = _get_color_for_fraction(fraction, status=status)
        reset = "\033[0m"

        # Dot
        dot_colored = f"{color}●{reset}"
        dot_plain = "●"

        # Fixed-width Account Name (20 chars)
        if len(name) > 20:
            disp_name = name[:19] + "…"
        else:
            disp_name = f"{name:<20}"

        # Fixed-width 5h Quota (12 chars)
        quota_colored = limit_5h_colored
        quota_plain = limit_5h_plain

        # Fixed-width Reset in (10 chars)
        reset_col = f"{reset_time_str:<10}"

        line_colored = f"[{dot_colored}] {disp_name}  |  5h: {quota_colored}  |  Reset in: {reset_col}"
        line_plain = f"[{dot_plain}] {disp_name}  |  5h: {quota_plain}  |  Reset in: {reset_col}"

        items.append(
            AccountPickerItem(
                account_name=name,
                limit_5h=limit_5h_text,
                reset_time=reset_time_str,
                raw_quota_sort_key=raw_quota_sort_key,
                raw_reset_timestamp=raw_reset_timestamp,
                remaining_fraction=fraction,
                status=status,
                error=err,
                formatted_line=line_colored if use_color else line_plain,
                formatted_line_plain=line_plain,
            )
        )

    # Sort strictly by:
    # 1. Quota availability descending (-raw_quota_sort_key)
    # 2. Earliest reset timestamp ascending (None/Ready or infinite last)
    # 3. Account name ascending
    def _sort_key(item: AccountPickerItem) -> tuple[float, float, str]:
        reset_val = item.raw_reset_timestamp if item.raw_reset_timestamp is not None else float("inf")
        return (-item.raw_quota_sort_key, reset_val, item.account_name.lower())

    return sorted(items, key=_sort_key)


def _read_single_key(fd: int) -> str:
    """Reads a single keypress or ANSI escape sequence on POSIX."""
    try:
        ch = os.read(fd, 1).decode("utf-8", errors="replace")
    except OSError:
        return ""

    if ch == "\x1b":
        import select

        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                ch2 = os.read(fd, 1).decode("utf-8", errors="replace")
                if ch2 == "[":
                    ch3 = os.read(fd, 1).decode("utf-8", errors="replace")
                    return f"\x1b[{ch3}"
                if ch2 == "O":
                    ch3 = os.read(fd, 1).decode("utf-8", errors="replace")
                    return f"\x1bO{ch3}"
                return f"\x1b{ch2}"
            except OSError:
                return "\x1b"
        return "\x1b"
    return ch


class AccountPicker:
    """Terminal UI component for interactive account selection with arrow keys and Enter."""

    def __init__(
        self,
        items: list[AccountPickerItem],
        *,
        stdout: Any = None,
        stdin: Any = None,
        use_color: bool | None = None,
        key_reader: Callable[[], str] | None = None,
    ) -> None:
        self.items = list(items)
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stdin = stdin if stdin is not None else sys.stdin
        self.is_tty = hasattr(self.stdout, "isatty") and self.stdout.isatty()
        self.use_color = self.is_tty if use_color is None else use_color
        self.key_reader = key_reader
        self.selected_index = 0
        self.last_lines_count = 0

    def _safe_write(self, text: str) -> None:
        try:
            self.stdout.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.stdout, "encoding", None) or "ascii"
            safe_text = text.encode(encoding, errors="replace").decode(encoding)
            self.stdout.write(safe_text)
        self.stdout.flush()

    def _render_menu_lines(self) -> list[str]:
        lines: list[str] = [
            "Select an account (↑/↓ to navigate, Enter to select, q to cancel):",
        ]
        for i, item in enumerate(self.items):
            line_content = item.formatted_line if self.use_color else item.formatted_line_plain
            if i == self.selected_index:
                indicator = "\033[1;36m>\033[0m " if self.use_color else "> "
                lines.append(f"{indicator}{line_content}")
            else:
                lines.append(f"  {line_content}")
        return lines

    def _draw_frame(self) -> None:
        lines = self._render_menu_lines()
        out: list[str] = []
        if self.last_lines_count > 0:
            out.append(f"\033[{self.last_lines_count}A\r")
        for line in lines:
            out.append(f"\033[2K{line}\n")
        self._safe_write("".join(out))
        self.last_lines_count = len(lines)

    def _clear_menu(self) -> None:
        if self.last_lines_count > 0:
            out: list[str] = [f"\033[{self.last_lines_count}A\r"]
            for _ in range(self.last_lines_count):
                out.append("\033[2K\n")
            out.append(f"\033[{self.last_lines_count}A\r")
            self._safe_write("".join(out))
            self.last_lines_count = 0

    def pick(self) -> str | None:
        """Runs the interactive selector loop. Returns selected account name or None on cancel."""
        if not self.items:
            return None

        # If a custom key reader is provided (e.g. for deterministic unit testing)
        if self.key_reader is not None:
            self._draw_frame()
            while True:
                key = self.key_reader()
                if key in ("\r", "\n"):
                    self._clear_menu()
                    return self.items[self.selected_index].account_name
                if key in ("q", "Q", "\x1b", "\x03"):
                    self._clear_menu()
                    return None
                if key in ("\x1b[A", "\x1bOA", "k", "K"):
                    self.selected_index = (self.selected_index - 1) % len(self.items)
                    self._draw_frame()
                elif key in ("\x1b[B", "\x1bOB", "j", "J"):
                    self.selected_index = (self.selected_index + 1) % len(self.items)
                    self._draw_frame()

        # Check if running in a real interactive terminal
        is_interactive = hasattr(self.stdin, "isatty") and self.stdin.isatty() and self.is_tty
        if not is_interactive:
            # Non-interactive fallback: select top-ranked account
            return self.items[0].account_name

        if sys.platform == "win32":
            import msvcrt

            def _win_read_key() -> str:
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    ch2 = msvcrt.getwch()
                    if ch2 == "H":
                        return "\x1b[A"
                    if ch2 == "P":
                        return "\x1b[B"
                    return ""
                return ch

            read_fn = _win_read_key
            restore_termios = None
        else:
            import termios
            import tty

            fd = self.stdin.fileno()
            old_settings = termios.tcgetattr(fd)

            def restore_termios() -> None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

            tty.setcbreak(fd)
            read_fn = lambda: _read_single_key(fd)

        # Hide cursor
        self._safe_write("\033[?25l")
        try:
            self._draw_frame()
            while True:
                key = read_fn()
                if key in ("\r", "\n"):
                    self._clear_menu()
                    return self.items[self.selected_index].account_name
                if key in ("q", "Q", "\x1b", "\x03"):
                    self._clear_menu()
                    return None
                if key in ("\x1b[A", "\x1bOA", "k", "K"):
                    self.selected_index = (self.selected_index - 1) % len(self.items)
                    self._draw_frame()
                elif key in ("\x1b[B", "\x1bOB", "j", "J"):
                    self.selected_index = (self.selected_index + 1) % len(self.items)
                    self._draw_frame()
        except KeyboardInterrupt:
            self._clear_menu()
            return None
        finally:
            if restore_termios is not None:
                restore_termios()
            self._safe_write("\033[?25h")


def run_picker(
    items: list[AccountPickerItem],
    *,
    stdout: Any = None,
    stdin: Any = None,
    use_color: bool | None = None,
    key_reader: Callable[[], str] | None = None,
) -> str | None:
    """Convenience helper to create and run an AccountPicker."""
    picker = AccountPicker(
        items,
        stdout=stdout,
        stdin=stdin,
        use_color=use_color,
        key_reader=key_reader,
    )
    return picker.pick()
