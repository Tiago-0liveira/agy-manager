from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .profiles import Profile, ProfileStore, _chmod_private_dir, _default_data_root

# ANSI escape codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[38;5;39m"
PURPLE = "\033[38;5;141m"
BLUE = "\033[38;5;75m"
GRAY = "\033[38;5;245m"
DARK_GRAY = "\033[38;5;239m"
SEP = f"{DARK_GRAY}│{RESET}"

COLOR_RANKS: list[tuple[float, str]] = [
    (90.0, "\033[38;5;48m"),   # Rank 1: Bright Emerald Green (>= 90%)
    (75.0, "\033[38;5;40m"),   # Rank 2: Green (75% - 89.99%)
    (50.0, "\033[38;5;184m"),  # Rank 3: Yellow-Green (50% - 74.99%)
    (25.0, "\033[38;5;214m"),  # Rank 4: Amber / Warm Yellow (25% - 49.99%)
    (10.0, "\033[38;5;208m"),  # Rank 5: Orange (10% - 24.99%)
    (0.0,  "\033[38;5;196m"),  # Rank 6: Red (< 10%)
]


def get_rank_color(percentage: float | int | None, no_color: bool = False) -> str:
    if no_color or percentage is None:
        return ""
    val = float(percentage)
    for threshold, code in COLOR_RANKS:
        if val >= threshold:
            return code
    return COLOR_RANKS[-1][1]


def format_mini_bar(percentage: int | None, width: int = 6, no_color: bool = False) -> str:
    if percentage is None:
        return ""
    pct = max(0, min(100, percentage))
    filled = round((pct / 100.0) * width)
    empty = width - filled
    color = get_rank_color(pct, no_color=no_color)
    reset = RESET if not no_color else ""
    return f"{color}[{'█' * filled}{'░' * empty}]{reset}"


def format_short_tokens(count: int | None) -> str:
    if count is None:
        return "-"
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        val = count / 1000.0
        return f"{val:.0f}k" if val >= 10 else f"{val:.1f}k".rstrip("0").rstrip(".") + "k"
    val = count / 1_000_000.0
    return f"{val:.1f}M"


def parse_datetime_utc(raw: str | None) -> datetime | None:
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


def format_short_reset_duration(
    dt: datetime | None = None,
    seconds_remaining: int | float | None = None,
    now: datetime | None = None,
) -> str:
    if seconds_remaining is not None:
        total_secs = int(seconds_remaining)
    elif dt is not None:
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        delta = dt - now
        total_secs = int(delta.total_seconds())
    else:
        return "-"

    if total_secs <= 0:
        return "now"

    days = total_secs // 86400
    rem = total_secs % 86400
    hours = rem // 3600
    rem_min = rem % 3600
    minutes = rem_min // 60

    if days > 0:
        return f"{days}d+" if hours > 0 else f"{days}d"
    if hours >= 3:
        return f"{hours}h+" if minutes > 0 else f"{hours}h"
    if hours > 0:
        return f"{hours}h{minutes:02d}m" if minutes > 0 else f"{hours}h"
    if minutes > 0:
        return f"{minutes}m"
    return "<1m"


@dataclass
class BucketQuota:
    percentage: int | None = None
    reset_display: str = "-"
    remaining_fraction: float | None = None


@dataclass
class AccountQuotaInfo:
    gemini_5h: BucketQuota | None = None
    gemini_weekly: BucketQuota | None = None
    claude_5h: BucketQuota | None = None
    claude_weekly: BucketQuota | None = None
    subscription_date: str | None = None


