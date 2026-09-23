"""agym self-updating engine and release checker.

Provides:
- Zero-dependency GitHub Releases querying via urllib.request.
- Semver version comparison.
- Atomic state caching for update checks and user dismissals.
- 24-hour negation cooldown memory.
- Non-blocking startup update prompts.
- Manual 'agym update' command supporting standalone binaries, wheels, and venvs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .profiles import _chmod_private_dir, _default_data_root

GITHUB_REPO = "Tiago-0liveira/agy-manager"
COOLDOWN_SECONDS = 24 * 3600  # 24 hours
CACHE_TTL_SECONDS = 6 * 3600  # 6 hours cache between network queries
REQUEST_TIMEOUT_SECONDS = 2.5  # Fast timeout to never block CLI commands


def parse_semver(ver: str) -> tuple[int, int, int]:
    """Parses semver string into (major, minor, patch) tuple."""
    clean = ver.strip().lstrip("v")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", clean)
    if not m:
        # Fallback to split or zeros if not standard 3-part semver
        parts = re.split(r"[^\d]+", clean)
        nums = [int(p) for p in parts if p.isdigit()]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2])
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer_version(candidate: str, current: str) -> bool:
    """Returns True if candidate version is strictly newer than current version."""
    try:
        return parse_semver(candidate) > parse_semver(current)
    except Exception:
        return False


def get_update_cache_file() -> Path:
    return _default_data_root() / "cache" / "updater.json"


def load_update_state() -> dict[str, Any]:
    cache_file = get_update_cache_file()
    if not cache_file.exists():
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_update_state(state: dict[str, Any]) -> None:
    cache_file = get_update_cache_file()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(cache_file.parent)

    fd, tmp_name = tempfile.mkstemp(prefix=".update.", suffix=".tmp", dir=cache_file.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, cache_file)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def fetch_latest_release(
    repo: str = GITHUB_REPO,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Fetches latest release info from GitHub API with short timeout."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {
        "User-Agent": f"agym/{__version__}",
        "Accept": "application/vnd.github.v3+json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                return None
            tag = data.get("tag_name", "")
            version = tag.lstrip("v")
            html_url = data.get("html_url", "")
            body = data.get("body", "")

            assets = data.get("assets", [])
            asset_map: dict[str, str] = {}
            wheel_url = ""
            for a in assets:
                name = a.get("name", "")
                download_url = a.get("browser_download_url", "")
                if name and download_url:
                    asset_map[name] = download_url
                    if name.endswith(".whl"):
                        wheel_url = download_url

            # Determine platform standalone asset
            standalone_url = ""
            sys_name = platform.system().lower()
            machine = platform.machine().lower()

            if sys_name == "windows" and machine in {"amd64", "x86_64"}:
                standalone_url = asset_map.get("agym-windows-amd64.exe", "")
            elif sys_name == "darwin":
                if "arm" in machine or "aarch" in machine:
                    standalone_url = asset_map.get("agym-darwin-arm64", "")
                elif machine in {"amd64", "x86_64"}:
                    standalone_url = asset_map.get("agym-darwin-amd64", "")
            elif sys_name == "linux" and machine in {"amd64", "x86_64"}:
                standalone_url = asset_map.get("agym-linux-amd64", "")

            return {
                "tag": tag,
                "version": version,
                "html_url": html_url,
                "body": body,
                "assets": asset_map,
                "wheel_url": wheel_url,
                "standalone_url": standalone_url,
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 404 from GitHub releases/latest means no releases published yet
            return {
                "tag": f"v{__version__}",
                "version": __version__,
                "html_url": f"https://github.com/{repo}/releases",
                "body": "",
                "assets": {},
                "wheel_url": "",
                "standalone_url": "",
                "no_releases": True,
            }
        return None
    except Exception:
        return None


def check_for_updates(
    force_network: bool = False,
    now_ts: float | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Checks whether an update is available.

    Returns:
        (release_info_dict, is_newer)
    """
    now = now_ts if now_ts is not None else time.time()
    state = load_update_state()

    last_check = state.get("last_check_ts", 0.0)
    should_query = force_network or (now - last_check > CACHE_TTL_SECONDS)

    release_info: dict[str, Any] | None = None
    if should_query:
        fetched = fetch_latest_release()
        if fetched:
            release_info = fetched
            state["last_check_ts"] = now
            state["latest_version"] = fetched["version"]
            state["latest_tag"] = fetched["tag"]
            state["html_url"] = fetched["html_url"]
            state["standalone_url"] = fetched["standalone_url"]
            state["wheel_url"] = fetched["wheel_url"]
            save_update_state(state)
        else:
            # Keep previous cached release info if network fails
            if state.get("latest_version"):
                release_info = {
                    "version": state["latest_version"],
                    "tag": state.get("latest_tag", f"v{state['latest_version']}"),
                    "html_url": state.get("html_url", ""),
                    "standalone_url": state.get("standalone_url", ""),
                    "wheel_url": state.get("wheel_url", ""),
                }
    else:
        if state.get("latest_version"):
            release_info = {
                "version": state["latest_version"],
                "tag": state.get("latest_tag", f"v{state['latest_version']}"),
                "html_url": state.get("html_url", ""),
                "standalone_url": state.get("standalone_url", ""),
                "wheel_url": state.get("wheel_url", ""),
            }

    if not release_info:
        return None, False

    newer = is_newer_version(release_info["version"], __version__)
    return release_info, newer


