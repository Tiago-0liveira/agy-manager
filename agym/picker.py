from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .usage import (
    AccountUsage,
    UsageBucket,
    UsageGroup,
    extract_quota_bucket,
    format_short_reset_time,
    parse_iso_datetime,
)
from .usage_graphs import format_quota_cell_simple


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
    open_sessions: int = 0
    weekly_remaining_fraction: float = 0.0


def prepare_accounts_for_picker(
    raw_usage_data: Sequence[AccountUsage | dict[str, Any]] | dict[str, Any],
    *,
    session_counts: dict[str, int] | None = None,
    now: datetime | None = None,
    use_color: bool = True,
) -> list[AccountPickerItem]:
    """Parses, formats, and sorts accounts for the interactive picker.

    Reuses shared quota bucket extraction, reset time formatting, and cell
    renderers from usage / usage_graphs.

    Sorting rules:
      1. Gemini 5h remaining — descending
      2. Gemini weekly remaining — descending
      3. open sessions — ascending
      4. 5h reset — ascending
      5. profile name (case-insensitive)
    """
    if isinstance(raw_usage_data, dict):
        if "accounts" in raw_usage_data and isinstance(raw_usage_data["accounts"], list):
            items_raw = raw_usage_data["accounts"]
        else:
            items_raw = list(raw_usage_data.values())
    else:
        items_raw = list(raw_usage_data)

    parsed_usages: list[AccountUsage] = []
    for elem in items_raw:
        if isinstance(elem, AccountUsage):
            parsed_usages.append(elem)
        elif isinstance(elem, dict):
            name = str(elem.get("account", elem.get("name", "unknown")))
            status = str(elem.get("status", "success"))
            err = elem.get("error")
            groups: list[UsageGroup] = []
            for g in elem.get("groups", []):
                if not isinstance(g, dict):
                    continue
                buckets: list[UsageBucket] = []
                for b in g.get("buckets", []):
                    if not isinstance(b, dict):
                        continue
                    raw_frac = b.get("remaining_fraction", 0.0)
                    try:
                        f_val = float(raw_frac)
                    except (ValueError, TypeError):
                        f_val = 0.0
                    reset_raw = b.get("reset_time")
                    reset_dt = parse_iso_datetime(str(reset_raw)) if reset_raw else None
                    buckets.append(
                        UsageBucket(
                            id=str(b.get("id", "")),
                            name=str(b.get("name", "")),
                            window=str(b.get("window", "")),
                            remaining_fraction=f_val,
                            reset_time=reset_dt,
                            reset_time_raw=str(reset_raw) if reset_raw else None,
                        )
                    )
                groups.append(UsageGroup(name=str(g.get("name", "")), description=g.get("description"), buckets=buckets))
            parsed_usages.append(
                AccountUsage(
                    account=name,
                    status=status,
                    error=err,
                    groups=groups,
                )
            )
        else:
            continue

    acc_width = max(15, max([len(u.account) for u in parsed_usages], default=15))
    sess_counts = session_counts if session_counts is not None else {}
    items: list[AccountPickerItem] = []

    reset_fn = (lambda dt: format_short_reset_time(dt, now=now)) if now is not None else format_short_reset_time

    for u in parsed_usages:
        name = u.account
        status = u.status
        err = u.error
        sess_num = sess_counts.get(name, 0)
        sess_cell = f"{sess_num:<7}"

        b_g5 = extract_quota_bucket(u, "gemini", "5h") if status == "success" else None
        b_gw = extract_quota_bucket(u, "gemini", "week") if status == "success" else None

        if status == "success" and b_g5 is not None:
            g5_frac = max(0.0, min(1.0, float(b_g5.remaining_fraction)))
            reset_dt = b_g5.reset_time
            if g5_frac >= 1.0 or reset_dt is None:
                reset_time_str = "Ready"
                raw_reset_timestamp = None
            else:
                raw_reset_timestamp = reset_dt.timestamp()
                reset_time_str = reset_fn(reset_dt)
            limit_5h_text = f"{b_g5.percentage:3d}%"

            if b_gw is not None:
                gw_frac = max(0.0, min(1.0, float(b_gw.remaining_fraction)))
            else:
                gw_frac = -1.0

            g5_colored = format_quota_cell_simple(b_g5, prefix="5h: ", use_color=True, format_short_reset_fn=reset_fn)
            g5_plain = format_quota_cell_simple(b_g5, prefix="5h: ", use_color=False, format_short_reset_fn=reset_fn)

            gw_colored = format_quota_cell_simple(b_gw, prefix="Wk: ", use_color=True, format_short_reset_fn=reset_fn)
            gw_plain = format_quota_cell_simple(b_gw, prefix="Wk: ", use_color=False, format_short_reset_fn=reset_fn)

            line_colored = f"{name:<{acc_width}}{sess_cell}{g5_colored}  {gw_colored}"
            line_plain = f"{name:<{acc_width}}{sess_cell}{g5_plain}  {gw_plain}"
        elif status == "unknown" or (status == "success" and b_g5 is None):
            status = "unknown"
            g5_frac = -1.0
            gw_frac = -1.0
            reset_time_str = "-"
            raw_reset_timestamp = float("inf")
            limit_5h_text = "Unknown"
            g5_colored = format_quota_cell_simple(None, prefix="5h: ", use_color=True)
            g5_plain = format_quota_cell_simple(None, prefix="5h: ", use_color=False)
            gw_colored = format_quota_cell_simple(None, prefix="Wk: ", use_color=True)
            gw_plain = format_quota_cell_simple(None, prefix="Wk: ", use_color=False)
            line_colored = f"{name:<{acc_width}}{sess_cell}{g5_colored}  {gw_colored}"
            line_plain = f"{name:<{acc_width}}{sess_cell}{g5_plain}  {gw_plain}"
        else:
            status = "error"
            g5_frac = -2.0
            gw_frac = -2.0
            reset_time_str = "-"
            raw_reset_timestamp = float("inf")
            limit_5h_text = "Error"
            failed_text = f"Failed: {err}" if err else "Failed"
            quota_area = 50
            if len(failed_text) > quota_area:
                failed_text = failed_text[: quota_area - 3] + "..."
            disp_err_colored = f"\033[31m{failed_text:<{quota_area}}\033[0m"
            disp_err_plain = f"{failed_text:<{quota_area}}"
            line_colored = f"{name:<{acc_width}}{sess_cell}{disp_err_colored}"
            line_plain = f"{name:<{acc_width}}{sess_cell}{disp_err_plain}"

        items.append(
            AccountPickerItem(
                account_name=name,
                limit_5h=limit_5h_text,
                reset_time=reset_time_str,
                raw_quota_sort_key=g5_frac,
                raw_reset_timestamp=raw_reset_timestamp,
                remaining_fraction=g5_frac,
                status=status,
                error=err,
                formatted_line=line_colored if use_color else line_plain,
                formatted_line_plain=line_plain,
                open_sessions=sess_num,
                weekly_remaining_fraction=gw_frac,
            )
        )

    def _sort_key(item: AccountPickerItem) -> tuple[float, float, int, float, str]:
        reset_val = item.raw_reset_timestamp if item.raw_reset_timestamp is not None else float("inf")
        return (
            -item.raw_quota_sort_key,
            -item.weekly_remaining_fraction,
            item.open_sessions,
            reset_val,
            item.account_name.lower(),
        )

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
        freshness_summary: str | None = None,
        stdout: Any = None,
        stdin: Any = None,
        use_color: bool | None = None,
        key_reader: Callable[[], str] | None = None,
    ) -> None:
        self.items = list(items)
        self.freshness_summary = freshness_summary
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
        lines: list[str] = []
        if self.freshness_summary:
            lines.append(self.freshness_summary)
            lines.append("")

        acc_width = max(15, max([len(item.account_name) for item in self.items], default=15))
        bold = "\033[1m" if self.use_color else ""
        reset = "\033[0m" if self.use_color else ""
        header = f"{bold}{'Account':<{acc_width + 2}}{'Sess':<7}{'Gemini 5h':<26}Gemini Wk{reset}"
        lines.append(header)

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
    freshness_summary: str | None = None,
    stdout: Any = None,
    stdin: Any = None,
    use_color: bool | None = None,
    key_reader: Callable[[], str] | None = None,
) -> str | None:
    """Convenience helper to create and run an AccountPicker."""
    picker = AccountPicker(
        items,
        freshness_summary=freshness_summary,
        stdout=stdout,
        stdin=stdin,
        use_color=use_color,
        key_reader=key_reader,
    )
    return picker.pick()
