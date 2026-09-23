from __future__ import annotations

from datetime import datetime, timezone

from agym.profiles import ProfileStore
from agym.usage import fetch_and_cache_usage

from .errors import IntegrationError, PROFILE_UNAVAILABLE


def list_usage(profile_name: str | None = None, refresh: bool = False,
               profiles: ProfileStore | None = None) -> list[dict]:
    source = profiles or ProfileStore()
    try:
        selected = [source.get(profile_name)] if profile_name else source.list()
    except Exception as exc:
        raise IntegrationError(PROFILE_UNAVAILABLE, "Profile unavailable") from exc
    if not selected:
        return []
    usages = fetch_and_cache_usage(selected, force=refresh)
    result = []
    for usage in usages:
        observed = usage.cached_at or datetime.now(timezone.utc).isoformat()
        if usage.status != "success":
            result.append({"profile_id": usage.account, "model_group": "unknown",
                           "windows": [], "observed_at": observed, "stale": True,
                           "error": usage.error or "Usage unavailable"})
            continue
        for group in usage.groups:
            group_name = group.name.lower()
            model_group = "gemini" if "gemini" in group_name else "claude" if "claude" in group_name or "gpt" in group_name else group_name
            result.append({"profile_id": usage.account, "model_group": model_group,
                           "windows": [{"name": bucket.window, "remaining": bucket.remaining_fraction,
                                        "reset_at": bucket.reset_time.isoformat() if bucket.reset_time else None}
                                       for bucket in group.buckets],
                           "observed_at": observed, "stale": False})
    return result
