from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .profiles import _chmod_private_dir, _default_data_root

# Shared default usage cache TTL (5 minutes)
DEFAULT_USAGE_CACHE_TTL_SECONDS = 300.0
USAGE_CACHE_TTL_SECONDS = DEFAULT_USAGE_CACHE_TTL_SECONDS
TTL_USAGE_SECONDS = DEFAULT_USAGE_CACHE_TTL_SECONDS
TTL_TOKENS_SECONDS = 600.0    # 10 minutes for token usage tracking
USAGE_CACHE_VERSION = 2  # v1 may contain quota values from the inaccurate direct API path


def parse_duration_seconds(duration_str: str) -> float:
    """Parses a simple duration string (e.g. '30s', '5m', '1h', '2d', or 'default') into seconds.

    If 'default' is passed, returns DEFAULT_USAGE_CACHE_TTL_SECONDS (300.0).
    Raises ValueError on invalid formats or non-positive durations.
    """
    raw = str(duration_str).strip().lower()
    if raw == "default":
        return DEFAULT_USAGE_CACHE_TTL_SECONDS

    import re

    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$", raw)
    if not m:
        raise ValueError(
            f"invalid duration format '{duration_str}'. Expected e.g. 30s, 5m, 1h, or default"
        )
    val = float(m.group(1))
    unit = m.group(2) or "s"
    if val <= 0:
        raise ValueError(f"duration must be greater than 0, got '{duration_str}'")

    if unit == "s":
        return val
    elif unit == "m":
        return val * 60.0
    elif unit == "h":
        return val * 3600.0
    elif unit == "d":
        return val * 86400.0
    return val


def format_duration(seconds: float) -> str:
    """Formats seconds into concise duration representation (e.g. 30s, 5m, 1h)."""
    secs = int(seconds) if isinstance(seconds, (int, float)) and float(seconds).is_integer() else seconds
    if isinstance(secs, int):
        if secs % 86400 == 0 and secs >= 86400:
            return f"{secs // 86400}d"
        if secs % 3600 == 0 and secs >= 3600:
            return f"{secs // 3600}h"
        if secs % 60 == 0 and secs >= 60:
            return f"{secs // 60}m"
    return f"{secs}s"


