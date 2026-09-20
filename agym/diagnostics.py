from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Iterable

from . import __version__
from .launcher import AgyNotFound, agy_version, resolve_agy
from .profiles import Profile, ProfileStore, unix_permissions_warning

KNOWN_CREDENTIAL_FILE = "antigravity-oauth-token"


def _credential_state_present(profile: Profile) -> bool:
    # Report presence only; never read credential contents.
    return (profile.home / ".gemini" / "antigravity-cli" / KNOWN_CREDENTIAL_FILE).is_file()


def _profile_state_present(profile: Profile) -> bool:
    root = profile.home / ".gemini" / "antigravity-cli"
    if not root.is_dir():
        return False
    try:
        return any(path.is_file() for path in root.rglob("*"))
    except OSError:
        return False


def doctor_lines(store: ProfileStore, selected: str | None = None) -> list[str]:
    lines = [
        f"agym version: {__version__}",
        f"Python: {platform.python_version()} ({sys.executable})",
        f"OS: {platform.platform()}",
    ]
    try:
        agy = resolve_agy()
        lines.append(f"agy executable: {agy}")
        lines.append(f"agy version: {agy_version(agy) or 'unknown'}")
    except AgyNotFound:
        lines.append("agy executable: NOT FOUND")
        lines.append("agy version: unavailable")

    lines.extend(
        [
            f"Config: {store.config_path}",
            f"Profile root: {store.profiles_root}",
            "Forced file auth configured by agym: yes",
            "Forced file auth integration-tested on this machine: unknown",
        ]
    )

    profiles: Iterable[Profile]
    if selected is not None:
        profiles = [store.get(selected)]
    else:
        profiles = store.list()

    profiles = list(profiles)
    if not profiles:
        lines.append("Profiles: none")
        return lines

    lines.append("Profiles:")
    for profile in profiles:
        exists = profile.home.is_dir()
        lines.append(f"  {profile.name}: directory {'exists' if exists else 'MISSING'}")
        lines.append(
            f"    Antigravity state: {'present' if _profile_state_present(profile) else 'not detected'}"
        )
        lines.append(
            f"    credential state file: {'present' if _credential_state_present(profile) else 'not detected'}"
        )
        warning = unix_permissions_warning(store.profile_dir(profile.name))
        if warning:
            lines.append(f"    {warning}")
        warning = unix_permissions_warning(profile.home)
        if warning:
            lines.append(f"    {warning}")
    return lines
