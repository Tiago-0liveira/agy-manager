from __future__ import annotations

from agym.launcher import persistent_profile_data_exists
from agym.profiles import Profile, ProfileStore

from .errors import IntegrationError, PROFILE_UNAVAILABLE, NO_CAPACITY
from .store import Store


def project(profile: Profile, store: Store) -> dict[str, str]:
    if store.active_lease(profile.name):
        readiness, reason = "busy", "Profile has an active integration run"
    elif profile.settings.validation_errors or not profile.home.is_dir():
        readiness, reason = "unavailable", "Profile is unavailable"
    elif not persistent_profile_data_exists(profile):
        readiness, reason = "auth_required", "Profile needs authentication"
    else:
        readiness, reason = "ready", ""
    return {"profile_id": profile.name, "name": profile.name,
            "readiness": readiness, "reason": reason}


def list_profiles(store: Store, profiles: ProfileStore | None = None) -> list[dict[str, str]]:
    return [project(p, store) for p in (profiles or ProfileStore()).list()]


def select(requested: str, store: Store, profiles: ProfileStore | None = None) -> Profile:
    source = profiles or ProfileStore()
    if requested != "auto":
        try:
            profile = source.get(requested)
        except Exception as exc:
            raise IntegrationError(PROFILE_UNAVAILABLE, "Profile unavailable") from exc
        readiness = project(profile, store)["readiness"]
        if readiness == "busy":
            raise IntegrationError(NO_CAPACITY, "Profile is busy", retryable=True)
        if readiness != "ready":
            raise IntegrationError(PROFILE_UNAVAILABLE, "Profile unavailable")
        return profile

    # AGYM's existing quota fetch and picker ranking supply the ordering.
    from agym.picker import prepare_accounts_for_picker
    from agym.usage import fetch_and_cache_usage
    ready = [p for p in source.list() if project(p, store)["readiness"] == "ready"]
    if not ready:
        raise IntegrationError(NO_CAPACITY, "No ready profile is available", retryable=True)
    try:
        usage = fetch_and_cache_usage(ready)
        ranking = prepare_accounts_for_picker(usage, use_color=False)
    except Exception as exc:
        raise IntegrationError(NO_CAPACITY, "Profile capacity is unavailable", retryable=True) from exc
    by_name = {p.name: p for p in ready}
    # The picker presents a missing 5h bucket as full capacity for humans. A
    # machine start must require actual quota evidence for both windows.
    from agym.usage import extract_quota_bucket
    capacity = {}
    for item in usage:
        if item.status != "success":
            continue
        short = extract_quota_bucket(item, "gemini", "5h") or extract_quota_bucket(item, "claude", "5h")
        weekly = extract_quota_bucket(item, "gemini", "week") or extract_quota_bucket(item, "claude", "week")
        if short and short.remaining_fraction > 0 and (weekly is None or weekly.remaining_fraction > 0):
            capacity[item.account] = True
    for item in ranking:
        if item.status == "success" and item.remaining_fraction > 0 and capacity.get(item.account_name) and item.account_name in by_name:
            return by_name[item.account_name]
    raise IntegrationError(NO_CAPACITY, "No profile has known capacity", retryable=True)
