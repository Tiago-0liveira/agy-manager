"""Exclusive profile leasing management for AGYM orchestration.

Manages persistent profile leases in `<AGYM_DATA_HOME>/orchestrator/leases/`.
Guarantees atomic cross-process lease acquisition, verified ownership release,
and conservative stale lease detection.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agym.profiles import _chmod_private_dir, _default_data_root
from agym.orchestration.contracts import LeaseId, ProfileLease, RunId, WorkerId
from agym.orchestration.locking import FileLock

logger = logging.getLogger(__name__)


class LeaseError(RuntimeError):
    """Base exception for lease operations."""


class ProfileAlreadyLeasedError(LeaseError):
    """Raised when a profile is already actively leased."""


class LeaseNotFoundError(LeaseError):
    """Raised when a lease record cannot be found."""


class LeaseOwnershipError(LeaseError):
    """Raised when a release is attempted by a non-owner."""


class LeaseReadError(LeaseError):
    """Raised when an existing lease file exists but cannot be read."""


class CorruptLeaseError(LeaseReadError):
    """Raised when an existing lease file contains corrupt or invalid data."""


def is_pid_alive(pid: int | None) -> bool:
    """Checks whether a process with the given PID is currently alive on the system."""
    if pid is None or pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle == 0:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


def is_lease_stale(
    lease: ProfileLease,
    timeout_seconds: float = 300.0,
    now: datetime | None = None,
) -> bool:
    """Conservatively checks if a lease is stale.

    Deterministic reclamation invariants:
    1. If an owner PID is recorded and conclusively dead, the lease is immediately stale,
       regardless of how recently it was acquired or updated.
    2. If the owner PID is alive, the lease is NEVER stolen while the process is running.
    3. If no PID was recorded, lease age/heartbeat timeout is evaluated.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # 1. Conclusively dead owner PID is stale immediately (W4-10)
    if lease.pid is not None:
        if not is_pid_alive(lease.pid):
            return True
        # Owner process is alive; never steal lease
        return False

    # 2. No PID recorded: fallback to age / heartbeat timeout
    ts_str = lease.heartbeat_at or lease.acquired_at
    if not ts_str:
        return True

    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (now - dt).total_seconds()
    except (ValueError, TypeError):
        return True

    return age >= timeout_seconds


