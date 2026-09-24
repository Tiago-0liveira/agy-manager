"""macOS Keychain credential management and isolation for Antigravity profiles.

On macOS, agy uses zalando/go-keyring which delegates to /usr/bin/security to manage
the 'antigravity' generic password in the user's default keychain.
When agym isolates a profile by setting HOME to the profile's home directory,
macOS searches for keychains under $HOME/Library/Keychains/login.keychain-db.
If this directory or keychain does not exist, macOS displays a system modal dialog:
  "Keychain Not Found: A keychain cannot be found to store 'antigravity.'"

This module initializes and manages an isolated login keychain for each profile
under $profile_home/Library/Keychains/login.keychain-db, ensuring:
1. No system popup appears during profile setup or launch.
2. Credentials remain fully isolated per profile (no shared host keychain pollution).
3. Tokens are synchronized between the isolated keychain and profile file storage.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from .wincred import (
    DEFAULT_USER,
    extract_email_from_blob,
    get_profile_token_path,
    save_profile_token,
)

logger = logging.getLogger("agym.maccred")

OAUTH_TOKEN_FILENAME = "antigravity-oauth-token"
KEYCHAIN_NAME = "login.keychain-db"
MAC_TARGET_NAME = "antigravity"


def is_darwin_platform() -> bool:
    """Returns True if the current operating system is macOS and maccred is not disabled."""
    if os.environ.get("AGYM_DISABLE_MACCRED") == "1":
        return False
    return platform.system() == "Darwin"


def get_profile_keychain_path(profile_home: Path | str) -> Path:
    """Resolves the isolated keychain database path within a profile home directory."""
    return Path(profile_home).resolve() / "Library" / "Keychains" / KEYCHAIN_NAME


_subprocess_run = subprocess.run


def _run_security_cmd(
    args: list[str],
    home: Path | str,
    *,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any] | None:
    """Invokes /usr/bin/security in an isolated HOME environment."""
    env = dict(os.environ, HOME=str(Path(home).resolve()))
    try:
        return _subprocess_run(
            ["security", *args],
            env=env,
            capture_output=capture_output,
            text=text,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.debug("Failed to execute security command %s: %s", args, exc)
        return None


def setup_profile_keychain(profile_home: Path | str, is_setup: bool = False) -> Path | None:
    """Initializes and configures an isolated login keychain for the profile on macOS.

    Ensures that when agy calls /usr/bin/security, it finds a valid default keychain
    in the profile's isolated HOME and does not trigger 'Keychain Not Found' dialogs.
    """
    if not is_darwin_platform():
        return None

    home_path = Path(profile_home).resolve()
    kc_path = get_profile_keychain_path(home_path)
    kc_dir = kc_path.parent
    try:
        kc_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("Could not create keychain directory %s: %s", kc_dir, exc)
        return None

    if is_setup and kc_path.exists():
        maccred_delete(home_path, MAC_TARGET_NAME)

    if not kc_path.exists():
        # Create a passwordless keychain for this profile
        _run_security_cmd(["create-keychain", "-p", "", str(kc_path)], home_path)
        # Disable auto-locking on sleep or timeout for seamless non-interactive access
        _run_security_cmd(["set-keychain-settings", str(kc_path)], home_path)

    # Configure this profile's keychain as default and in search list
    _run_security_cmd(["default-keychain", "-s", str(kc_path)], home_path)
    _run_security_cmd(["list-keychains", "-s", str(kc_path)], home_path)

    return kc_path


def maccred_read(profile_home: Path | str, target: str = MAC_TARGET_NAME) -> tuple[str, bytes] | None:
    """Reads a generic password credential from the profile's isolated macOS keychain."""
    if not is_darwin_platform():
        return None

    home_path = Path(profile_home).resolve()
    if not get_profile_keychain_path(home_path).is_file():
        return None

    res = _run_security_cmd(
        ["find-generic-password", "-s", target, "-a", DEFAULT_USER, "-w"],
        home_path,
    )
    if res and res.returncode == 0 and isinstance(res.stdout, str) and res.stdout.strip():
        blob = res.stdout.rstrip("\r\n").encode("utf-8")
        return DEFAULT_USER, blob

    # Fallback without explicit account name in case agy stored under a different user
    res = _run_security_cmd(["find-generic-password", "-s", target, "-w"], home_path)
    if res and res.returncode == 0 and isinstance(res.stdout, str) and res.stdout.strip():
        blob = res.stdout.rstrip("\r\n").encode("utf-8")
        return DEFAULT_USER, blob

    return None


