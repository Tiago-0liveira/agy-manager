#!/usr/bin/env python3
"""
Antigravity Account Manager (agy-manager)
=========================================

A small, zero-dependency TUI for managing isolated Google Antigravity CLI
profiles on Windows, macOS, and Linux.

What it does:
- Add / edit / remove named Antigravity profiles.
- Give every profile its own Antigravity home/config tree.
- Force file-based credential storage for profile isolation.
- Launch each profile in a separate terminal window.
- Optionally pin a model and agent mode per profile.
- Read model quota usage with:
      agy -p "/usage" --output-format json
- Cache the latest quota snapshot for every profile.

Important:
Antigravity currently does not expose a first-class multi-account profile
selector. This manager uses an isolated HOME + GEMINI_FORCE_FILE_STORAGE=true.
That is deliberately less invasive than copying tokens in/out of the OS
keyring, but it depends on behavior of the installed agy version.

Python: 3.9+
Dependencies: standard library only.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


APP_NAME = "agy-manager"
APP_VERSION = "0.1.0"
MIN_USAGE_VERSION = (1, 1, 11)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
REVERSE = "\033[7m"


def enable_ansi() -> None:
    """Enable VT100/ANSI processing on modern Windows terminals."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        stdout = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(stdout, ctypes.byref(mode)):
            kernel32.SetConsoleMode(stdout, mode.value | 0x0004)
    except Exception:
        pass


def clear_screen() -> None:
    print("\033[2J\033[H", end="", flush=True)


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


def visible_len(value: str) -> int:
    return len(strip_ansi(value))


def fit(value: str, width: int) -> str:
    if width <= 0:
        return ""
    plain = strip_ansi(value)
    if len(plain) <= width:
        return value + (" " * (width - len(plain)))
    if width <= 1:
        return plain[:width]
    return plain[: width - 1] + "…"


def center(value: str, width: int) -> str:
    pad = max(0, width - visible_len(value))
    left = pad // 2
    return (" " * left) + value + (" " * (pad - left))


def data_root() -> Path:
    # Keep manager metadata in the user's real home, not an isolated profile home.
    return Path.home() / ".agy-manager"


DATA_DIR = data_root()
PROFILES_FILE = DATA_DIR / "profiles.json"
CACHE_FILE = DATA_DIR / "usage-cache.json"
PROFILE_DIR = DATA_DIR / "profiles"


@dataclass
class Profile:
    id: str
    name: str
    email: str = ""
    workspace: str = ""
    model: str = ""
    mode: str = ""
    notes: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Profile":
        return cls(
            id=str(value.get("id") or uuid.uuid4().hex[:12]),
            name=str(value.get("name") or "Unnamed"),
            email=str(value.get("email") or ""),
            workspace=str(value.get("workspace") or ""),
            model=str(value.get("model") or ""),
            mode=str(value.get("mode") or ""),
            notes=str(value.get("notes") or ""),
            created_at=str(value.get("created_at") or ""),
        )


class Store:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    def load_profiles(self) -> List[Profile]:
        if not PROFILES_FILE.exists():
            return []
        try:
            payload = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            values = payload.get("profiles", []) if isinstance(payload, dict) else payload
            return [Profile.from_dict(v) for v in values if isinstance(v, dict)]
        except Exception:
            return []

    def save_profiles(self, profiles: List[Profile]) -> None:
        payload = {
            "version": 1,
            "profiles": [asdict(p) for p in profiles],
        }
        atomic_write_json(PROFILES_FILE, payload)

    def load_cache(self) -> Dict[str, Any]:
        if not CACHE_FILE.exists():
            return {}
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_cache(self, cache: Dict[str, Any]) -> None:
        atomic_write_json(CACHE_FILE, cache)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def profile_home(profile: Profile) -> Path:
    return PROFILE_DIR / profile.id / "home"


def profile_runtime_dir(profile: Profile) -> Path:
    return PROFILE_DIR / profile.id / "runtime"


