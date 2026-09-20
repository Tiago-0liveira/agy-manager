from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESERVED_NAMES = {"setup", "list", "remove", "doctor", "config"}


class ProfileError(RuntimeError):
    pass


class InvalidProfileName(ProfileError):
    pass


class ProfileExists(ProfileError):
    pass


class ProfileNotFound(ProfileError):
    pass


@dataclass(frozen=True)
class ProfileSettings:
    model: str | None = None
    dangerously_skip_permissions: bool = False
    validation_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "dangerously_skip_permissions": self.dangerously_skip_permissions,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ProfileSettings:
        if data is None:
            return cls(model=None, dangerously_skip_permissions=False, validation_errors=())
        if not isinstance(data, dict):
            return cls(
                model=None,
                dangerously_skip_permissions=False,
                validation_errors=("settings must be a dictionary",),
            )

        errors: list[str] = []
        raw_model = data.get("model")
        if raw_model is not None and not isinstance(raw_model, str):
            errors.append("model must be null or a string")
            model = None
        else:
            model = raw_model

        raw_danger = data.get("dangerously_skip_permissions", False)
        if not isinstance(raw_danger, bool):
            errors.append("dangerously_skip_permissions must be a boolean")
            dangerously_skip_permissions = False
        else:
            dangerously_skip_permissions = raw_danger

        return cls(
            model=model,
            dangerously_skip_permissions=dangerously_skip_permissions,
            validation_errors=tuple(errors),
        )


@dataclass(frozen=True)
class Profile:
    name: str
    home: Path
    created_at: str
    agy_version: str | None = None
    settings: ProfileSettings = field(default_factory=ProfileSettings)


def validate_profile_name(name: str) -> str:
    if name in RESERVED_NAMES:
        raise InvalidProfileName(
            f"'{name}' is a reserved command name and cannot be used as a profile name"
        )
    if name in {".", ".."} or not PROFILE_RE.fullmatch(name):
        raise InvalidProfileName(
            "profile names must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ "
            "and may not be '.' or '..'"
        )
    return name


def _default_config_root() -> Path:
    override = os.environ.get("AGYM_CONFIG_HOME")
    if override:
        return Path(override).expanduser().resolve()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "agym"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "agym"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "agym"


def _default_data_root() -> Path:
    override = os.environ.get("AGYM_DATA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "agym"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "agym"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "agym"


def _chmod_private_dir(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)


def _write_json_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=path.parent)
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
            tmp.unlink()


class ProfileStore:
    def __init__(self, config_root: Path | None = None, data_root: Path | None = None) -> None:
        self.config_root = Path(config_root) if config_root else _default_config_root()
        self.data_root = Path(data_root) if data_root else _default_data_root()
        self.config_path = self.config_root / "config.json"
        self.profiles_root = self.data_root / "profiles"

    def _load(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"version": 1, "profiles": {}}
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"cannot read {self.config_path}: {exc}") from exc
        if data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
            raise ProfileError(f"unsupported or invalid config file: {self.config_path}")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        _write_json_private(self.config_path, data)

    def create(self, name: str, agy_version: str | None = None) -> Profile:
        validate_profile_name(name)
        data = self._load()
        if name in data["profiles"]:
            raise ProfileExists(f"profile already exists: {name}")

        self.profiles_root.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(self.data_root)
        _chmod_private_dir(self.profiles_root)

        profile_dir = self.profiles_root / name
        home = profile_dir / "home"
        home.mkdir(parents=True, exist_ok=False)
        _chmod_private_dir(profile_dir)
        _chmod_private_dir(home)

        profile = Profile(
            name=name,
            home=home.resolve(),
            created_at=datetime.now(timezone.utc).isoformat(),
            agy_version=agy_version,
            settings=ProfileSettings(),
        )
        data["profiles"][name] = {
            "created_at": profile.created_at,
            "home": str(profile.home),
            "agy_version": profile.agy_version,
            "settings": profile.settings.to_dict(),
        }
        try:
            self._save(data)
        except Exception:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise
        return profile

    def get(self, name: str) -> Profile:
        validate_profile_name(name)
        data = self._load()
        raw = data["profiles"].get(name)
        if raw is None:
            raise ProfileNotFound(f"profile not found: {name}")
        return Profile(
            name=name,
            home=Path(raw["home"]),
            created_at=raw["created_at"],
            agy_version=raw.get("agy_version"),
            settings=ProfileSettings.from_dict(raw.get("settings")),
        )

    def list(self) -> list[Profile]:
        data = self._load()
        result: list[Profile] = []
        for name in sorted(data["profiles"]):
            raw = data["profiles"][name]
            result.append(
                Profile(
                    name=name,
                    home=Path(raw["home"]),
                    created_at=raw["created_at"],
                    agy_version=raw.get("agy_version"),
                    settings=ProfileSettings.from_dict(raw.get("settings")),
                )
            )
        return result

    def update_settings(self, name: str, settings: ProfileSettings) -> Profile:
        validate_profile_name(name)
        data = self._load()
        if name not in data["profiles"]:
            raise ProfileNotFound(f"profile not found: {name}")
        data["profiles"][name]["settings"] = settings.to_dict()
        self._save(data)
        return self.get(name)

    def remove(self, name: str) -> Path:
        profile = self.get(name)
        data = self._load()
        profile_dir = self.profiles_root / name

        # Refuse to delete anything that does not resolve directly beneath our profiles root.
        expected = profile_dir.resolve()
        actual_parent = expected.parent
        if actual_parent != self.profiles_root.resolve():
            raise ProfileError(f"refusing unsafe profile path: {expected}")

        if profile_dir.exists():
            shutil.rmtree(profile_dir)
        data["profiles"].pop(name, None)
        self._save(data)
        return expected

    def profile_dir(self, name: str) -> Path:
        validate_profile_name(name)
        return self.profiles_root / name


def unix_permissions_warning(path: Path) -> str | None:
    if os.name == "nt" or not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return f"WARNING: {path} permissions are {mode:04o}; expected no group/world access"
    return None