def load_cache_quota(account: str, data_root: Path | None = None) -> AccountQuotaInfo | None:
    root = Path(data_root) if data_root else _default_data_root()
    cache_file = root / "cache" / "usage" / f"{account}.json"
    if not cache_file.is_file():
        return None
    try:
        with cache_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        parsed = data.get("parsed_data", {})
        info = AccountQuotaInfo()
        sub = parsed.get("subscription")
        if isinstance(sub, dict) and sub.get("date"):
            info.subscription_date = str(sub["date"])

        now = datetime.now(timezone.utc)
        for group in parsed.get("groups", []):
            gname = (group.get("name") or "").lower()
            for b in group.get("buckets", []):
                bid = (b.get("id") or "").lower()
                pct = b.get("percentage")
                frac = b.get("remaining_fraction")
                if pct is None and frac is not None:
                    pct = round(frac * 100)
                reset_dt = parse_datetime_utc(b.get("reset_time"))
                reset_disp = format_short_reset_duration(dt=reset_dt, now=now)
                b_obj = BucketQuota(percentage=pct, reset_display=reset_disp, remaining_fraction=frac)

                if "weekly" in bid:
                    if "gemini" in bid or "gemini" in gname:
                        info.gemini_weekly = b_obj
                    else:
                        info.claude_weekly = b_obj
                elif "5h" in bid or "five" in bid:
                    if "gemini" in bid or "gemini" in gname:
                        info.gemini_5h = b_obj
                    else:
                        info.claude_5h = b_obj
        return info
    except Exception:
        return None


def extract_payload_quota(quota_dict: Any) -> AccountQuotaInfo | None:
    if not isinstance(quota_dict, dict) or not quota_dict:
        return None
    info = AccountQuotaInfo()
    now = datetime.now(timezone.utc)
    for key, val in quota_dict.items():
        if not isinstance(val, dict):
            continue
        k = key.lower()
        frac = val.get("remaining_fraction")
        pct = round(frac * 100) if frac is not None else val.get("percentage")
        sec = val.get("reset_in_seconds")
        raw_time = val.get("reset_time")
        reset_dt = parse_datetime_utc(raw_time) if raw_time else None
        reset_disp = format_short_reset_duration(dt=reset_dt, seconds_remaining=sec, now=now)
        b_obj = BucketQuota(percentage=pct, reset_display=reset_disp, remaining_fraction=frac)

        if "weekly" in k:
            if "gemini" in k:
                info.gemini_weekly = b_obj
            elif "claude" in k or "3p" in k or "gpt" in k:
                info.claude_weekly = b_obj
        elif "5h" in k or "five" in k:
            if "gemini" in k:
                info.gemini_5h = b_obj
            elif "claude" in k or "3p" in k or "gpt" in k:
                info.claude_5h = b_obj
    return info


def resolve_quota(account: str, payload: dict[str, Any], data_root: Path | None = None) -> AccountQuotaInfo:
    live_quota = extract_payload_quota(payload.get("quota"))
    cached_quota = load_cache_quota(account, data_root=data_root)

    if live_quota is None and cached_quota is None:
        return AccountQuotaInfo()
    if live_quota is None:
        return cached_quota or AccountQuotaInfo()
    if cached_quota is None:
        return live_quota

    return AccountQuotaInfo(
        gemini_5h=live_quota.gemini_5h or cached_quota.gemini_5h,
        gemini_weekly=live_quota.gemini_weekly or cached_quota.gemini_weekly,
        claude_5h=live_quota.claude_5h or cached_quota.claude_5h,
        claude_weekly=live_quota.claude_weekly or cached_quota.claude_weekly,
        subscription_date=cached_quota.subscription_date,
    )


def resolve_account_name(payload: dict[str, Any] | None = None) -> str:
    env_profile = os.environ.get("AGYM_PROFILE")
    if env_profile and env_profile.strip():
        return env_profile.strip()

    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    match = re.search(r"profiles[/\\\\]([^/\\\\]+)[/\\\\]home", home)
    if match:
        return match.group(1)

    if payload:
        for k in ("account", "account_name", "user", "profile"):
            v = payload.get(k)
            if v and isinstance(v, str):
                return v

    return "default"