def should_refresh_cache(
    cached_timestamp: float | None,
    force_fresh: bool = False,
    ttl_seconds: float = USAGE_CACHE_TTL_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Evaluates whether usage cache should be refreshed according to TTL and freshness flags."""
    if force_fresh:
        return True
    if cached_timestamp is None:
        return True
    if now is None:
        now_ts = datetime.now(timezone.utc).timestamp()
    else:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_ts = now.timestamp()
    if (now_ts - cached_timestamp) > ttl_seconds:
        return True
    return False


def format_age(seconds: float) -> str:
    """Formats age in seconds into human-readable text."""
    if seconds < 1.0:
        return "just now"
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s ago"
    minutes = secs // 60
    rem_secs = secs % 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    rem_mins = minutes % 60
    if hours < 24:
        return f"{hours}h {rem_mins}m ago" if rem_mins > 0 else f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def format_freshness_badge(
    cached: bool,
    age_seconds: float = 0.0,
    *,
    use_color: bool = True,
) -> str:
    """Returns a badge like 'Live' or 'Cached 42s ago' with subtle ANSI formatting."""
    if not cached:
        if use_color:
            return "\033[32mLive\033[0m"
        return "Live"

    age_str = format_age(age_seconds)
    text = f"Cached {age_str}"
    if use_color:
        return f"\033[90m{text}\033[0m"
    return text


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".cache.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class CacheManager:
    """Manages multi-tier cached responses for agym."""

    def __init__(self, cache_root: Path | None = None) -> None:
        if cache_root is None:
            self.cache_root = _default_data_root() / "cache"
        else:
            self.cache_root = Path(cache_root)

        self.usage_dir = self.cache_root / "usage"
        self.tokens_dir = self.cache_root / "tokens"

    def _ensure_dirs(self) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.usage_dir.mkdir(parents=True, exist_ok=True)
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(self.cache_root)
        _chmod_private_dir(self.usage_dir)
        _chmod_private_dir(self.tokens_dir)

    def _get_entry(
        self,
        filepath: Path,
        max_age: float,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], float, str] | None:
        """Reads a cache file, checks TTL, and handles corrupted files gracefully."""
        if not filepath.exists():
            return None

        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        try:
            with filepath.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # Corrupted cache file: treat as miss
            return None

        if not isinstance(payload, dict):
            return None

        cached_timestamp = payload.get("cached_timestamp")
        if not isinstance(cached_timestamp, (int, float)):
            return None

        age = now.timestamp() - float(cached_timestamp)
        if age < 0:
            age = 0.0

        if age > max_age:
            return None

        cached_at = str(payload.get("cached_at", ""))
        return payload, age, cached_at

    # --- Quota Usage Cache ---

    def get_usage(
        self,
        profile_name: str,
        max_age: float = TTL_USAGE_SECONDS,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], str, float, str] | None:
        """Retrieves cached quota usage data if valid within max_age.

        Returns (parsed_data, raw_output, age_seconds, cached_at) or None on miss.
        """
        filepath = self.usage_dir / f"{profile_name}.json"
        entry = self._get_entry(filepath, max_age, now=now)
        if entry is None:
            return None

        payload, age, cached_at = entry
        if payload.get("version") != USAGE_CACHE_VERSION:
            return None
        parsed_data = payload.get("parsed_data")
        raw_output = payload.get("raw_output", "")
        if not isinstance(parsed_data, dict):
            return None

        # Check if cached 5h bucket reset_time has already passed
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        groups = parsed_data.get("groups")
        if isinstance(groups, list):
            for g in groups:
                if not isinstance(g, dict):
                    continue
                g_name = str(g.get("name") or g.get("displayName") or "").lower()
                for b in g.get("buckets", []):
                    if not isinstance(b, dict):
                        continue
                    b_id = str(b.get("id") or b.get("bucketId") or "").lower()
                    w = str(b.get("window") or "").lower()
                    if ("gemini" in g_name or "gemini" in b_id) and ("5h" in w or "5h" in b_id):
                        raw_reset = b.get("reset_time") or b.get("resetTime")
                        if raw_reset:
                            from .usage import parse_iso_datetime
                            reset_dt = parse_iso_datetime(str(raw_reset))
                            if reset_dt is not None:
                                if reset_dt.tzinfo is None:
                                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                                if reset_dt <= now_dt:
                                    return None

        return parsed_data, str(raw_output), age, cached_at

    def set_usage(
        self,
        profile_name: str,
        parsed_data: dict[str, Any],
        raw_output: str,
        now: datetime | None = None,
    ) -> None:
        """Saves quota usage to cache atomically with private permissions."""
        self._ensure_dirs()
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        payload = {
            "version": USAGE_CACHE_VERSION,
            "profile": profile_name,
            "cached_at": now.isoformat(),
            "cached_timestamp": now.timestamp(),
            "data_type": "usage",
            "raw_output": raw_output,
            "parsed_data": parsed_data,
        }
        filepath = self.usage_dir / f"{profile_name}.json"
        _write_json_atomic(filepath, payload)

    def get_cached_usage_with_meta(
        self,
        profile_name: str,
    ) -> tuple[dict[str, Any] | None, float | None]:
        """Retrieves raw cached usage data along with the timestamp of when it was fetched."""
        filepath = self.usage_dir / f"{profile_name}.json"
        if not filepath.exists():
            return None, None
        try:
            with filepath.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return None, None
            if payload.get("version") != USAGE_CACHE_VERSION:
                return None, None
            cached_timestamp = payload.get("cached_timestamp")
            if not isinstance(cached_timestamp, (int, float)):
                return None, None
            parsed_data = payload.get("parsed_data")
            return parsed_data, float(cached_timestamp)
        except (OSError, json.JSONDecodeError):
            return None, None

    # --- Token Usage Cache & Ledger ---

    def get_tokens(
        self,
        profile_name: str,
        max_age: float = TTL_TOKENS_SECONDS,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], float, str] | None:
        """Retrieves cached token usage data if valid within max_age.

        Returns (payload, age_seconds, cached_at) or None on miss.
        """
        filepath = self.tokens_dir / f"{profile_name}.json"
        entry = self._get_entry(filepath, max_age, now=now)
        if entry is None:
            return None
        payload, age, cached_at = entry
        return payload, age, cached_at

    def load_cumulative_ledger(self, profile_name: str) -> dict[str, Any]:
        """Loads the cumulative token ledger for a profile, or default structure if none."""
        filepath = self.tokens_dir / f"{profile_name}.json"
        if not filepath.exists():
            return {
                "version": 1,
                "profile": profile_name,
                "cumulative": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "cache_read_tokens": 0,
                    "total_tokens": 0,
                    "snapshot_count": 0,
                },
                "snapshots": [],
            }
        try:
            with filepath.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict) and "cumulative" in data:
                    return data
        except (OSError, json.JSONDecodeError):
            pass

        return {
            "version": 1,
            "profile": profile_name,
            "cumulative": {
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
                "snapshot_count": 0,
            },
            "snapshots": [],
        }

    def record_token_snapshot(
        self,
        profile_name: str,
        snapshot: dict[str, int],
        now: datetime | None = None,
        source: str = "query",
    ) -> dict[str, Any]:
        """Records a new token snapshot and recomputes cumulative totals."""
        self._ensure_dirs()
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        data = self.load_cumulative_ledger(profile_name)
        inp = int(snapshot.get("input_tokens", 0))
        out = int(snapshot.get("output_tokens", 0))
        thk = int(snapshot.get("thinking_tokens", 0))
        crd = int(snapshot.get("cache_read_tokens", 0))
        tot = int(snapshot.get("total_tokens", inp + out) or (inp + out))

        # Check if identical to last snapshot to avoid duplicate zero or redundant updates
        snapshots = data.get("snapshots", [])
        should_record = True
        if snapshots:
            last = snapshots[-1]
            if (
                last.get("input_tokens") == inp
                and last.get("output_tokens") == out
                and last.get("thinking_tokens") == thk
                and last.get("cache_read_tokens") == crd
                and last.get("total_tokens") == tot
            ):
                # Don't keep piling up identical snapshots repeatedly
                should_record = False

        if should_record and tot > 0:
            snapshots.append(
                {
                    "timestamp": now.isoformat(),
                    "source": source,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "thinking_tokens": thk,
                    "cache_read_tokens": crd,
                    "total_tokens": tot,
                }
            )
            # Keep history to max 500 snapshots
            if len(snapshots) > 500:
                snapshots = snapshots[-500:]
            data["snapshots"] = snapshots

            # Recompute cumulative
            cum = data.setdefault("cumulative", {})
            cum["input_tokens"] = int(cum.get("input_tokens", 0)) + inp
            cum["output_tokens"] = int(cum.get("output_tokens", 0)) + out
            cum["thinking_tokens"] = int(cum.get("thinking_tokens", 0)) + thk
            cum["cache_read_tokens"] = int(cum.get("cache_read_tokens", 0)) + crd
            cum["total_tokens"] = int(cum.get("total_tokens", 0)) + tot
            cum["snapshot_count"] = len(snapshots)

        data["cached_at"] = now.isoformat()
        data["cached_timestamp"] = now.timestamp()
        data["data_type"] = "tokens"
        data["latest_snapshot"] = {
            "timestamp": now.isoformat(),
            "source": source,
            "input_tokens": inp,
            "output_tokens": out,
            "thinking_tokens": thk,
            "cache_read_tokens": crd,
            "total_tokens": tot,
        }

        filepath = self.tokens_dir / f"{profile_name}.json"
        _write_json_atomic(filepath, data)
        return data

    def set_tokens(
        self,
        profile_name: str,
        cumulative_tokens: dict[str, int],
        latest_snapshot: dict[str, int] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Sets tokens cache directly."""
        self._ensure_dirs()
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        data = self.load_cumulative_ledger(profile_name)
        data["cached_at"] = now.isoformat()
        data["cached_timestamp"] = now.timestamp()
        data["cumulative"] = cumulative_tokens
        if latest_snapshot is not None:
            data["latest_snapshot"] = latest_snapshot
        filepath = self.tokens_dir / f"{profile_name}.json"
        _write_json_atomic(filepath, data)

    def rename(self, old_profile: str, new_profile: str) -> None:
        """Renames cache and token ledger files from old_profile to new_profile."""
        self._ensure_dirs()
        # 1. Quota usage cache
        old_u = self.usage_dir / f"{old_profile}.json"
        new_u = self.usage_dir / f"{new_profile}.json"
        if old_u.exists():
            try:
                with old_u.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    data["profile"] = new_profile
                    _write_json_atomic(new_u, data)
                else:
                    os.replace(old_u, new_u)
                if old_u.exists() and old_u.resolve() != new_u.resolve():
                    try:
                        old_u.unlink()
                    except OSError:
                        pass
            except (OSError, json.JSONDecodeError):
                if old_u.exists() and old_u.resolve() != new_u.resolve():
                    try:
                        os.replace(old_u, new_u)
                    except OSError:
                        pass

        # 2. Tokens cache & cumulative ledger
        old_t = self.tokens_dir / f"{old_profile}.json"
        new_t = self.tokens_dir / f"{new_profile}.json"
        if old_t.exists():
            try:
                with old_t.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    data["profile"] = new_profile
                    _write_json_atomic(new_t, data)
                else:
                    os.replace(old_t, new_t)
                if old_t.exists() and old_t.resolve() != new_t.resolve():
                    try:
                        old_t.unlink()
                    except OSError:
                        pass
            except (OSError, json.JSONDecodeError):
                if old_t.exists() and old_t.resolve() != new_t.resolve():
                    try:
                        os.replace(old_t, new_t)
                    except OSError:
                        pass

    def clear(self, profile_name: str | None = None) -> None:
        """Clears cache files for a specific profile or all profiles."""
        if profile_name:
            u_path = self.usage_dir / f"{profile_name}.json"
            t_path = self.tokens_dir / f"{profile_name}.json"
            if u_path.exists():
                u_path.unlink()
            if t_path.exists():
                t_path.unlink()
        else:
            if self.usage_dir.exists():
                for f in self.usage_dir.glob("*.json"):
                    f.unlink()
            if self.tokens_dir.exists():
                for f in self.tokens_dir.glob("*.json"):
                    f.unlink()


def get_cached_usage_with_meta(
    profile_name: str,
    cache_manager: CacheManager | None = None,
) -> tuple[dict[str, Any] | None, float | None]:
    """Convenience helper to retrieve (data, last_fetched_timestamp) for a profile."""
    cm = cache_manager if cache_manager is not None else CacheManager()
    return cm.get_cached_usage_with_meta(profile_name)


def fetch_and_cache_usage(
    profiles: Any = None,
    force: bool = False,
    *,
    agy_path: Any = None,
    cache_manager: CacheManager | None = None,
    concurrency_limit: int = 8,
    timeout: float = 30.0,
    runner: Any = None,
    on_progress: Any = None,
) -> Any:
    """Convenience wrapper delegating to agym.usage.fetch_and_cache_usage."""
    from .usage import fetch_and_cache_usage as _fetch_and_cache

    return _fetch_and_cache(
        profiles=profiles,
        force=force,
        agy_path=agy_path,
        cache_manager=cache_manager,
        concurrency_limit=concurrency_limit,
        timeout=timeout,
        runner=runner,
        on_progress=on_progress,
    )
