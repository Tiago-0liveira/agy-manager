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


def render_statusline(
    payload: dict[str, Any] | None = None,
    profile_name: str | None = None,
    terminal_width: int | None = None,
    no_color: bool | None = None,
    data_root: Path | None = None,
) -> str:
    if payload is None:
        payload = {}

    if no_color is None:
        no_color = "NO_COLOR" in os.environ or not sys.stdout.isatty()

    account = profile_name or resolve_account_name(payload)
    model = resolve_model_display(payload)
    quota = resolve_quota(account, payload, data_root=data_root)
    ctx = resolve_context_window(payload)

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
        if model_tag:
            parts.append(model_tag)
        parts.append(quota_5h_tag)
        if quota_wk_tag:
            parts.append(quota_wk_tag)
        if ctx_tag:
            parts.append(ctx_tag)
        return sep.join(parts)

    if terminal_width >= 55:
        parts = [account_tag, quota_5h_tag]
        if ctx_tag:
            parts.append(ctx_tag)
        return sep.join(parts)

    return f"{account_tag}{sep}{quota_5h_tag}"


def get_statusline_script_path(data_root: Path | None = None) -> Path:
    root = Path(data_root) if data_root else _default_data_root()
    return root / "bin" / "statusline"


def install_statusline_script(data_root: Path | None = None) -> Path:
    script_path = get_statusline_script_path(data_root)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(script_path.parent)

    python_bin = sys.executable
    package_root = str(Path(__file__).resolve().parent.parent)
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

    fd, tmp_name = tempfile.mkstemp(prefix=".statusline.", suffix=".tmp", dir=script_path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        if os.name != "nt":
            tmp.chmod(0o755)
        os.replace(tmp, script_path)
    finally:
        if tmp.exists():
            tmp.unlink()

    return script_path


def get_profile_settings_path(profile_home: Path) -> Path:
    return Path(profile_home) / ".gemini" / "antigravity-cli" / "settings.json"


def sync_profile_statusline(
    profile_home: Path,
    script_path: Path,
    enabled: bool = True,
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

    statusline_cfg = {
        "type": "command",
        "command": str(script_path.resolve()),
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
    try:
        raw = ""
        if not sys.stdin.isatty():
            try:
                raw = sys.stdin.read()
            except Exception:
                raw = ""
        payload = json.loads(raw) if raw.strip() else {}
        output = render_statusline(payload)
        sys.stdout.write(output + "\n")
        sys.stdout.flush()
        return 0
    except Exception:
        profile = os.environ.get("AGYM_PROFILE", "antigravity")
        sys.stdout.write(f"[{profile}]\n")
        sys.stdout.flush()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