def resolve_model_display(payload: dict[str, Any] | None = None) -> str | None:
    if not payload:
        return None
    model_obj = payload.get("model")
    if isinstance(model_obj, dict):
        raw_name = model_obj.get("display_name") or model_obj.get("id") or ""
    elif isinstance(model_obj, str):
        raw_name = model_obj
    else:
        raw_name = ""

    if not raw_name:
        return None

    cleaned = re.sub(r"\s*\([^)]*\)", "", raw_name).strip()
    return cleaned if cleaned else raw_name


@dataclass
class ContextWindowInfo:
    used_percentage: int | None = None
    current_tokens: int | None = None
    total_tokens: int | None = None


def resolve_context_window(payload: dict[str, Any] | None = None) -> ContextWindowInfo | None:
    if not payload:
        return None
    ctx = payload.get("context_window")
    if not isinstance(ctx, dict):
        return None

    pct = ctx.get("used_percentage")
    if pct is not None:
        try:
            pct = round(float(pct))
        except (ValueError, TypeError):
            pct = None

    current = ctx.get("current_usage")
    if current is None:
        current = ctx.get("total_input_tokens")
    size = ctx.get("context_window_size")

    if pct is None and current is not None and size:
        pct = max(0, min(100, round((current / size) * 100)))

    return ContextWindowInfo(
        used_percentage=pct,
        current_tokens=current,
        total_tokens=size,
    )


@dataclass
class VCSInfo:
    branch: str | None = None
    worktree: str | None = None
    directory: str | None = None


def parse_git_head(head_content: str | None) -> str | None:
    if not head_content or not isinstance(head_content, str):
        return None
    cleaned = head_content.strip()
    if not cleaned:
        return None
    lines = cleaned.splitlines()
    if not lines:
        return None
    line = lines[0].strip()
    if not line:
        return None
    if line.startswith("ref: refs/heads/"):
        return line[len("ref: refs/heads/"):].strip()
    if line.startswith("ref: refs/tags/"):
        return line[len("ref: refs/tags/"):].strip()
    if line.startswith("ref: refs/remotes/"):
        return line[len("ref: refs/remotes/"):].strip()
    if line.startswith("ref: "):
        return line[len("ref: "):].strip()
    # Detached HEAD (hash): check if 7+ hex characters
    if len(line) >= 7 and all(c in "0123456789abcdefABCDEF" for c in line[:7]):
        return line[:7]
    return line


