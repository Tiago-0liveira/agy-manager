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
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", clean)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    parts = re.split(r"[^\d]+", clean)
    nums = [int(p) for p in parts if p.isdigit()]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def is_newer_version(candidate: str, current: str) -> bool:
    """Returns True if candidate version is strictly newer than current version."""
    try:
        return parse_semver(candidate) > parse_semver(current)
    except Exception:
        return False


def is_running_in_repo() -> bool:
    """Returns True if agym is running from within its source repository or worktree."""
    try:
        # Check 1: Is the loaded agym package directly inside its git repository?
        # (e.g. python3 -m agym.cli, or running from local source tree)
        pkg_dir = Path(__file__).resolve().parent
        repo_root = pkg_dir.parent
        if (repo_root / ".git").exists() and (repo_root / "agym" / "cli.py").is_file():
            return True
    except Exception:
        pass

    try:
        # Check 2: Is the current working directory inside the agym source repository?
        cwd = Path.cwd().resolve()
        for p in (cwd, *cwd.parents):
            if (p / ".git").exists() and (p / "agym" / "cli.py").is_file():
                return True
    except Exception:
        pass

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

            if sys_name == "windows" and machine in {"amd64", "x86_64", "x64"}:
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
            # Fall back to previous cached release info ONLY if not forced network query
            if not force_network and state.get("latest_version"):
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


