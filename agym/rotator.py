from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .launcher import cleanup_profile_locks
from .profiles import ProfileError, ProfileStore, load_accounts_file

logger = logging.getLogger("agym.rotator")


class RotationError(ProfileError):
    pass


@dataclass
class RotationState:
    index: int = 0
    last_account: str | None = None
    history: list[str] = field(default_factory=list)
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "last_account": self.last_account,
            "history": self.history,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RotationState:
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            index=int(data.get("index", 0)),
            last_account=data.get("last_account"),
            history=list(data.get("history", [])),
            updated_at=data.get("updated_at"),
        )


def _atomic_save_state(state_path: Path, state: RotationState) -> None:
    """Saves rotation state atomically using tempfile and os.replace.

    Guarantees cross-platform safety on Windows NTFS, preventing WinError 32
    (file locking collisions) when updating the state.
    """
    state_path = Path(state_path).resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=".rotation.",
        suffix=".tmp",
        dir=str(state_path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, indent=2)
            handle.write("\n")
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, state_path)
    except Exception as exc:
        logger.error("Failed to atomically persist rotation state to %s: %s", state_path, exc, exc_info=True)
        raise RotationError(f"cannot save rotation state to {state_path}: {exc}") from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_load_state(state_path: Path) -> RotationState:
    """Loads rotation state using utf-8-sig to automatically handle Windows UTF-8 BOM."""
    state_path = Path(state_path).resolve()
    if not state_path.is_file():
        return RotationState()
    try:
        with state_path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return RotationState.from_dict(data)
    except Exception as exc:
        logger.warning("Could not read rotation state file %s (%s); starting fresh", state_path, exc)
        return RotationState()


