from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import platform
import tempfile
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger("agym.wincred")

TARGET_NAME = "gemini:antigravity"
DEFAULT_USER = "antigravity"
CREDENTIAL_FILENAME = "token.json"


def is_windows_platform() -> bool:
    if os.environ.get("AGYM_DISABLE_WINCRED") == "1":
        return False
    return os.name == "nt" or platform.system() == "Windows"


if is_windows_platform():
    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_char_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    try:
        Advapi32 = ctypes.windll.Advapi32
        CredReadW = Advapi32.CredReadW
        CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
        ]
        CredReadW.restype = wintypes.BOOL

        CredWriteW = Advapi32.CredWriteW
        CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        CredWriteW.restype = wintypes.BOOL

        CredDeleteW = Advapi32.CredDeleteW
        CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        CredDeleteW.restype = wintypes.BOOL

        CredFree = Advapi32.CredFree
        CredFree.argtypes = [ctypes.c_void_p]
        CredFree.restype = None
    except (AttributeError, OSError) as exc:
        logger.warning("Could not initialize Advapi32 credential functions: %s", exc)
        CredReadW = None  # type: ignore
        CredWriteW = None  # type: ignore
        CredDeleteW = None  # type: ignore
        CredFree = None  # type: ignore
else:
    CredReadW = None  # type: ignore
    CredWriteW = None  # type: ignore
    CredDeleteW = None  # type: ignore
    CredFree = None  # type: ignore


def wincred_read(target: str = TARGET_NAME) -> tuple[str, bytes] | None:
    """Reads a generic credential from Windows Credential Manager."""
    if not is_windows_platform() or CredReadW is None:
        return None
    pcred = ctypes.POINTER(CREDENTIAL)()
    # CRED_TYPE_GENERIC = 1
    if not CredReadW(target, 1, 0, ctypes.byref(pcred)):
        return None
    try:
        c = pcred.contents
        user = c.UserName or DEFAULT_USER
        blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        return user, blob
    finally:
        if CredFree and pcred:
            CredFree(pcred)


def wincred_write(
    target: str = TARGET_NAME,
    username: str = DEFAULT_USER,
    blob: bytes | str = b"",
) -> bool:
    """Writes a generic credential to Windows Credential Manager."""
    if not is_windows_platform() or CredWriteW is None:
        return False
    raw_blob = blob.encode("utf-8") if isinstance(blob, str) else blob
    c = CREDENTIAL()
    c.Flags = 0
    c.Type = 1  # CRED_TYPE_GENERIC
    c.TargetName = target
    c.Comment = None
    c.CredentialBlobSize = len(raw_blob)
    c.CredentialBlob = raw_blob
    c.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    c.AttributeCount = 0
    c.Attributes = None
    c.TargetAlias = None
    c.UserName = username
    return bool(CredWriteW(ctypes.byref(c), 0))


def wincred_delete(target: str = TARGET_NAME) -> bool:
    """Deletes a credential from Windows Credential Manager."""
    if not is_windows_platform() or CredDeleteW is None:
        return False
    # CRED_TYPE_GENERIC = 1
    return bool(CredDeleteW(target, 1, 0))


def get_profile_token_path(profile_home: Path | str) -> Path:
    """Resolves the isolated token storage path within a profile home directory."""
    return Path(profile_home).resolve() / ".gemini" / "antigravity-cli" / CREDENTIAL_FILENAME


def extract_email_from_blob(blob: bytes | str) -> str | None:
    """Extracts user email from an OAuth ID token JWT or JSON structure if present."""
    try:
        raw_text = blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else blob
        data = json.loads(raw_text)
        # Direct email field in JSON
        if isinstance(data, dict):
            if "email" in data and isinstance(data["email"], str):
                return data["email"]
            id_token = data.get("id_token")
            if isinstance(id_token, str) and "." in id_token:
                parts = id_token.split(".")
                if len(parts) >= 2:
                    payload_b64 = parts[1]
                    payload_b64 += "=" * (-len(payload_b64) % 4)
                    claims_text = base64.urlsafe_b64decode(payload_b64).decode("utf-8", errors="replace")
                    claims = json.loads(claims_text)
                    if isinstance(claims, dict) and "email" in claims:
                        return str(claims["email"])
    except Exception:
        pass
    return None