def resolve_git_vcs(
    cwd: Path | str | None = None,
    payload: dict[str, Any] | None = None,
) -> VCSInfo:
    if payload is None:
        payload = {}

    branch: str | None = None
    worktree: str | None = None
    directory: str | None = None

    # Check if payload explicitly provides VCS metadata without an explicit cwd
    has_explicit_cwd = (
        cwd is not None
        or bool(payload.get("cwd"))
        or bool(payload.get("workspace_path"))
        or bool(payload.get("workspace"))
    )
    payload_vcs = payload.get("vcs") if isinstance(payload.get("vcs"), dict) else {}
    payload_git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    payload_branch = payload_vcs.get("branch") or payload_git.get("branch") or payload.get("branch")
    payload_worktree = payload_vcs.get("worktree") or payload_git.get("worktree") or payload.get("worktree")
    payload_dir = payload_vcs.get("directory") or payload_git.get("directory") or payload.get("directory")
    if payload_dir:
        directory = str(payload_dir).strip()

    # Determine directory to inspect
    target_dir: Path | None = None
    if cwd is not None:
        try:
            target_dir = Path(cwd).resolve()
        except Exception:
            target_dir = None
    elif payload.get("cwd"):
        try:
            target_dir = Path(str(payload["cwd"])).resolve()
        except Exception:
            target_dir = None
    elif payload.get("workspace_path"):
        try:
            target_dir = Path(str(payload["workspace_path"])).resolve()
        except Exception:
            target_dir = None
    elif isinstance(payload.get("workspace"), str):
        try:
            target_dir = Path(payload["workspace"]).resolve()
        except Exception:
            target_dir = None
    elif isinstance(payload.get("workspace"), dict) and payload["workspace"].get("path"):
        try:
            target_dir = Path(str(payload["workspace"]["path"])).resolve()
        except Exception:
            target_dir = None
    else:
        try:
            target_dir = Path.cwd().resolve()
        except Exception:
            target_dir = None

    if directory is None and target_dir is not None and target_dir.name:
        directory = target_dir.name

    if not has_explicit_cwd and payload_branch:
        return VCSInfo(
            branch=str(payload_branch).strip(),
            worktree=str(payload_worktree).strip() if payload_worktree else None,
            directory=directory,
        )

    # Inspect disk
    if target_dir is not None:
        try:
            cur = target_dir
            dot_git: Path | None = None
            while True:
                candidate = cur / ".git"
                if candidate.exists():
                    dot_git = candidate
                    break
                parent = cur.parent
                if parent == cur:
                    break
                cur = parent

            if dot_git is not None:
                if dot_git.is_file():
                    content = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                    if content.startswith("gitdir:"):
                        gitdir_raw = content[len("gitdir:"):].strip()
                        resolved_gitdir = (dot_git.parent / gitdir_raw).resolve()
                        if resolved_gitdir.is_dir():
                            commondir = resolved_gitdir / "commondir"
                            if commondir.is_file() or resolved_gitdir.parent.name == "worktrees":
                                worktree = resolved_gitdir.name
                            head_file = resolved_gitdir / "HEAD"
                            if head_file.is_file():
                                head_content = head_file.read_text(encoding="utf-8", errors="replace")
                                branch = parse_git_head(head_content)
                elif dot_git.is_dir():
                    head_file = dot_git / "HEAD"
                    if head_file.is_file():
                        head_content = head_file.read_text(encoding="utf-8", errors="replace")
                        branch = parse_git_head(head_content)
        except Exception:
            pass

    # Fallback to payload metadata if branch or worktree was not found on disk
    if branch is None and payload_branch:
        branch = str(payload_branch).strip()
    if worktree is None and payload_worktree:
        worktree = str(payload_worktree).strip()

    return VCSInfo(branch=branch, worktree=worktree, directory=directory)


def format_vcs_tag(
    vcs: VCSInfo | None,
    include_worktree: bool = True,
    include_directory: bool = True,
    max_branch_len: int | None = None,
    max_dir_len: int | None = None,
    no_color: bool = False,
) -> str | None:
    if not vcs or not vcs.branch:
        return None

    branch = vcs.branch
    if max_branch_len and len(branch) > max_branch_len:
        branch = branch[: max_branch_len - 1] + "…"

    blue = BLUE if not no_color else ""
    gray = GRAY if not no_color else ""
    reset = RESET if not no_color else ""

    # Inside worktree: show compact worktree icon before the branch
    if vcs.worktree:
        if include_worktree:
            if no_color:
                return f"🌳 🌿 {branch}"
            return f"{blue}🌳 🌿 {branch}{reset}"
        if no_color:
            return f"🌿 {branch}"
        return f"{blue}🌿 {branch}{reset}"

    # Normal repository: show current directory (last directory of path) before branch name
    if include_directory and vcs.directory:
        dir_name = vcs.directory
        if max_dir_len and len(dir_name) > max_dir_len:
            dir_name = dir_name[: max_dir_len - 1] + "…"
        if no_color:
            return f"{dir_name} 🌿 {branch}"
        return f"{gray}{dir_name} {blue}🌿 {branch}{reset}"

    if no_color:
        return f"🌿 {branch}"
    return f"{blue}🌿 {branch}{reset}"