class AccountRotator:
    """Manages cross-platform account rotation across profiles or account files.

    Eliminates Windows rotation failures caused by:
    - Process spawn memory reset (uses atomic disk state / IPC queues)
    - Windows file locking (atomic tempfile + os.replace)
    - CRLF line endings (normalized via load_accounts_file)
    - Chromium SingletonLock and zombie sessions (stale lock cleanup)
    """

    def __init__(
        self,
        store: ProfileStore | None = None,
        accounts: Sequence[str] | None = None,
        accounts_file: Path | str | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.store = store or ProfileStore()
        self._explicit_accounts = list(accounts) if accounts is not None else None
        self._accounts_file = Path(accounts_file).resolve() if accounts_file else None
        self.state_path = (
            Path(state_path).resolve()
            if state_path
            else (self.store.data_root / "rotation_state.json").resolve()
        )

    def get_accounts(self) -> list[str]:
        """Returns the list of accounts to rotate through."""
        if self._explicit_accounts is not None:
            return list(self._explicit_accounts)
        if self._accounts_file is not None:
            return load_accounts_file(self._accounts_file)
        profiles = self.store.list()
        return [p.name for p in profiles]

    def load_state(self) -> RotationState:
        return _atomic_load_state(self.state_path)

    def save_state(self, state: RotationState) -> None:
        _atomic_save_state(self.state_path, state)

    def reset(self) -> None:
        """Resets the rotation state back to the beginning."""
        state = RotationState(index=0, last_account=None, history=[], updated_at=datetime.now(timezone.utc).isoformat())
        self.save_state(state)

    def get_profile_home(self, account_id: str) -> Path:
        """Resolves the isolated home path for the given account."""
        try:
            profile = self.store.get(account_id)
            return profile.home.resolve()
        except ProfileError:
            # For standalone / file-based accounts not in ProfileStore
            return (self.store.profiles_root / account_id / "home").resolve()

    def peek_next(self) -> tuple[int, str, Path]:
        """Peeks at the next account in rotation without updating state."""
        accounts = self.get_accounts()
        if not accounts:
            raise RotationError("no accounts configured or available for rotation")

        state = self.load_state()
        if state.last_account is None:
            next_index = 0
        else:
            next_index = (state.index + 1) % len(accounts)

        account_id = accounts[next_index]
        profile_home = self.get_profile_home(account_id)
        return next_index, account_id, profile_home

    def rotate(self, *, cleanup_locks: bool = True) -> tuple[int, str, Path]:
        """Rotates to the next account atomically, updates state, cleans up locks,
        and logs the rotation details in the required format.
        """
        accounts = self.get_accounts()
        if not accounts:
            raise RotationError("no accounts configured or available for rotation")

        state = self.load_state()
        if state.last_account is None:
            next_index = 0
        else:
            next_index = (state.index + 1) % len(accounts)

        account_id = accounts[next_index]
        profile_home = self.get_profile_home(account_id)
        profile_home.mkdir(parents=True, exist_ok=True)

        if cleanup_locks:
            cleanup_profile_locks(profile_home)

        # Update and save state atomically
        state.index = next_index
        state.last_account = account_id
        state.history.append(account_id)
        # Keep last 100 history items
        if len(state.history) > 100:
            state.history = state.history[-100:]
        state.updated_at = datetime.now(timezone.utc).isoformat()
        self.save_state(state)

        total = len(accounts)
        # Required explicit debug logs matching Phase 3 Step 3
        logger.info("Active Account: %s", account_id)
        logger.info("Resolved Profile Path: %s", profile_home)
        logger.info("Rotation Index: %s/%s", next_index + 1, total)

        return next_index, account_id, profile_home

    def get_status(self) -> dict[str, Any]:
        """Returns the current status of account rotation."""
        accounts = self.get_accounts()
        state = self.load_state()
        next_idx = 0 if state.last_account is None else (state.index + 1) % max(1, len(accounts))
        next_acc = accounts[next_idx] if accounts else None
        return {
            "total_accounts": len(accounts),
            "accounts": accounts,
            "current_index": state.index if state.last_account is not None else None,
            "active_account": state.last_account,
            "next_index": next_idx if accounts else None,
            "next_account": next_acc,
            "updated_at": state.updated_at,
            "state_file": str(self.state_path),
        }


def _worker_entry(
    queue: Any,
    result_queue: Any,
    worker_func: Callable[[str], Any],
) -> None:
    """Safe worker entry point for Windows multiprocessing spawn mode.

    Consumes tasks directly from IPC queue rather than module globals.
    """
    while True:
        try:
            account = queue.get_nowait()
        except Exception:
            break
        if account is None:
            break
        try:
            res = worker_func(account)
            result_queue.put((account, "success", res))
        except Exception as exc:
            result_queue.put((account, "error", str(exc)))


def dispatch_concurrent_workers(
    accounts: Sequence[str],
    worker_func: Callable[[str], Any],
    num_workers: int = 1,
    timeout: float = 30.0,
    mp_context: str | None = None,
) -> list[tuple[str, str, Any]]:
    """Dispatches accounts across worker processes using an IPC Queue.

    Addresses Root Cause A: On Windows, multiprocessing always uses 'spawn',
    causing memory-based iterators to re-initialize to 0 in child processes.
    By feeding explicit accounts through multiprocessing.Queue, each worker
    receives its assigned account with complete isolation.
    """
    if not accounts:
        return []

    ctx = mp.get_context(mp_context) if mp_context else mp.get_context()
    task_queue = ctx.Queue()
    result_queue = ctx.Queue()

    for acc in accounts:
        task_queue.put(acc)

    worker_count = min(len(accounts), max(1, num_workers))
    for _ in range(worker_count):
        task_queue.put(None)  # Sentinel to stop worker

    processes = []
    for _ in range(worker_count):
        p = ctx.Process(
            target=_worker_entry,
            args=(task_queue, result_queue, worker_func),
        )
        p.start()
        processes.append(p)

    results: list[tuple[str, str, Any]] = []
    for _ in range(len(accounts)):
        try:
            res = result_queue.get(timeout=timeout)
            results.append(res)
        except Exception:
            break

    for p in processes:
        p.join(timeout=2.0)
        if p.is_alive():
            p.terminate()

    return results