def maccred_write(
    profile_home: Path | str,
    target: str = MAC_TARGET_NAME,
    user: str = DEFAULT_USER,
    blob: bytes | str = b"",
) -> bool:
    """Stores a generic password credential in the profile's isolated macOS keychain."""
    if not is_darwin_platform():
        return False

    home_path = Path(profile_home).resolve()
    setup_profile_keychain(home_path)
    blob_str = blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else str(blob)
    res = _run_security_cmd(
        ["add-generic-password", "-U", "-s", target, "-a", user, "-w", blob_str],
        home_path,
    )
    return res is not None and res.returncode == 0


def maccred_delete(profile_home: Path | str, target: str = MAC_TARGET_NAME) -> bool:
    """Deletes a generic password credential from the profile's isolated macOS keychain."""
    if not is_darwin_platform():
        return False

    home_path = Path(profile_home).resolve()
    if not get_profile_keychain_path(home_path).is_file():
        return True

    res = _run_security_cmd(["delete-generic-password", "-s", target], home_path)
    # returncode 0 = deleted; 44 = item not found in keychain (already clear)
    return res is not None and res.returncode in (0, 44)


def sync_mac_credentials_before_launch(profile_home: Path | str, is_setup: bool = False) -> None:
    """Prepares the isolated macOS Keychain before agy is executed.

    - Ensures the isolated login keychain exists and is registered as default.
    - For setup profiles: purges existing credentials to ensure a fresh Google OAuth prompt.
    - For existing profiles: if the keychain is empty but token files exist, pre-populates
      the keychain so agy finds credentials immediately without falling back.
    """
    if not is_darwin_platform():
        return

    home_path = Path(profile_home).resolve()
    setup_profile_keychain(home_path, is_setup=is_setup)

    if is_setup:
        maccred_delete(home_path, MAC_TARGET_NAME)
        oauth_path = home_path / ".gemini" / "antigravity-cli" / OAUTH_TOKEN_FILENAME
        if oauth_path.is_file():
            try:
                oauth_path.unlink()
            except OSError:
                pass
        token_path = get_profile_token_path(home_path)
        if token_path.is_file():
            try:
                token_path.unlink()
            except OSError:
                pass
        return

    existing_kc = maccred_read(home_path, MAC_TARGET_NAME)
    if existing_kc is None:
        oauth_path = home_path / ".gemini" / "antigravity-cli" / OAUTH_TOKEN_FILENAME
        if oauth_path.is_file():
            try:
                raw_text = oauth_path.read_text(encoding="utf-8").strip()
                if raw_text:
                    maccred_write(home_path, MAC_TARGET_NAME, DEFAULT_USER, raw_text)
                    logger.debug("Pre-populated profile keychain from %s", oauth_path)
            except OSError as exc:
                logger.debug("Could not read %s for keychain pre-population: %s", oauth_path, exc)
        else:
            token_path = get_profile_token_path(home_path)
            if token_path.is_file():
                try:
                    with open(token_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    blob = data.get("blob", "")
                    if blob:
                        maccred_write(home_path, MAC_TARGET_NAME, DEFAULT_USER, blob)
                        logger.debug("Pre-populated profile keychain from %s", token_path)
                except Exception as exc:
                    logger.debug("Could not read %s for keychain pre-population: %s", token_path, exc)


def sync_mac_credentials_after_launch(profile_home: Path | str, is_setup: bool = False) -> None:
    """Captures any newly acquired or refreshed credentials from macOS Keychain on agy exit,
    persisting them to profile file storage (antigravity-oauth-token and token.json).
    """
    if not is_darwin_platform():
        return

    home_path = Path(profile_home).resolve()
    cred = maccred_read(home_path, MAC_TARGET_NAME)
    if cred is not None:
        user, blob = cred
        if not isinstance(blob, (bytes, str)):
            return
        raw_text = blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else blob
        new_email = extract_email_from_blob(raw_text)

        # Cross-account protection if not setup
        if not is_setup:
            from .wincred import get_profile_email
            existing_email = get_profile_email(home_path)
            if existing_email and new_email and existing_email.strip().lower() != new_email.strip().lower():
                logger.warning(
                    "Prevented token cross-contamination for profile at '%s': existing account '%s' does not match credential account '%s'",
                    home_path,
                    existing_email,
                    new_email,
                )
                return

        # Save to token.json
        save_profile_token(home_path, user, blob)

        # Also write/update antigravity-oauth-token if valid JSON
        oauth_path = home_path / ".gemini" / "antigravity-cli" / OAUTH_TOKEN_FILENAME
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict) and ("token" in parsed or "access_token" in parsed):
                oauth_path.parent.mkdir(parents=True, exist_ok=True)
                with open(oauth_path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, indent=2)
                    f.write("\n")
                if os.name != "nt":
                    oauth_path.chmod(0o600)
        except Exception:
            pass