def render_statusline(
    payload: dict[str, Any] | None = None,
    profile_name: str | None = None,
    terminal_width: int | None = None,
    no_color: bool | None = None,
    data_root: Path | None = None,
    cwd: Path | str | None = None,
) -> str:
    if payload is None:
        payload = {}

    if no_color is None:
        no_color = "NO_COLOR" in os.environ or not sys.stdout.isatty()

    account = profile_name or resolve_account_name(payload)
    model = resolve_model_display(payload)
    quota = resolve_quota(account, payload, data_root=data_root)
    ctx = resolve_context_window(payload)
    vcs = resolve_git_vcs(cwd=cwd, payload=payload)

    if terminal_width is None:
        try:
            terminal_width = shutil.get_terminal_size((80, 24)).columns
        except Exception:
            terminal_width = 80

    bold = BOLD if not no_color else ""
    cyan = CYAN if not no_color else ""
    purple = PURPLE if not no_color else ""
    gray = GRAY if not no_color else ""
    reset = RESET if not no_color else ""
    sep = " │ " if no_color else f" {SEP} "

    # Account tag
    account_tag = f"{bold}{cyan}👤 {account}{reset}"

    # VCS tag
    include_worktree = terminal_width >= 55
    include_directory = terminal_width >= 65
    max_branch = None if terminal_width >= 85 else 16
    max_dir = None if terminal_width >= 85 else 14
    vcs_tag = format_vcs_tag(
        vcs,
        include_worktree=include_worktree,
        include_directory=include_directory,
        max_branch_len=max_branch,
        max_dir_len=max_dir,
        no_color=no_color,
    )

    # Model tag
    model_tag = f"{purple}⚡ {model}{reset}" if model else None

    # Quota 5h
    q5 = quota.gemini_5h
    if q5 and q5.percentage is not None:
        c5 = get_rank_color(q5.percentage, no_color=no_color)
        bar5 = format_mini_bar(q5.percentage, width=6, no_color=no_color)
        reset_suffix = f" ({q5.reset_display})" if q5.reset_display and q5.reset_display != "-" else ""
        if terminal_width >= 85:
            quota_5h_tag = f"5h: {bar5} {c5}{q5.percentage}%{reset}{reset_suffix}"
        else:
            quota_5h_tag = f"5h: {c5}{q5.percentage}%{reset}{reset_suffix}"
    else:
        quota_5h_tag = f"5h: {gray}-{reset}"

    # Quota Weekly
    qw = quota.gemini_weekly
    if qw and qw.percentage is not None:
        cw = get_rank_color(qw.percentage, no_color=no_color)
        reset_suffix = f" ({qw.reset_display})" if qw.reset_display and qw.reset_display != "-" else ""
        quota_wk_tag = f"Wk: {cw}{qw.percentage}%{reset}{reset_suffix}"
    else:
        quota_wk_tag = None

    # Claude 5h
    c3 = quota.claude_5h
    if c3 and c3.percentage is not None:
        cc = get_rank_color(c3.percentage, no_color=no_color)
        claude_tag = f"Claude: {cc}{c3.percentage}%{reset}"
    else:
        claude_tag = None

    # Context window
    if ctx and ctx.used_percentage is not None:
        rem_pct = 100 - ctx.used_percentage
        ctx_color = get_rank_color(rem_pct, no_color=no_color)
        if terminal_width >= 90 and ctx.current_tokens and ctx.total_tokens:
            cur_s = format_short_tokens(ctx.current_tokens)
            tot_s = format_short_tokens(ctx.total_tokens)
            ctx_tag = f"Ctx: {ctx_color}{ctx.used_percentage}%{reset} ({cur_s}/{tot_s})"
        else:
            ctx_tag = f"Ctx: {ctx_color}{ctx.used_percentage}%{reset}"
    else:
        ctx_tag = None

    # Width-based adaptive layout
    if terminal_width >= 105:
        parts = [account_tag]
        if vcs_tag:
            parts.append(vcs_tag)
        if model_tag:
            parts.append(model_tag)
        parts.append(quota_5h_tag)
        if quota_wk_tag:
            parts.append(quota_wk_tag)
        if claude_tag:
            parts.append(claude_tag)
        if ctx_tag:
            parts.append(ctx_tag)
        return sep.join(parts)

    if terminal_width >= 75:
        parts = [account_tag]
        if vcs_tag:
            parts.append(vcs_tag)
        if model_tag:
            parts.append(model_tag)
        parts.append(quota_5h_tag)
        if quota_wk_tag:
            parts.append(quota_wk_tag)
        if ctx_tag:
            parts.append(ctx_tag)
        return sep.join(parts)

    if terminal_width >= 55:
        parts = [account_tag]
        if vcs_tag:
            parts.append(vcs_tag)
        parts.append(quota_5h_tag)
        if ctx_tag:
            parts.append(ctx_tag)
        return sep.join(parts)

    return f"{account_tag}{sep}{quota_5h_tag}"