def should_prompt_user(
    release_info: dict[str, Any],
    now_ts: float | None = None,
) -> bool:
    """Evaluates whether to prompt the user according to the 24-hour negation rule."""
    if not is_newer_version(release_info["version"], __version__):
        return False

    now = now_ts if now_ts is not None else time.time()
    state = load_update_state()

    dismissed_ver = state.get("dismissed_version")
    dismissed_ts = state.get("last_dismissed_ts", 0.0)

    # If the user previously negated this exact version, check 24h cooldown
    if dismissed_ver == release_info["version"]:
        if (now - dismissed_ts) < COOLDOWN_SECONDS:
            return False

    return True


def record_dismissal(version: str, now_ts: float | None = None) -> None:
    now = now_ts if now_ts is not None else time.time()
    state = load_update_state()
    state["last_dismissed_ts"] = now
    state["dismissed_version"] = version
    save_update_state(state)


def execute_binary_update(download_url: str) -> None:
    """Downloads new binary and replaces current binary atomically."""
    current_exe = Path(sys.executable).resolve()
    temp_dir = current_exe.parent
    temp_download = temp_dir / f".agym_update_{int(time.time())}.tmp"

    print(f"Downloading update from {download_url}...")
    headers = {"User-Agent": f"agym/{__version__}"}
    req = urllib.request.Request(download_url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp, open(temp_download, "wb") as f:
        shutil.copyfileobj(resp, f)

    if os.name != "nt":
        temp_download.chmod(0o755)

    if os.name == "nt":
        # Windows file locking trick: rename running exe to .old, place new exe
        old_exe = current_exe.with_suffix(".exe.old")
        if old_exe.exists():
            try:
                old_exe.unlink()
            except OSError:
                pass
        try:
            current_exe.rename(old_exe)
        except OSError:
            # Fallback copy
            pass
        temp_download.rename(current_exe)
    else:
        temp_download.replace(current_exe)

    print(f"Update applied successfully! Current executable updated: {current_exe}")


def execute_python_update(wheel_url: str | None = None) -> None:
    """Updates python package via pip."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if wheel_url:
        print(f"Installing update from {wheel_url}...")
        cmd.append(wheel_url)
    else:
        print(f"Installing update from git repository...")
        cmd.append(f"git+https://github.com/{GITHUB_REPO}.git")

    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"Pip update failed with exit code {proc.returncode}")
    print("Python package updated successfully.")


def do_update(release_info: dict[str, Any] | None = None) -> int:
    """Performs self-update."""
    if release_info is None:
        release_info, _ = check_for_updates(force_network=True)
        if not release_info:
            print("Could not retrieve latest release information. Please check your internet connection.", file=sys.stderr)
            return 1

    target_ver = release_info["version"]
    print(f"Updating agym: {__version__} -> {target_ver}...")

    # Detect execution environment
    is_frozen = getattr(sys, "frozen", False)
    standalone_url = release_info.get("standalone_url")

    try:
        if is_frozen and standalone_url:
            execute_binary_update(standalone_url)
        elif is_frozen:
            raise RuntimeError("No standalone update is available for this platform")
        else:
            execute_python_update(release_info.get("wheel_url"))
        print(f"agym has been updated to version {target_ver}!")
        return 0
    except Exception as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1


def maybe_prompt_startup_update(argv: list[str]) -> None:
    """Hook invoked at startup of agym CLI.

    Bypasses non-interactive sessions, fast statusline calls, and 24h dismissed updates.
    """
    if os.environ.get("AGYM_NO_UPDATE_CHECK") == "1":
        return

    # Skip if non-interactive
    if not sys.stdin.isatty():
        return

    # Skip for statusline, update, help, or json flags
    if argv:
        cmd0 = argv[0].lower()
        if cmd0 in {"statusline", "update", "help", "-h", "--help"}:
            return
        if any(arg in {"--json", "-j"} for arg in argv):
            return

    try:
        release_info, is_newer = check_for_updates(force_network=False)
        if not release_info or not is_newer:
            return

        if not should_prompt_user(release_info):
            return

        target_ver = release_info["version"]
        notes_url = release_info.get("html_url", "")
        print(f"\n\033[36m[agym]\033[0m A new version is available: \033[33m{__version__}\033[0m -> \033[32m{target_ver}\033[0m")
        if notes_url:
            print(f"\033[36m[agym]\033[0m Release info: {notes_url}")

        sys.stdout.write("Would you like to update now? [y/N]: ")
        sys.stdout.flush()

        # Read single line response
        ans = sys.stdin.readline().strip().lower()
        if ans in {"y", "yes"}:
            code = do_update(release_info)
            if code == 0:
                print("Update complete! Continuing with your command...\n")
        else:
            record_dismissal(target_ver)
            print("Update postponed. You will be reminded again in 24 hours.\n")
    except Exception:
        # Never break or block the user command if update check fails
        pass


def run_update_cli(argv: list[str]) -> int:
    """Entry point for 'agym update' command."""
    parser = argparse.ArgumentParser(
        prog="agym update",
        description="Check for and install updates to agym.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for updates without downloading or installing",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force reinstall / update even if already on latest version",
    )
    ns = parser.parse_args(argv)

    print("Checking for updates on GitHub...")
    release_info, is_newer = check_for_updates(force_network=True)
    if not release_info:
        print("Failed to reach GitHub Releases API. Check your network connection.", file=sys.stderr)
        return 1

    latest_ver = release_info["version"]
    if ns.check:
        print(f"Current version: {__version__}")
        print(f"Latest version:  {latest_ver}")
        if is_newer:
            print(f"A new version ({latest_ver}) is available! Run 'agym update' to install.")
        else:
            print("agym is up to date.")
        return 0

    if not is_newer and not ns.force:
        print(f"agym is already up to date ({__version__}). Use --force to reinstall.")
        return 0

    return do_update(release_info)