def save_profile_token(
    profile_home: Path | str,
    username: str = DEFAULT_USER,
    blob: bytes | str | None = None,
) -> bool:
    """Saves credential data into the profile's isolated token file."""
    if blob is None:
        read_res = wincred_read(TARGET_NAME)
        if read_res is None:
            return False
        username, raw_bytes = read_res
    else:
        raw_bytes = blob.encode("utf-8") if isinstance(blob, str) else blob

    token_path = get_profile_token_path(profile_home)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    blob_text = raw_bytes.decode("utf-8", errors="replace")
    email = extract_email_from_blob(blob_text)

    payload = {
        "target": TARGET_NAME,
        "username": username,
        "blob": blob_text,
        "email": email,
    }

    fd, tmp_name = tempfile.mkstemp(prefix=".token.", suffix=".tmp", dir=token_path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, token_path)
        logger.debug("Saved isolated profile token to %s", token_path)
        return True
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return False


def load_profile_token(profile_home: Path | str) -> tuple[str, bytes] | None:
    """Loads stored credential data from the profile's isolated token file."""
    token_path = get_profile_token_path(profile_home)
    if not token_path.is_file():
        return None
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        username = data.get("username", DEFAULT_USER)
        blob = data.get("blob", "")
        return username, blob.encode("utf-8") if isinstance(blob, str) else b""
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read token file %s: %s", token_path, exc)
        return None


def has_profile_token(profile_home: Path | str) -> bool:
    """Checks if a valid token file exists for the given profile home."""
    return load_profile_token(profile_home) is not None


def get_profile_email(profile_home: Path | str) -> str | None:
    """Returns the authenticated email associated with this profile, if recorded."""
    token_path = get_profile_token_path(profile_home)
    if not token_path.is_file():
        return None
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if data.get("email"):
                return str(data["email"])
            blob = data.get("blob", "")
            return extract_email_from_blob(blob)
    except Exception:
        pass
    return None


def sync_credentials_before_launch(profile_home: Path | str, is_setup: bool = False) -> None:
    """Prepares the Windows Credential Manager before agy is executed.

    - For setup or unauthenticated profiles: purges existing gemini:antigravity
      so agy prompts for a fresh Google sign-in instead of reusing another account.
    - For established profiles: injects the profile's isolated token into gemini:antigravity.
    """
    if not is_windows_platform():
        return

    profile_home_path = Path(profile_home).resolve()
    if is_setup:
        # In setup mode, purge any existing credential to ensure a fresh Google OAuth prompt
        wincred_delete(TARGET_NAME)
        logger.debug("Purged '%s' before setup to force fresh sign-in flow", TARGET_NAME)
        return

    token_data = load_profile_token(profile_home_path)
    if token_data is not None:
        user, blob = token_data
        wincred_write(TARGET_NAME, user, blob)
        logger.debug("Loaded isolated token for profile into '%s'", TARGET_NAME)
    else:
        # Profile has no saved token; purge shared target to trigger fresh login
        wincred_delete(TARGET_NAME)
        logger.debug("No saved token found; purged '%s'", TARGET_NAME)


def sync_credentials_after_launch(profile_home: Path | str) -> None:
    """Persists any updated or newly acquired credential from Windows Credential Manager
    into the profile's isolated token storage.
    """
    if not is_windows_platform():
        return

    profile_home_path = Path(profile_home).resolve()
    cred = wincred_read(TARGET_NAME)
    if cred is not None:
        user, blob = cred
        save_profile_token(profile_home_path, user, blob)
        logger.debug("Persisted updated '%s' to profile %s", TARGET_NAME, profile_home_path)


@contextmanager
def profile_credential_context(
    profile_home: Path | str,
    *,
    is_setup: bool = False,
) -> Generator[None, None, None]:
    """Context manager ensuring safe, isolated Windows Credential Manager access for a profile.

    Synchronizes credentials before launch and captures updated tokens on exit.
    """
    if not is_windows_platform():
        yield
        return

    sync_credentials_before_launch(profile_home, is_setup=is_setup)
    try:
        yield
    finally:
        sync_credentials_after_launch(profile_home)
