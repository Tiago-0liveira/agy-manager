"""Desktop auth broker for native Antigravity profile authentication and status verification."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

from agym.council.models import (
    AccountAuthStatus,
    AccountStatus,
    ModelDescriptor,
    ProviderType,
)
from agym.council.providers.antigravity import AntigravityProviderAdapter
from agym.council.providers.base import ProviderAdapter
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.storage import get_account, update_account
from agym.profiles import ProfileStore


def is_gui_available() -> bool:
    """Detect whether a graphical display environment is available."""
    if os.name == "nt":
        # Windows GUI desktop is generally available unless running as non-interactive service
        return True
    if sys.platform == "darwin":
        return True
    # Linux / BSD: check DISPLAY or WAYLAND_DISPLAY
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def find_terminal_emulator() -> tuple[str, list[str]] | None:
    """Find an installed terminal emulator command pattern."""
    if os.name == "nt":
        cmd_exe = shutil.which("cmd.exe") or "cmd.exe"
        return "cmd.exe", [cmd_exe, "/c", "start", "cmd", "/k"]

    if sys.platform == "darwin":
        return "Terminal.app", ["open", "-a", "Terminal"]

    # Candidate terminal emulators on Linux/Unix
    candidates = [
        ("x-terminal-emulator", lambda c: ["x-terminal-emulator", "-e", c]),
        ("gnome-terminal", lambda c: ["gnome-terminal", "--", "bash", "-c", f"{c}; exec bash"]),
        ("konsole", lambda c: ["konsole", "-e", "bash", "-c", f"{c}; exec bash"]),
        ("xfce4-terminal", lambda c: ["xfce4-terminal", "-e", f"bash -c '{c}; exec bash'"]),
        ("alacritty", lambda c: ["alacritty", "-e", "bash", "-c", f"{c}; exec bash"]),
        ("kitty", lambda c: ["kitty", "bash", "-c", f"{c}; exec bash"]),
        ("xterm", lambda c: ["xterm", "-hold", "-e", c]),
    ]
    for term_name, cmd_builder in candidates:
        if shutil.which(term_name):
            return term_name, []
    return None


def launch_setup_terminal(profile_ref: str) -> dict[str, Any]:
    """Attempt to launch an interactive terminal running 'agym setup <profile_ref>'.

    In headless environments (WSL without X11, SSH, Docker, headless servers),
    returns a fallback payload instructing the user to run the command in their shell.

    Args:
        profile_ref: Name of the profile to configure.

    Returns:
        A dictionary with 'status' ('launched' or 'manual_required'), 'command', and 'message'.
    """
    setup_cmd = f"agym setup {profile_ref}"

    if not is_gui_available():
        return {
            "status": "manual_required",
            "command": setup_cmd,
            "message": (
                f"Headless environment detected. Please open a terminal and execute:\n"
                f"  {setup_cmd}\n"
                f"Complete Google authentication, then click 'Check Status'."
            ),
        }

    # Attempt GUI launch
    if os.name == "nt":
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "cmd", "/k", "agym", "setup", profile_ref])
            return {
                "status": "launched",
                "terminal": "cmd.exe",
                "command": setup_cmd,
                "message": f"Opened terminal to configure profile '{profile_ref}'.",
            }
        except Exception as exc:
            return {
                "status": "manual_required",
                "command": setup_cmd,
                "error": str(exc),
                "message": f"Failed to open terminal ({exc}). Please run: {setup_cmd}",
            }

    if sys.platform == "darwin":
        try:
            osa_script = f'tell app "Terminal" to do script "{setup_cmd}"'
            subprocess.Popen(["osascript", "-e", osa_script])
            return {
                "status": "launched",
                "terminal": "Terminal.app",
                "command": setup_cmd,
                "message": f"Opened Terminal to configure profile '{profile_ref}'.",
            }
        except Exception as exc:
            return {
                "status": "manual_required",
                "command": setup_cmd,
                "error": str(exc),
                "message": f"Failed to launch macOS Terminal ({exc}). Please run: {setup_cmd}",
            }

    # Linux / BSD
    term_info = find_terminal_emulator()
    if term_info is None:
        return {
            "status": "manual_required",
            "command": setup_cmd,
            "message": (
                f"No graphical terminal emulator detected. Please execute:\n"
                f"  {setup_cmd}"
            ),
        }

    term_name, _ = term_info
    try:
        if term_name == "gnome-terminal":
            cmd = ["gnome-terminal", "--", "bash", "-c", f"{setup_cmd}; exec bash"]
        elif term_name == "konsole":
            cmd = ["konsole", "-e", "bash", "-c", f"{setup_cmd}; exec bash"]
        elif term_name == "xfce4-terminal":
            cmd = ["xfce4-terminal", "-e", f"bash -c '{setup_cmd}; exec bash'"]
        elif term_name in ("alacritty", "kitty"):
            cmd = [term_name, "-e", "bash", "-c", f"{setup_cmd}; exec bash"]
        elif term_name == "xterm":
            cmd = ["xterm", "-hold", "-e", setup_cmd]
        else:
            cmd = [term_name, "-e", setup_cmd]

        subprocess.Popen(cmd, start_new_session=True)
        return {
            "status": "launched",
            "terminal": term_name,
            "command": setup_cmd,
            "message": f"Launched {term_name} for '{profile_ref}'.",
        }
    except Exception as exc:
        return {
            "status": "manual_required",
            "command": setup_cmd,
            "error": str(exc),
            "message": f"Failed to launch terminal emulator '{term_name}' ({exc}). Run: {setup_cmd}",
        }


async def check_account_probe(
    conn: sqlite3.Connection,
    account_id: str,
    provider_adapter: ProviderAdapter | None = None,
) -> AccountStatus:
    """Probe an account's live authentication status and update SQLite.

    Performs a non-billable CLI check via 'agy models' (or simulated check for fake provider).
    Does NOT infer authentication status solely from stored file existence.

    Args:
        conn: SQLite database connection.
        account_id: Account identifier.
        provider_adapter: Optional pre-configured provider adapter.

    Returns:
        Updated AccountStatus.
    """
    row = get_account(conn, account_id)
    if not row:
        return AccountStatus(
            account_ref=account_id,
            status=AccountAuthStatus.UNAVAILABLE,
            message=f"Account '{account_id}' not found in database.",
        )

    profile_ref = str(row["profile_ref"])
    provider_type = str(row["provider_type"]).lower()

    # Step 1: Verify profile entry exists in ProfileStore
    try:
        store = ProfileStore()
        prof = store.get(profile_ref)
    except Exception as exc:
        status_result = AccountStatus(
            account_ref=profile_ref,
            status=AccountAuthStatus.UNAVAILABLE,
            message=f"Profile '{profile_ref}' not found in agym profile store: {exc}",
        )
        update_account(conn, {
            "account_id": account_id,
            "auth_status": AccountAuthStatus.UNAVAILABLE.value,
            "last_auth_check": status_result.checked_at,
        })
        return status_result

    # Step 2: Live non-billable CLI probe
    if provider_adapter is None:
        if provider_type == ProviderType.FAKE.value:
            provider_adapter = FakeProviderAdapter()
        else:
            provider_adapter = AntigravityProviderAdapter()

    try:
        status_result = await provider_adapter.check_account(profile_ref)
    except Exception as exc:
        status_result = AccountStatus(
            account_ref=profile_ref,
            status=AccountAuthStatus.UNKNOWN,
            message=f"Check probe encountered unexpected error: {exc}",
        )

    # Persist updated status
    update_data: dict[str, Any] = {
        "account_id": account_id,
        "auth_status": (
            status_result.status.value
            if isinstance(status_result.status, AccountAuthStatus)
            else str(status_result.status)
        ),
        "last_auth_check": status_result.checked_at,
    }
    if status_result.cli_version:
        update_data["cli_version"] = status_result.cli_version
    if status_result.advisory_usage:
        update_data["usage_advisory"] = status_result.advisory_usage
        update_data["usage_collected_at"] = status_result.checked_at

    update_account(conn, update_data)
    return status_result


async def discover_account_models(
    conn: sqlite3.Connection,
    account_id: str,
    provider_adapter: ProviderAdapter | None = None,
) -> list[ModelDescriptor]:
    """Discover available models for an account."""
    row = get_account(conn, account_id)
    if not row:
        return []

    profile_ref = str(row["profile_ref"])
    provider_type = str(row["provider_type"]).lower()

    if provider_adapter is None:
        if provider_type == ProviderType.FAKE.value:
            provider_adapter = FakeProviderAdapter()
        else:
            provider_adapter = AntigravityProviderAdapter()

    try:
        return await provider_adapter.discover_models(profile_ref)
    except Exception:
        return []