class ProfileLeaseManager:
    """Manages exclusive leases on AGYM profiles across processes."""

    def __init__(
        self,
        lease_root: Path | str | None = None,
        stale_timeout_seconds: float = 300.0,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        if lease_root is None:
            self.lease_root = (_default_data_root() / "orchestrator" / "leases").resolve()
        else:
            self.lease_root = Path(lease_root).resolve()

        self.stale_timeout_seconds = max(0.0, stale_timeout_seconds)
        self.lock_timeout_seconds = max(0.1, lock_timeout_seconds)
        self._lock_path = self.lease_root / ".leases.lock"

    def _ensure_dirs(self) -> None:
        self.lease_root.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(self.lease_root)

    def _profile_path(self, profile_name: str) -> Path:
        return self.lease_root / f"{profile_name}.json"

    def _write_lease_atomic(self, path: Path, lease: ProfileLease) -> None:
        self._ensure_dirs()
        fd, tmp_name = tempfile.mkstemp(prefix=".lease.", suffix=".tmp", dir=self.lease_root)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(lease.to_dict(), handle, indent=2, sort_keys=True)
                handle.write("\n")
            if os.name != "nt":
                try:
                    tmp.chmod(0o600)
                except OSError:
                    pass
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _read_lease_file(self, path: Path, raise_on_error: bool = False) -> ProfileLease | None:
        if not path.exists():
            return None
        if not path.is_file():
            if raise_on_error:
                raise LeaseReadError(f"Lease path at {path} is not a regular file")
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                if raise_on_error:
                    raise CorruptLeaseError(f"Lease file at {path} does not contain a JSON dictionary")
                return None
            return ProfileLease.from_dict(data)
        except OSError as exc:
            logger.warning("Failed to read lease file at %s: %s", path, exc)
            if raise_on_error:
                raise LeaseReadError(f"Failed to read lease file at {path}: {exc}") from exc
            return None
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("Failed to parse lease file at %s: %s", path, exc)
            if raise_on_error:
                raise CorruptLeaseError(f"Failed to parse corrupt lease file at {path}: {exc}") from exc
            return None

    def acquire(
        self,
        profile_name: str,
        run_id: RunId | str,
        worker_id: WorkerId | str,
        pid: int | None = None,
    ) -> ProfileLease:
        """Exclusively acquires a lease for a profile.

        Raises:
            ProfileAlreadyLeasedError: If profile is already leased and not stale.
            LeaseReadError: If an existing lease file cannot be read or is corrupt.
            LockTimeoutError: If lock acquisition times out.
        """
        self._ensure_dirs()
        with FileLock(self._lock_path, timeout=self.lock_timeout_seconds):
            path = self._profile_path(profile_name)
            existing = self._read_lease_file(path, raise_on_error=True)

            if existing is not None:
                if is_lease_stale(existing, timeout_seconds=self.stale_timeout_seconds):
                    logger.info("Evicting stale lease for profile %s: %s", profile_name, existing.lease_id)
                    try:
                        path.unlink()
                    except OSError:
                        pass
                else:
                    raise ProfileAlreadyLeasedError(
                        f"Profile '{profile_name}' is already leased by run '{existing.run_id}' "
                        f"worker '{existing.worker_id}' (lease_id={existing.lease_id})"
                    )

            now_iso = datetime.now(timezone.utc).isoformat()
            new_lease_id = LeaseId(f"lease_{uuid.uuid4().hex[:12]}")
            actual_pid = pid if pid is not None else os.getpid()

            lease = ProfileLease(
                lease_id=new_lease_id,
                profile_name=profile_name,
                run_id=RunId(run_id),
                worker_id=WorkerId(worker_id),
                pid=actual_pid,
                acquired_at=now_iso,
                heartbeat_at=now_iso,
            )
            self._write_lease_atomic(path, lease)
            return lease

    def heartbeat(self, lease_id: LeaseId | str) -> bool:
        """Renews heartbeat timestamp for an active lease. Returns True if updated."""
        target_id = str(lease_id)
        self._ensure_dirs()
        with FileLock(self._lock_path, timeout=self.lock_timeout_seconds):
            for path in self.lease_root.glob("*.json"):
                lease = self._read_lease_file(path)
                if lease and str(lease.lease_id) == target_id:
                    lease.heartbeat_at = datetime.now(timezone.utc).isoformat()
                    self._write_lease_atomic(path, lease)
                    return True
        return False

    def release(
        self,
        lease_id: LeaseId | str | ProfileLease,
        run_id: RunId | str | None = None,
    ) -> bool:
        """Releases a lease when work finishes, verifying ownership.

        If run_id is supplied, it must match the active lease's run_id.
        Returns True if released, False if not found or ownership mismatch.
        """
        if isinstance(lease_id, ProfileLease):
            if run_id is None:
                run_id = lease_id.run_id
            target_id = str(lease_id.lease_id)
        else:
            target_id = str(lease_id)

        target_run_id = str(run_id) if run_id is not None else None

        self._ensure_dirs()
        with FileLock(self._lock_path, timeout=self.lock_timeout_seconds):
            for path in self.lease_root.glob("*.json"):
                lease = self._read_lease_file(path)
                if lease and str(lease.lease_id) == target_id:
                    if target_run_id is not None and str(lease.run_id) != target_run_id:
                        logger.warning(
                            "Lease release rejected for %s: expected run %s but got %s",
                            target_id, lease.run_id, target_run_id
                        )
                        return False
                    try:
                        path.unlink()
                        return True
                    except OSError:
                        return False
        return False

    def release_profile(
        self,
        profile_name: str,
        lease_id: LeaseId | str,
        run_id: RunId | str | None = None,
    ) -> bool:
        """Releases a lease by profile name, verifying lease_id and optional run_id."""
        target_id = str(lease_id)
        target_run_id = str(run_id) if run_id is not None else None
        path = self._profile_path(profile_name)

        self._ensure_dirs()
        with FileLock(self._lock_path, timeout=self.lock_timeout_seconds):
            lease = self._read_lease_file(path)
            if lease is None:
                return False
            if str(lease.lease_id) != target_id:
                logger.warning(
                    "Profile lease release rejected for %s: lease ID mismatch (%s != %s)",
                    profile_name, lease.lease_id, target_id
                )
                return False
            if target_run_id is not None and str(lease.run_id) != target_run_id:
                logger.warning(
                    "Profile lease release rejected for %s: run ID mismatch (%s != %s)",
                    profile_name, lease.run_id, target_run_id
                )
                return False
            try:
                path.unlink()
                return True
            except OSError:
                return False

    def get_lease(self, profile_name: str) -> ProfileLease | None:
        """Retrieves active lease for the given profile if one exists."""
        path = self._profile_path(profile_name)
        lease = self._read_lease_file(path)
        if lease is None:
            return None
        if is_lease_stale(lease, timeout_seconds=self.stale_timeout_seconds):
            return None
        return lease

    def is_leased(self, profile_name: str) -> bool:
        """Checks if a profile currently has an active, non-stale lease."""
        return self.get_lease(profile_name) is not None

    def list_leases(self) -> list[ProfileLease]:
        """Lists all active (non-stale) leases."""
        self._ensure_dirs()
        with FileLock(self._lock_path, timeout=self.lock_timeout_seconds):
            leases: list[ProfileLease] = []
            for path in sorted(self.lease_root.glob("*.json")):
                lease = self._read_lease_file(path)
                if lease is not None and not is_lease_stale(lease, timeout_seconds=self.stale_timeout_seconds):
                    leases.append(lease)
            leases.sort(key=lambda item: item.profile_name)
            return leases

    def revoke_stale(self, timeout_seconds: float | None = None) -> list[ProfileLease]:
        """Revokes all leases that meet conservative stale criteria. Returns revoked leases."""
        eff_timeout = self.stale_timeout_seconds if timeout_seconds is None else max(0.0, timeout_seconds)
        self._ensure_dirs()
        revoked: list[ProfileLease] = []

        with FileLock(self._lock_path, timeout=self.lock_timeout_seconds):
            for path in sorted(self.lease_root.glob("*.json")):
                lease = self._read_lease_file(path)
                if lease is not None and is_lease_stale(lease, timeout_seconds=eff_timeout):
                    try:
                        path.unlink()
                        revoked.append(lease)
                    except OSError:
                        pass
        return revoked
