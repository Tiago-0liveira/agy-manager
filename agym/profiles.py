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
RESERVED_NAMES = {
    "setup",
    "list",
    "remove",
    "doctor",
    "usage",
    "token",
    "tokens",
    "token-usage",
    "edit",
    "help",
    "config",
    "statusline",
    "rotate",
    "rename",
    "mv",
}


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

        danger_keys = (
            "dangerously_skip_permissions",
            "dangerously_skip_permission",
            "dsp",
            "skip_perms",
        )
        raw_danger = None
        for key in danger_keys:
            if key in data:
                raw_danger = data[key]
                break

        if raw_danger is None:
            dangerously_skip_permissions = False
        elif not isinstance(raw_danger, bool):
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
    settings: ProfileSettings = field(default_factory=ProfileSettings)
    subscription_date: str | None = None


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


def _detect_profile_escape_roots() -> tuple[Path | None, Path | None]:
    home_str = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if not home_str:
        return None, None
    try:
        norm = home_str.replace("\\", "/")
        parts = [part for part in norm.split("/") if part]
        if len(parts) >= 3 and parts[-1] == "home" and parts[-3] == "profiles":
            if platform.system() == "Windows" or os.name == "nt":
                data_root_str = norm.rsplit("/profiles/", 1)[0]
                data_root = Path(data_root_str)
                return data_root, data_root
            p = Path(home_str).resolve()
            data_root = p.parent.parent.parent
            if data_root.parent.name == "share" and data_root.parent.parent.name == ".local":
                host_home = data_root.parent.parent.parent
                return host_home / ".config" / "agym", data_root
            return data_root / "config", data_root
    except Exception:
        pass
    return None, None