def resolve_python_executable(gui: bool = False) -> Path:
    current = Path(sys.executable).resolve()
    if gui and sys.platform == "win32":
        candidate = current.with_name("pythonw.exe")
        if candidate.is_file():
            return candidate
    return current


def _get_short_path(path: Path) -> Path:
    if sys.platform != "win32":
        return path
    resolved = path.resolve()
    if " " not in str(resolved):
        return resolved
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(500)
        res = ctypes.windll.kernel32.GetShortPathNameW(str(resolved), buf, 500)
        if res > 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return resolved


def get_statusline_script_path(data_root: Path | None = None) -> Path:
    root = Path(data_root) if data_root else _default_data_root()
    ext = ".cmd" if sys.platform == "win32" else ""
    return root / "bin" / f"statusline{ext}"


def _format_command_path(path: Path) -> str:
    if sys.platform == "win32":
        cmd_path = path if path.suffix.lower() == ".cmd" else path.with_suffix(".cmd")
        return str(_get_short_path(cmd_path))
    resolved = str(path.resolve())
    if " " in resolved and not (resolved.startswith('"') and resolved.endswith('"')):
        return f'"{resolved}"'
    return resolved


def get_statusline_command(data_root: Path | None = None) -> str:
    script_path = get_statusline_script_path(data_root)
    return _format_command_path(script_path)