def verify_binary(new_exe: Path, target_version: str | None = None) -> None:
    """Verifies that the downloaded binary is executable and outputs expected version.

    Steps:
    1. Check file exists and size > 0.
    2. Ensure executable permissions on POSIX.
    3. Run new binary with --version.
    4. Confirm output version matches target release.
    """
    if not new_exe.exists() or new_exe.stat().st_size == 0:
        raise RuntimeError(f"Downloaded binary {new_exe} is missing or empty.")

    if os.name != "nt":
        new_exe.chmod(0o755)

    try:
        proc = subprocess.run(
            [str(new_exe), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to execute downloaded binary: {exc}") from exc

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"Binary verification failed with exit code {proc.returncode}: {err}")

    if target_version:
        output_ver = proc.stdout.strip()
        target_clean = target_version.lstrip("v").strip()
        if parse_semver(output_ver) != parse_semver(target_clean):
            raise RuntimeError(
                f"Binary version mismatch: expected {target_clean}, got '{output_ver}'"
            )


def safe_replace_posix(current_exe: Path, new_exe: Path) -> None:
    """Safely replaces executable on Linux/macOS with rollback support.

    Flow:
      agym.new
      agym -> agym.old
      agym.new -> agym
      Restore .old if replacement fails.
      Delete .old after success.
    """
    old_exe = current_exe.with_name(f"{current_exe.name}.old")

    if old_exe.exists():
        try:
            old_exe.unlink()
        except OSError:
            pass

    # Step 1: agym -> agym.old
    try:
        current_exe.rename(old_exe)
    except Exception as exc:
        if new_exe.exists():
            try:
                new_exe.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to backup current executable to {old_exe}: {exc}") from exc

    # Step 2: agym.new -> agym
    try:
        new_exe.rename(current_exe)
    except Exception as replace_exc:
        # Step 3: Rollback - restore .old if replacement fails!
        rollback_error = None
        try:
            old_exe.rename(current_exe)
        except Exception as rb_exc:
            rollback_error = rb_exc

        if rollback_error:
            raise RuntimeError(
                f"Binary replacement failed ({replace_exc}) and rollback to restore {old_exe} also failed ({rollback_error})!"
            ) from replace_exc
        raise RuntimeError(
            f"Binary replacement failed ({replace_exc}). Restored original executable from {old_exe}."
        ) from replace_exc

    # Step 4: Success - clean up old executable
    try:
        old_exe.unlink()
    except OSError:
        pass

    print(f"Update applied successfully! Current executable updated: {current_exe}")


def spawn_windows_deferred_replacement(
    current_exe: Path,
    new_exe: Path,
    old_exe: Path | None = None,
    pid: int | None = None,
) -> None:
    """Spawns helper process on Windows to replace locked executable after AGYM exits.

    Flow:
      1. Download agym.exe.new (already done).
      2. Spawn a small helper process/script.
      3. Exit AGYM.
      4. Helper replaces the executable.
      5. Delete the old version after success.
    """
    if old_exe is None:
        old_exe = current_exe.with_name(f"{current_exe.name}.old")
    if pid is None:
        pid = os.getpid()

    bat_file = Path(tempfile.gettempdir()) / f"agym_update_{pid}.bat"
    bat_content = f"""@echo off
set PID={pid}
set TARGET="{current_exe}"
set OLD="{old_exe}"
set NEW="{new_exe}"

:wait_pid
timeout /t 1 /nobreak >nul 2>&1
tasklist /FI "PID eq %PID%" 2>nul | findstr /i "%PID%" >nul
if not errorlevel 1 goto wait_pid

set RETRY=0
:replace_loop
if exist %OLD% del /f /q %OLD% >nul 2>&1
move /y %TARGET% %OLD% >nul 2>&1
if errorlevel 1 (
    set /a RETRY+=1
    if %RETRY% lss 20 (
        timeout /t 1 /nobreak >nul 2>&1
        goto replace_loop
    )
)
move /y %NEW% %TARGET% >nul 2>&1
if exist %OLD% del /f /q %OLD% >nul 2>&1
del /f /q "%~f0" >nul 2>&1
"""
    bat_file.write_text(bat_content, encoding="utf-8")

    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    cmd = ["cmd.exe", "/c", str(bat_file)]
    subprocess.Popen(
        cmd,
        creationflags=creation_flags,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("Update verified! Replacement helper spawned. Exiting to complete update...")
    sys.exit(0)


def execute_binary_update(download_url: str, target_version: str | None = None) -> None:
    """Downloads new binary, verifies it with --version, and safely replaces current binary."""
    current_exe = Path(sys.executable).resolve()
    new_exe = current_exe.with_name(f"{current_exe.name}.new")

    print(f"Downloading update from {download_url}...")
    headers = {"User-Agent": f"agym/{__version__}"}
    req = urllib.request.Request(download_url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp, open(new_exe, "wb") as f:
        shutil.copyfileobj(resp, f)

    try:
        print("Verifying new binary...")
        verify_binary(new_exe, target_version)
    except Exception as exc:
        if new_exe.exists():
            try:
                new_exe.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Binary verification failed: {exc}") from exc

    if os.name == "nt":
        spawn_windows_deferred_replacement(current_exe, new_exe)
    else:
        safe_replace_posix(current_exe, new_exe)


def execute_python_update(wheel_url: str | None = None) -> None:
    """Updates python package via pip or pipx using release wheel."""
    if not wheel_url:
        raise RuntimeError("No wheel (.whl) asset found in latest GitHub release. Cannot update without a release wheel.")

    print(f"Installing update from {wheel_url}...")

    # Detect pipx installation
    is_pipx = False
    pipx_bin = shutil.which("pipx")
    if pipx_bin:
        prefix_parts = [p.lower() for p in Path(sys.prefix).parts]
        if "pipx" in prefix_parts and "agym" in prefix_parts:
            is_pipx = True

    if is_pipx and pipx_bin:
        cmd = [pipx_bin, "install", "--force", wheel_url]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", wheel_url]

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

    # If standalone_url or wheel_url is missing (e.g. from an old cache), refresh from network
    if (is_frozen and not standalone_url) or (not is_frozen and not release_info.get("wheel_url")):
        fresh_info = fetch_latest_release()
        if fresh_info:
            release_info = fresh_info
            standalone_url = release_info.get("standalone_url")

    try:
        if is_frozen and standalone_url:
            execute_binary_update(standalone_url, target_ver)
        elif is_frozen:
            raise RuntimeError(f"No standalone update binary is available for platform {platform.system()} {platform.machine()}")
        else:
            execute_python_update(release_info.get("wheel_url"))
        print(f"agym has been updated to version {target_ver}!")
        return 0
    except Exception as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1


def maybe_prompt_startup_update(argv: list[str]) -> None:
    """Hook invoked at startup of agym CLI.

    Bypasses non-interactive sessions, repository checkouts, fast statusline calls, and 24h dismissed updates.
    """
    if os.environ.get("AGYM_NO_UPDATE_CHECK") == "1":
        return

    # Skip when running inside the source repository (e.g. python3 -m agym.cli)
    if is_running_in_repo():
        return

    # Skip if non-interactive
    if not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
        return

    # Skip for statusline, update, help, version, or json flags
    if argv:
        for arg in argv:
            arg_lower = arg.lower()
            if arg_lower in {"--help", "-h", "help", "--version", "-v", "--json", "-j", "statusline", "update", "--statusline-render"}:
                return

    try:
        release_info, is_newer = check_for_updates(force_network=False)
        if not release_info or not is_newer:
            return

        if not should_prompt_user(release_info):
            return

        target_ver = release_info["version"]
        print(f"[agym] Update available: {__version__} -> {target_ver}")
        sys.stdout.write("Update now? [y/N]: ")
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
    except (KeyboardInterrupt, EOFError):
        pass
    except Exception:
        # Never break or block the user command if update check fails
        pass


def build_update_parser() -> argparse.ArgumentParser:
    usage = """update
  [--check]
  [-f | --force]"""
    parser = argparse.ArgumentParser(
        prog="agym update",
        usage=usage,
        description="Check for and install updates to agym.",
        add_help=True,
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
    return parser


def run_update_cli(argv: list[str]) -> int:
    """Entry point for 'agym update' command."""
    parser = build_update_parser()
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