def ensure_profile_dirs(profile: Profile) -> None:
    home = profile_home(profile)
    runtime = profile_runtime_dir(profile)
    home.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    # This makes the isolation layout obvious if the user inspects it manually.
    note = PROFILE_DIR / profile.id / "README.txt"
    if not note.exists():
        note.write_text(
            "This directory is managed by agy-manager.\n"
            "home/ is used as the isolated HOME for this Antigravity profile.\n"
            "runtime/ contains generated launcher scripts only.\n",
            encoding="utf-8",
        )


def find_agy() -> Optional[str]:
    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return str(Path(found).resolve())

    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "agy",
        home / ".local" / "bin" / "agy.exe",
    ]
    if os.name == "nt":
        localapp = os.environ.get("LOCALAPPDATA", "")
        if localapp:
            candidates.extend(
                [
                    Path(localapp) / "agy" / "bin" / "agy.exe",
                    Path(localapp) / "agy" / "bin" / "agy",
                ]
            )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return None


def agy_version(agy: Optional[str]) -> Tuple[str, Optional[Tuple[int, ...]]]:
    if not agy:
        return "not found", None
    try:
        cp = subprocess.run(
            [agy, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        raw = (cp.stdout or cp.stderr or "").strip()
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
        parsed = tuple(int(x) for x in match.groups()) if match else None
        return raw or "unknown", parsed
    except Exception as exc:
        return f"error: {exc}", None


def build_env(profile: Profile) -> Dict[str, str]:
    """
    Build an isolated environment.

    HOME/USERPROFILE is redirected so ~/.gemini/antigravity-cli is profile-local.
    GEMINI_FORCE_FILE_STORAGE avoids the single shared OS-keyring credential slot.
    GEMINI_CLI_HOME is also set for versions that honor it.
    """
    ensure_profile_dirs(profile)
    env = os.environ.copy()
    ph = str(profile_home(profile).resolve())

    env["HOME"] = ph
    env["GEMINI_CLI_HOME"] = ph
    env["GEMINI_FORCE_FILE_STORAGE"] = "true"
    env["AGY_MANAGER_PROFILE"] = profile.name

    if os.name == "nt":
        env["USERPROFILE"] = ph

    return env


def workspace_for(profile: Profile) -> Path:
    value = (profile.workspace or "").strip()
    if value:
        path = Path(os.path.expanduser(value))
        if path.exists() and path.is_dir():
            return path.resolve()
    return Path.cwd().resolve()


def agy_args(profile: Profile) -> List[str]:
    args: List[str] = []
    if profile.model.strip():
        args += ["--model", profile.model.strip()]
    mode = profile.mode.strip()
    if mode:
        args += [f"--mode={mode}"]
    return args


def quote_cmd_windows(arg: str) -> str:
    # subprocess.list2cmdline handles Windows command-line quoting correctly.
    return subprocess.list2cmdline([arg])


def make_posix_launcher(profile: Profile, agy: str) -> Path:
    ensure_profile_dirs(profile)
    launcher = profile_runtime_dir(profile) / ("launch.command" if platform.system().lower() == "darwin" else "launch.sh")
    env = build_env(profile)
    workspace = workspace_for(profile)
    command = [agy] + agy_args(profile)

    lines = [
        "#!/bin/sh",
        f"export HOME={shlex.quote(env['HOME'])}",
        f"export GEMINI_CLI_HOME={shlex.quote(env['GEMINI_CLI_HOME'])}",
        "export GEMINI_FORCE_FILE_STORAGE=true",
        f"export AGY_MANAGER_PROFILE={shlex.quote(profile.name)}",
        f"cd {shlex.quote(str(workspace))}",
        "exec " + " ".join(shlex.quote(part) for part in command),
    ]
    launcher.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        launcher.chmod(0o700)
    except Exception:
        pass
    return launcher


def launch_profile(profile: Profile, agy: str) -> Tuple[bool, str]:
    """Launch a selected profile in a separate terminal."""
    ensure_profile_dirs(profile)
    env = build_env(profile)
    cwd = str(workspace_for(profile))
    command = [agy] + agy_args(profile)

    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
            subprocess.Popen(command, cwd=cwd, env=env, creationflags=flags)
            return True, f"Launched {profile.name} in a new Windows console."

        system = platform.system().lower()
        launcher = make_posix_launcher(profile, agy)

        if system == "darwin":
            subprocess.Popen(["open", "-a", "Terminal", str(launcher)])
            return True, f"Launched {profile.name} in Terminal."

        # Linux / BSD desktop terminals.
        candidates = [
            ("x-terminal-emulator", ["x-terminal-emulator", "-e", "sh", str(launcher)]),
            ("gnome-terminal", ["gnome-terminal", "--", "sh", str(launcher)]),
            ("konsole", ["konsole", "-e", "sh", str(launcher)]),
            ("xfce4-terminal", ["xfce4-terminal", "-e", f"sh {shlex.quote(str(launcher))}"]),
            ("xterm", ["xterm", "-e", "sh", str(launcher)]),
        ]
        for binary, cmd in candidates:
            if shutil.which(binary):
                subprocess.Popen(cmd)
                return True, f"Launched {profile.name} with {binary}."

        # If no graphical terminal is available, detach in the current terminal
        # environment as a last resort. Interactive rendering may overlap, so tell
        # the user what happened.
        subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
        return True, "No desktop terminal emulator found; started agy detached."
    except Exception as exc:
        return False, f"Launch failed: {exc}"


def token_file(profile: Profile) -> Path:
    return profile_home(profile) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def find_first_key(value: Any, keys: Iterable[str]) -> Optional[Any]:
    wanted = set(keys)
    if isinstance(value, dict):
        for key, item in value.items():
            if key in wanted and item not in (None, "", [], {}):
                return item
        for item in value.values():
            found = find_first_key(item, wanted)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_first_key(item, wanted)
            if found not in (None, "", [], {}):
                return found
    return None


def find_groups(value: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(value, dict):
        groups = value.get("groups")
        if isinstance(groups, list) and any(
            isinstance(g, dict) and isinstance(g.get("buckets"), list) for g in groups
        ):
            return [g for g in groups if isinstance(g, dict)]
        for item in value.values():
            found = find_groups(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_groups(item)
            if found:
                return found
    return None


def normalize_remaining(value: Any) -> Optional[float]:
    try:
        number = float(value)
        if 0.0 <= number <= 1.0:
            return number
    except Exception:
        return None
    return None


def parse_usage_payload(payload: Any) -> Dict[str, Any]:
    """
    Normalize several known /usage JSON shapes.

    Newer agy builds expose groups[].buckets[]; status/quota-shaped payloads
    may expose a quota mapping directly. We support both without assuming
    every build returns account or plan labels.
    """
    rows: List[Dict[str, Any]] = []
    groups = find_groups(payload)

    if groups:
        for group in groups:
            group_name = str(
                group.get("displayName")
                or group.get("display_name")
                or group.get("name")
                or "Models"
            )
            buckets = group.get("buckets") or []
            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue
                bucket_id = str(
                    bucket.get("bucketId")
                    or bucket.get("bucket_id")
                    or bucket.get("id")
                    or bucket.get("displayName")
                    or bucket.get("display_name")
                    or "quota"
                )
                label = str(
                    bucket.get("displayName")
                    or bucket.get("display_name")
                    or bucket_id
                )
                remaining = normalize_remaining(
                    bucket.get("remainingFraction", bucket.get("remaining_fraction"))
                )
                reset = (
                    bucket.get("resetTime")
                    or bucket.get("reset_time")
                    or bucket.get("resetAt")
                    or ""
                )
                window = str(bucket.get("window") or "")
                rows.append(
                    {
                        "group": group_name,
                        "id": bucket_id,
                        "label": label,
                        "window": window,
                        "remaining": remaining,
                        "reset": str(reset or ""),
                    }
                )

    if not rows:
        quota = find_first_key(payload, ["quota"])
        if isinstance(quota, dict):
            for bucket_id, bucket in quota.items():
                if not isinstance(bucket, dict):
                    continue
                remaining = normalize_remaining(
                    bucket.get("remaining_fraction", bucket.get("remainingFraction"))
                )
                reset = bucket.get("reset_time") or bucket.get("resetTime") or ""
                rows.append(
                    {
                        "group": "Quota",
                        "id": str(bucket_id),
                        "label": str(bucket_id),
                        "window": "",
                        "remaining": remaining,
                        "reset": str(reset),
                    }
                )

    email = find_first_key(payload, ["email", "accountEmail", "account_email"])
    plan = find_first_key(
        payload,
        ["plan", "planName", "plan_name", "paidTier", "paid_tier", "tierName", "tier_name"],
    )

    def label(value: Any) -> str:
        if isinstance(value, (str, int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ("displayName", "display_name", "name", "id", "tierId", "tier_id"):
                item = value.get(key)
                if isinstance(item, (str, int, float)) and str(item).strip():
                    return str(item)
        return ""

    return {
        "rows": rows,
        "email": label(email),
        "plan": label(plan),
    }


def fetch_usage(profile: Profile, agy: str, timeout: int = 35) -> Dict[str, Any]:
    # Do not let a quota check silently fall through to a shared OS-keyring
    # credential. If file-based isolation did not produce a profile-local token,
    # the account identity is not trustworthy for this manager.
    if not token_file(profile).exists():
        return {
            "ok": False,
            "error": "No isolated auth file for this profile. Press I, sign in, then retry.",
            "rows": [],
        }

    env = build_env(profile)
    cmd = [agy, "-p", "/usage", "--output-format", "json"]
    started = time.time()
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(workspace_for(profile)),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"Timed out after {timeout}s. Press I to authenticate this profile.",
            "rows": [],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "rows": []}

    stdout = (cp.stdout or "").strip()
    stderr = (cp.stderr or "").strip()

    if cp.returncode != 0:
        message = stderr or stdout or f"agy exited with code {cp.returncode}"
        message = " ".join(message.split())
        return {"ok": False, "error": message[:500], "rows": []}

    if not stdout:
        return {"ok": False, "error": "agy returned no usage JSON.", "rows": []}

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Some builds may place a clean JSON object on the last line.
        candidate = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                candidate = line
                break
        if not candidate:
            return {
                "ok": False,
                "error": "Could not parse agy usage output as JSON.",
                "rows": [],
                "raw": stdout[:1000],
            }
        try:
            payload = json.loads(candidate)
        except Exception:
            return {
                "ok": False,
                "error": "Could not parse agy usage output as JSON.",
                "rows": [],
            }

    normalized = parse_usage_payload(payload)
    normalized.update(
        {
            "ok": bool(normalized.get("rows")),
            "error": "" if normalized.get("rows") else "No recognized quota buckets in agy response.",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(time.time() - started, 2),
        }
    )
    return normalized


def pct_remaining(row: Dict[str, Any]) -> str:
    remaining = row.get("remaining")
    if remaining is None:
        return "disabled/?"
    return f"{remaining * 100:.0f}% left"


def usage_summary(snapshot: Optional[Dict[str, Any]]) -> str:
    if not snapshot:
        return "not checked"
    if not snapshot.get("ok"):
        return "error"
    rows = snapshot.get("rows") or []
    by_id = {str(r.get("id")): r for r in rows if isinstance(r, dict)}
    parts: List[str] = []
    for key, label in [
        ("gemini-5h", "G5h"),
        ("gemini-weekly", "Gwk"),
        ("3p-5h", "3P5h"),
        ("3p-weekly", "3Pwk"),
    ]:
        if key in by_id:
            rem = by_id[key].get("remaining")
            parts.append(f"{label}:{'?' if rem is None else f'{rem*100:.0f}%'}")
    if parts:
        return " ".join(parts)
    if rows:
        r = rows[0]
        return f"{r.get('id', 'quota')} {pct_remaining(r)}"
    return "no quota rows"


def human_time(iso: str) -> str:
    if not iso:
        return "-"
    try:
        text = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:24]


def read_key() -> str:
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {
                "H": "UP",
                "P": "DOWN",
                "K": "LEFT",
                "M": "RIGHT",
                "I": "PGUP",
                "Q": "PGDN",
            }.get(code, code)
        if ch == "\x1b":
            return "ESC"
        return ch

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "\x1b":
            seq = ""
            # Read the rest of a likely escape sequence without blocking.
            for _ in range(4):
                ready, _, _ = select.select([fd], [], [], 0.035)
                if not ready:
                    break
                seq += os.read(fd, 1).decode(errors="ignore")
            return {
                "[A": "UP",
                "[B": "DOWN",
                "[C": "RIGHT",
                "[D": "LEFT",
                "[5~": "PGUP",
                "[6~": "PGDN",
            }.get(seq, "ESC")
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def prompt_value(label: str, current: str = "", required: bool = False) -> str:
    suffix = f" [{current}]" if current else ""
    while True:
        try:
            value = input(f"{label}{suffix}: ").strip()
        except EOFError:
            return current
        if not value:
            value = current
        if value or not required:
            return value
        print("A value is required.")


def prompt_profile(existing: Optional[Profile] = None) -> Optional[Profile]:
    clear_screen()
    print(f"{BOLD}{CYAN}Antigravity profile {'editor' if existing else 'setup'}{RESET}\n")
    print(
        textwrap.fill(
            "Each profile gets an isolated Antigravity HOME and file-based auth store. "
            "On first launch, sign in to the Google account you want attached to this profile.",
            width=max(60, min(100, shutil.get_terminal_size((100, 30)).columns - 2)),
        )
    )
    print()

    current = existing or Profile(id=uuid.uuid4().hex[:12], name="")
    name = prompt_value("Profile name", current.name, required=True)
    email = prompt_value("Account email/label (optional)", current.email)
    workspace = prompt_value(
        "Default workspace",
        current.workspace or str(Path.cwd()),
    )
    model = prompt_value(
        "Default model slug (blank = agy default)",
        current.model,
    )
    mode = prompt_value(
        "Agent mode: default / plan / accept-edits (blank = agy default)",
        current.mode,
    )
    if mode and mode not in {"default", "plan", "accept-edits"}:
        print(f"{YELLOW}Unknown mode '{mode}'. It will still be passed to agy.{RESET}")
    notes = prompt_value("Notes (optional)", current.notes)

    profile = Profile(
        id=current.id,
        name=name,
        email=email,
        workspace=workspace,
        model=model,
        mode=mode,
        notes=notes,
        created_at=current.created_at or datetime.now(timezone.utc).isoformat(),
    )
    ensure_profile_dirs(profile)
    return profile


def wait_key(message: str = "Press any key to continue...") -> None:
    print(f"\n{DIM}{message}{RESET}", end="", flush=True)
    read_key()


class App:
    def __init__(self) -> None:
        self.store = Store()
        self.profiles = self.store.load_profiles()
        self.cache = self.store.load_cache()
        self.selected = 0
        self.status = "Ready."
        self.agy = find_agy()
        self.version_text, self.version_tuple = agy_version(self.agy)

    def persist(self) -> None:
        self.store.save_profiles(self.profiles)
        self.store.save_cache(self.cache)

    def selected_profile(self) -> Optional[Profile]:
        if not self.profiles:
            return None
        self.selected = max(0, min(self.selected, len(self.profiles) - 1))
        return self.profiles[self.selected]

    def render(self) -> None:
        clear_screen()
        cols, rows = shutil.get_terminal_size((110, 34))
        cols = max(72, cols)

        title = f"{BOLD}{CYAN} Antigravity Account Manager {RESET} {DIM}v{APP_VERSION}{RESET}"
        agy_label = (
            f"{GREEN}agy {self.version_text}{RESET}"
            if self.agy
            else f"{RED}agy not found{RESET}"
        )
        print(fit(title, max(1, cols - visible_len(agy_label) - 1)) + " " + agy_label)
        print("─" * cols)

        if not self.agy:
            print(
                f"{RED}Antigravity CLI was not found.{RESET} Install agy, then restart this manager.\n"
            )

        if self.agy and self.version_tuple and self.version_tuple < MIN_USAGE_VERSION:
            print(
                f"{YELLOW}Usage JSON needs agy >= {'.'.join(map(str, MIN_USAGE_VERSION))}; "
                f"launching profiles still works.{RESET}"
            )

        if not self.profiles:
            print()
            print(center(f"{BOLD}No profiles yet{RESET}", cols))
            print(center("Press A to add your first Antigravity account.", cols))
            print()
        else:
            name_w = min(24, max(14, cols // 6))
            acct_w = min(30, max(18, cols // 5))
            model_w = min(24, max(12, cols // 6))
            usage_w = max(22, cols - name_w - acct_w - model_w - 10)

            header = (
                f"  {'PROFILE':<{name_w}} "
                f"{'ACCOUNT/LABEL':<{acct_w}} "
                f"{'MODEL':<{model_w}} "
                f"{'LATEST USAGE':<{usage_w}}"
            )
            print(f"{BOLD}{header[:cols]}{RESET}")
            print("─" * cols)

            max_list = max(4, min(len(self.profiles), rows // 3 + 3))
            start = 0
            if self.selected >= max_list:
                start = self.selected - max_list + 1
            shown = self.profiles[start : start + max_list]

            for offset, profile in enumerate(shown):
                idx = start + offset
                marker = "▶" if idx == self.selected else " "
                snapshot = self.cache.get(profile.id)
                account = profile.email or "(sign in / unlabeled)"
                model = profile.model or "(agy default)"
                usage = usage_summary(snapshot)
                line = (
                    f"{marker} {profile.name:<{name_w}} "
                    f"{account:<{acct_w}} "
                    f"{model:<{model_w}} "
                    f"{usage:<{usage_w}}"
                )
                line = fit(line, cols)
                if idx == self.selected:
                    print(REVERSE + line + RESET)
                else:
                    print(line)

            selected = self.selected_profile()
            if selected:
                print()
                print("─" * cols)
                self.render_usage(selected, cols, rows)

        print()
        print("─" * cols)
        help_line = (
            f"{BOLD}↑/↓{RESET} select   "
            f"{BOLD}A{RESET} add   {BOLD}E{RESET} edit   {BOLD}D{RESET} delete   "
            f"{BOLD}I{RESET} sign-in/setup   {BOLD}L{RESET} launch   "
            f"{BOLD}U{RESET} usage   {BOLD}R{RESET} refresh all   {BOLD}Q{RESET} quit"
        )
        print(fit(help_line, cols))
        status_color = RED if self.status.lower().startswith("error") else DIM
        print(fit(f"{status_color}{self.status}{RESET}", cols), end="", flush=True)

    def render_usage(self, profile: Profile, cols: int, rows: int) -> None:
        snapshot = self.cache.get(profile.id)
        home = profile_home(profile)
        token_state = f"{GREEN}auth file found{RESET}" if token_file(profile).exists() else f"{YELLOW}auth not confirmed{RESET}"
        print(
            fit(
                f"{BOLD}{profile.name}{RESET}  •  {token_state}  •  "
                f"workspace: {workspace_for(profile)}",
                cols,
            )
        )

        if not snapshot:
            print(f"{DIM}No quota snapshot. Press U to fetch this account's usage.{RESET}")
            return
        if not snapshot.get("ok"):
            print(f"{RED}Usage error:{RESET} {snapshot.get('error', 'unknown error')}")
            return

        meta: List[str] = []
        if snapshot.get("email"):
            meta.append(str(snapshot["email"]))
        if snapshot.get("plan"):
            meta.append(str(snapshot["plan"]))
        if snapshot.get("fetched_at"):
            try:
                fetched = datetime.fromisoformat(snapshot["fetched_at"]).astimezone()
                meta.append("updated " + fetched.strftime("%H:%M:%S"))
            except Exception:
                pass
        if meta:
            print(f"{DIM}{' • '.join(meta)}{RESET}")

        quota_rows = snapshot.get("rows") or []
        max_rows = max(2, min(6, rows // 5))
        for row in quota_rows[:max_rows]:
            remaining = row.get("remaining")
            if remaining is None:
                rem_text = "unknown/disabled"
                used_text = ""
            else:
                rem_text = f"{remaining * 100:5.1f}% left"
                used_text = f"{(1.0 - remaining) * 100:5.1f}% used"
            reset = human_time(str(row.get("reset") or ""))
            group = str(row.get("group") or "")
            label = str(row.get("label") or row.get("id") or "quota")
            print(
                fit(
                    f"  {BOLD}{group}{RESET} / {label}: "
                    f"{GREEN}{rem_text}{RESET}  {DIM}{used_text}  reset {reset}{RESET}",
                    cols,
                )
            )

    def add(self) -> None:
        profile = prompt_profile()
        if not profile:
            self.status = "Add cancelled."
            return
        self.profiles.append(profile)
        self.selected = len(self.profiles) - 1
        self.persist()
        self.status = f"Added profile '{profile.name}'. Press I to sign in."

    def edit(self) -> None:
        profile = self.selected_profile()
        if not profile:
            self.status = "No profile selected."
            return
        updated = prompt_profile(profile)
        if updated:
            self.profiles[self.selected] = updated
            self.persist()
            self.status = f"Updated '{updated.name}'."

    def delete(self) -> None:
        profile = self.selected_profile()
        if not profile:
            self.status = "No profile selected."
            return
        clear_screen()
        print(f"{BOLD}{RED}Remove profile: {profile.name}{RESET}\n")
        print("1) Remove it from the manager only (keeps isolated account data).")
        print("2) Remove it and delete its isolated HOME/account data.")
        print("Anything else cancels.")
        choice = input("\nChoice [1/2]: ").strip()
        if choice not in {"1", "2"}:
            self.status = "Delete cancelled."
            return

        removed = self.profiles.pop(self.selected)
        self.cache.pop(removed.id, None)
        if choice == "2":
            try:
                shutil.rmtree(PROFILE_DIR / removed.id)
                self.status = f"Removed '{removed.name}' and deleted its isolated data."
            except Exception as exc:
                self.status = f"Removed profile metadata; data deletion failed: {exc}"
        else:
            self.status = f"Removed '{removed.name}' from the manager; data kept."
        self.selected = max(0, self.selected - 1)
        self.persist()

    def launch(self, setup: bool = False) -> None:
        profile = self.selected_profile()
        if not profile:
            self.status = "No profile selected."
            return
        if not self.agy:
            self.status = "Error: agy executable not found."
            return
        ok, message = launch_profile(profile, self.agy)
        if setup and ok:
            message += " Complete Google sign-in there; this profile will reuse that isolated login."
        self.status = message if ok else "Error: " + message

    def refresh_one(self) -> None:
        profile = self.selected_profile()
        if not profile:
            self.status = "No profile selected."
            return
        if not self.agy:
            self.status = "Error: agy executable not found."
            return
        self.status = f"Fetching usage for {profile.name}..."
        self.render()
        snapshot = fetch_usage(profile, self.agy)
        self.cache[profile.id] = snapshot
        self.persist()
        if snapshot.get("ok"):
            self.status = f"Updated usage for {profile.name} in {snapshot.get('elapsed', '?')}s."
        else:
            self.status = f"Error fetching {profile.name}: {snapshot.get('error', 'unknown error')}"

    def refresh_all(self) -> None:
        if not self.profiles:
            self.status = "No profiles to refresh."
            return
        if not self.agy:
            self.status = "Error: agy executable not found."
            return
        ok_count = 0
        for index, profile in enumerate(self.profiles, start=1):
            self.status = f"Refreshing {index}/{len(self.profiles)}: {profile.name}..."
            self.render()
            snapshot = fetch_usage(profile, self.agy)
            self.cache[profile.id] = snapshot
            if snapshot.get("ok"):
                ok_count += 1
            self.store.save_cache(self.cache)
        self.status = f"Refresh complete: {ok_count}/{len(self.profiles)} profiles returned quota data."

    def run(self) -> None:
        enable_ansi()
        try:
            while True:
                self.render()
                key = read_key()
                if key == "UP":
                    if self.profiles:
                        self.selected = (self.selected - 1) % len(self.profiles)
                elif key == "DOWN":
                    if self.profiles:
                        self.selected = (self.selected + 1) % len(self.profiles)
                elif key in {"q", "Q", "\x03"}:
                    clear_screen()
                    print("Bye.")
                    return
                elif key in {"a", "A"}:
                    self.add()
                elif key in {"e", "E"}:
                    self.edit()
                elif key in {"d", "D"}:
                    self.delete()
                elif key in {"i", "I"}:
                    self.launch(setup=True)
                elif key in {"l", "L", "\r", "\n"}:
                    self.launch(setup=False)
                elif key in {"u", "U"}:
                    self.refresh_one()
                elif key in {"r", "R"}:
                    self.refresh_all()
        except KeyboardInterrupt:
            clear_screen()
            print("Bye.")


def doctor() -> int:
    enable_ansi()
    store = Store()
    profiles = store.load_profiles()
    agy = find_agy()
    version_text, version_tuple = agy_version(agy)

    print(f"{BOLD}agy-manager doctor{RESET}")
    print(f"Platform : {platform.platform()}")
    print(f"Python   : {sys.version.split()[0]}")
    print(f"Data dir : {DATA_DIR}")
    print(f"agy      : {agy or 'NOT FOUND'}")
    print(f"version  : {version_text}")
    print(f"profiles : {len(profiles)}")
    if version_tuple and version_tuple < MIN_USAGE_VERSION:
        print(
            f"{YELLOW}warning  : /usage JSON is expected on agy >= "
            f"{'.'.join(map(str, MIN_USAGE_VERSION))}{RESET}"
        )

    for p in profiles:
        print(
            f"- {p.name}: home={profile_home(p)} "
            f"auth_file={'yes' if token_file(p).exists() else 'no'} "
            f"workspace={workspace_for(p)}"
        )
    return 0 if agy else 1


def print_help() -> None:
    print(
        f"""agy-manager {APP_VERSION}

Usage:
  python {Path(sys.argv[0]).name}
  python {Path(sys.argv[0]).name} --doctor
  python {Path(sys.argv[0]).name} --help

Keys:
  Up/Down  Select profile
  A        Add profile
  E        Edit profile
  D        Remove profile
  I        Launch profile for first-time sign-in/setup
  L/Enter  Launch selected profile in a new terminal
  U        Fetch selected profile quota usage
  R        Refresh quota usage for every profile
  Q        Quit

Profile data:
  {DATA_DIR}

Security model:
  The manager never reads, copies, prints, or exports OAuth token contents.
  Each profile is launched with an isolated HOME and
  GEMINI_FORCE_FILE_STORAGE=true.
"""
    )


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print_help()
        return 0
    if "--doctor" in sys.argv:
        return doctor()
    App().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