def _default_config_root() -> Path:
    override = os.environ.get("AGYM_CONFIG_HOME")
    if override:
        return Path(override).expanduser().resolve()
    cfg_root, _ = _detect_profile_escape_roots()
    if cfg_root is not None:
        return cfg_root
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
    _, data_root = _detect_profile_escape_roots()
    if data_root is not None:
        return data_root
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
            with self.config_path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"cannot read {self.config_path}: {exc}") from exc
        if data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
            raise ProfileError(f"unsupported or invalid config file: {self.config_path}")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        _write_json_private(self.config_path, data)

    def create(
        self,
        name: str,
        settings: ProfileSettings | None = None,
        subscription_date: str | None = None,
    ) -> Profile:
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
            settings=settings or ProfileSettings(),
            subscription_date=subscription_date,
        )
        data["profiles"][name] = {
            "created_at": profile.created_at,
            "home": str(profile.home),
            "settings": profile.settings.to_dict(),
            "subscription_date": profile.subscription_date,
        }
        try:
            self._save(data)
        except Exception:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise
        return profile

    def exists(self, name: str) -> bool:
        try:
            validate_profile_name(name)
        except InvalidProfileName:
            return False
        data = self._load()
        return name in data.get("profiles", {})

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
            settings=ProfileSettings.from_dict(raw.get("settings")),
            subscription_date=raw.get("subscription_date"),
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
                    settings=ProfileSettings.from_dict(raw.get("settings")),
                    subscription_date=raw.get("subscription_date"),
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

    def set_subscription_date(self, name: str, subscription_date: str | None) -> Profile:
        validate_profile_name(name)
        data = self._load()
        if name not in data["profiles"]:
            raise ProfileNotFound(f"profile not found: {name}")
        data["profiles"][name]["subscription_date"] = subscription_date
        self._save(data)
        return self.get(name)

    def rename(self, old_name: str, new_name: str) -> Profile:
        validate_profile_name(old_name)
        validate_profile_name(new_name)
        if old_name == new_name:
            raise ProfileError(f"cannot rename profile to the same name: '{old_name}'")

        data = self._load()
        if old_name not in data["profiles"]:
            raise ProfileNotFound(f"profile not found: {old_name}")
        if new_name in data["profiles"]:
            raise ProfileExists(f"profile already exists: {new_name}")

        old_dir = self.profiles_root / old_name
        new_dir = self.profiles_root / new_name

        # Refuse to touch anything that does not resolve directly beneath our profiles root.
        if old_dir.resolve().parent != self.profiles_root.resolve():
            raise ProfileError(f"refusing unsafe profile path: {old_dir.resolve()}")
        if new_dir.resolve().parent != self.profiles_root.resolve():
            raise ProfileError(f"refusing unsafe profile path: {new_dir.resolve()}")

        if new_dir.exists():
            raise ProfileExists(f"target profile directory already exists: {new_dir}")

        dir_moved = False
        if old_dir.exists():
            shutil.move(str(old_dir), str(new_dir))
            dir_moved = True
            new_home = (new_dir / "home").resolve()
            _chmod_private_dir(new_dir)
            if not new_home.exists():
                new_home.mkdir(parents=True, exist_ok=True)
            _chmod_private_dir(new_home)
        else:
            self.profiles_root.mkdir(parents=True, exist_ok=True)
            _chmod_private_dir(self.data_root)
            _chmod_private_dir(self.profiles_root)
            new_dir.mkdir(parents=True, exist_ok=True)
            new_home = (new_dir / "home").resolve()
            new_home.mkdir(parents=True, exist_ok=True)
            _chmod_private_dir(new_dir)
            _chmod_private_dir(new_home)

        profile_data = data["profiles"].pop(old_name)
        profile_data["home"] = str(new_home)
        data["profiles"][new_name] = profile_data

        try:
            self._save(data)
        except Exception:
            if dir_moved and new_dir.exists():
                try:
                    shutil.move(str(new_dir), str(old_dir))
                except OSError:
                    pass
            raise

        try:
            from .cache import CacheManager

            cm = CacheManager(cache_root=self.data_root / "cache")
            cm.rename(old_name, new_name)
        except Exception:
            pass

        try:
            rot_file = self.data_root / "rotation_state.json"
            if rot_file.is_file():
                with rot_file.open("r", encoding="utf-8-sig") as handle:
                    rot_data = json.load(handle)
                if isinstance(rot_data, dict):
                    changed = False
                    if rot_data.get("last_account") == old_name:
                        rot_data["last_account"] = new_name
                        changed = True
                    if "history" in rot_data and isinstance(rot_data["history"], list):
                        new_history = [new_name if x == old_name else x for x in rot_data["history"]]
                        if new_history != rot_data["history"]:
                            rot_data["history"] = new_history
                            changed = True
                    if changed:
                        from .rotator import _atomic_save_state, RotationState

                        _atomic_save_state(rot_file, RotationState.from_dict(rot_data))
        except Exception:
            pass

        return self.get(new_name)

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


def load_accounts_file(filepath: Path | str) -> list[str]:
    """Loads a list of account identifiers or profile names from a text or JSON file.

    Handles Windows CRLF (\\r\\n), universal newlines, and UTF-8-SIG (stripping BOM).
    Ignores comments (#) and blank lines.
    """
    path = Path(filepath).resolve()
    if not path.is_file():
        raise ProfileError(f"account file does not exist: {path}")

    # Check for JSON extension
    if path.suffix.lower() == ".json":
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"cannot parse accounts JSON from {path}: {exc}") from exc
        if isinstance(data, list):
            raw_accounts = data
        elif isinstance(data, dict):
            raw_accounts = data.get("accounts", [])
        else:
            raise ProfileError(f"invalid JSON structure in {path}: expected list or dict with 'accounts'")

        accounts: list[str] = []
        for item in raw_accounts:
            if isinstance(item, str) and item.strip():
                accounts.append(item.strip())
            elif isinstance(item, dict) and "name" in item:
                accounts.append(str(item["name"]).strip())
        return accounts

    # Text / lines format
    accounts = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                accounts.append(stripped)
    except OSError as exc:
        raise ProfileError(f"cannot read account file {path}: {exc}") from exc

    return accounts