def install_statusline_script(data_root: Path | None = None) -> Path:
    script_path = get_statusline_script_path(data_root)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(script_path.parent)

    python_bin = sys.executable
    package_root = str(Path(__file__).resolve().parent.parent)

    is_windows = sys.platform == "win32"
    py_target = script_path.parent / "statusline.py"
    content = (
        f"#!{python_bin}\n"
        "# Auto-generated by agym. Do not edit directly.\n"
        "import sys\n"
        f"if {package_root!r} not in sys.path:\n"
        f"    sys.path.insert(0, {package_root!r})\n"
        "from agym.statusline import main\n\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )

    fd, tmp_name = tempfile.mkstemp(prefix=".statusline_py.", suffix=".tmp", dir=script_path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        if not is_windows:
            tmp.chmod(0o755)
        os.replace(tmp, py_target)
    finally:
        if tmp.exists():
            tmp.unlink()

    if is_windows:
        pythonw_bin = resolve_python_executable(gui=True)
        cmd_content = (
            "@echo off\r\n"
            f'"{pythonw_bin}" "%~dp0statusline.py" %*\r\n'
        )
        fd_cmd, tmp_cmd_name = tempfile.mkstemp(prefix=".statusline_cmd.", suffix=".tmp", dir=script_path.parent)
        tmp_cmd = Path(tmp_cmd_name)
        try:
            with os.fdopen(fd_cmd, "w", encoding="utf-8") as handle:
                handle.write(cmd_content)
            os.replace(tmp_cmd, script_path)
        finally:
            if tmp_cmd.exists():
                tmp_cmd.unlink()
    else:
        # On POSIX (Linux/macOS), wrap statusline.py in a /bin/sh runner script.
        # This handles environments where sys.executable or data_root contains spaces
        # (e.g. macOS ~/Library/Application Support/...), which breaks kernel shebang parsing.
        sh_content = (
            "#!/bin/sh\n"
            'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
            f'exec "{python_bin}" "$DIR/statusline.py" "$@"\n'
        )
        fd_sh, tmp_sh_name = tempfile.mkstemp(prefix=".statusline_sh.", suffix=".tmp", dir=script_path.parent)
        tmp_sh = Path(tmp_sh_name)
        try:
            with os.fdopen(fd_sh, "w", encoding="utf-8") as handle:
                handle.write(sh_content)
            tmp_sh.chmod(0o755)
            os.replace(tmp_sh, script_path)
        finally:
            if tmp_sh.exists():
                tmp_sh.unlink()

    return script_path


def get_profile_settings_path(profile_home: Path) -> Path:
    return Path(profile_home) / ".gemini" / "antigravity-cli" / "settings.json"


def sync_profile_statusline(
    profile_home: Path,
    script_path: Path | None = None,
    enabled: bool = True,
    command: str | None = None,
) -> bool:
    settings_file = get_profile_settings_path(profile_home)
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(settings_file.parent)

    existing: dict[str, Any] = {}
    if settings_file.is_file():
        try:
            with settings_file.open("r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    if command is not None:
        target_command = command
    elif script_path is not None:
        target_command = _format_command_path(script_path)
    else:
        target_command = get_statusline_command()

    statusline_cfg = {
        "type": "command",
        "command": target_command,
        "enabled": enabled,
    }

    if existing.get("statusLine") == statusline_cfg:
        return False

    existing["statusLine"] = statusline_cfg

    fd, tmp_name = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=settings_file.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=2, sort_keys=True)
            handle.write("\n")
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, settings_file)
        return True
    finally:
        if tmp.exists():
            tmp.unlink()


def sync_all_profiles(store: ProfileStore, enabled: bool = True) -> list[str]:
    script_path = install_statusline_script(store.data_root)
    updated: list[str] = []
    for profile in store.list():
        sync_profile_statusline(profile.home, script_path, enabled=enabled)
        updated.append(profile.name)
    return updated


def get_statusline_status(store: ProfileStore) -> dict[str, Any]:
    script_path = get_statusline_script_path(store.data_root)
    installed = script_path.is_file() and (os.name == "nt" or os.access(script_path, os.X_OK))
    profiles_status: dict[str, Any] = {}
    for profile in store.list():
        settings_file = get_profile_settings_path(profile.home)
        configured = False
        enabled = False
        if settings_file.is_file():
            try:
                with settings_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                st = data.get("statusLine")
                if isinstance(st, dict):
                    configured = True
                    enabled = bool(st.get("enabled", True))
            except Exception:
                pass
        profiles_status[profile.name] = {
            "configured": configured,
            "enabled": enabled,
            "settings_path": str(settings_file),
        }
    return {
        "installed": installed,
        "script_path": str(script_path),
        "profiles": profiles_status,
    }


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr, sys.stdin):
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
    try:
        raw = ""
        if sys.stdin is not None and not getattr(sys.stdin, "isatty", lambda: False)():
            try:
                raw = sys.stdin.read()
            except Exception:
                raw = ""
        payload = json.loads(raw) if raw.strip() else {}
        output = render_statusline(payload)
        if sys.stdout is not None:
            sys.stdout.write(output + "\n")
            sys.stdout.flush()
        return 0
    except Exception:
        profile = os.environ.get("AGYM_PROFILE", "antigravity")
        if sys.stdout is not None:
            sys.stdout.write(f"[{profile}]\n")
            sys.stdout.flush()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
