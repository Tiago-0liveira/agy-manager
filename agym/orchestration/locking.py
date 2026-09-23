"""Cross-process file lock primitive for AGYM orchestration.

Provides cross-platform exclusive locking for short critical sections:
- Checking leases
- Creating leases
- Releasing leases
- Cleaning up stale leases

Locks must NOT be held during LLM invocations or external network operations.
"""

from __future__ import annotations

import os
import sys
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LockError(RuntimeError):
    """Base exception for file locking errors."""


class LockTimeoutError(LockError, TimeoutError):
    """Raised when acquiring a lock exceeds the timeout."""


class _LockEntry:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.ref_count = 0


_THREAD_LOCKS: dict[Path, _LockEntry] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _get_thread_lock(canonical_path: Path) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        entry = _THREAD_LOCKS.get(canonical_path)
        if entry is None:
            entry = _LockEntry()
            _THREAD_LOCKS[canonical_path] = entry
        entry.ref_count += 1
        return entry.lock


def _release_thread_lock(canonical_path: Path) -> None:
    with _THREAD_LOCKS_GUARD:
        entry = _THREAD_LOCKS.get(canonical_path)
        if entry is not None:
            entry.ref_count -= 1
            if entry.ref_count <= 0:
                _THREAD_LOCKS.pop(canonical_path, None)


class FileLock:
    """Cross-platform, cross-process and thread-safe exclusive lock on a file path.

    Uses `fcntl.flock` on Unix systems and `msvcrt.locking` on Windows,
    combined with in-process threading reentrant locks for intra-process synchronization.
    """

    def __init__(
        self,
        lock_file: Path | str,
        timeout: float | None = 10.0,
        poll_interval: float = 0.02,
    ) -> None:
        self.path = Path(lock_file).resolve()
        self.timeout = timeout
        self.poll_interval = max(0.001, poll_interval)
        self._fd: int | None = None
        self._depth = 0
        self._thread_lock = _get_thread_lock(self.path)
        self._thread_acquired = False

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Acquires the exclusive lock.

        Args:
            blocking: If False, returns immediately if lock cannot be acquired.
            timeout: Timeout in seconds. If None, uses `self.timeout`. Negative means indefinite.

        Returns:
            True if acquired.

        Raises:
            LockTimeoutError: If timeout expires while waiting.
            LockError: If an OS error occurs while acquiring.
        """
        if self._depth > 0:
            self._depth += 1
            return True

        eff_timeout = self.timeout if timeout is None else timeout
        start_time = time.monotonic()

        # Step 1: Thread-level locking
        if blocking:
            if eff_timeout is not None and eff_timeout >= 0:
                acquired_thread = self._thread_lock.acquire(timeout=eff_timeout)
                if not acquired_thread:
                    raise LockTimeoutError(
                        f"Timed out after {eff_timeout}s waiting for in-process lock on {self.path}"
                    )
            else:
                self._thread_lock.acquire()
        else:
            acquired_thread = self._thread_lock.acquire(blocking=False)
            if not acquired_thread:
                return False

        self._thread_acquired = True

        # Step 2: Cross-process OS file lock
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                try:
                    self.path.parent.chmod(0o700)
                except OSError:
                    pass

            flags = os.O_RDWR | os.O_CREAT
            mode = 0o600
            fd = os.open(self.path, flags, mode)
            if os.name != "nt":
                try:
                    os.chmod(fd, mode)
                except OSError:
                    pass

            while True:
                locked = self._try_os_lock(fd)
                if locked:
                    self._fd = fd
                    self._depth = 1
                    return True

                if not blocking:
                    os.close(fd)
                    self._thread_lock.release()
                    self._thread_acquired = False
                    return False

                now = time.monotonic()
                if eff_timeout is not None and eff_timeout >= 0 and (now - start_time) >= eff_timeout:
                    os.close(fd)
                    raise LockTimeoutError(
                        f"Timed out after {eff_timeout}s waiting for process lock on {self.path}"
                    )

                time.sleep(self.poll_interval)
        except Exception:
            if self._thread_acquired:
                self._thread_lock.release()
                self._thread_acquired = False
            raise

    def _try_os_lock(self, fd: int) -> bool:
        if os.name == "nt":
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (BlockingIOError, OSError):
                return False

    def _unlock_os_lock(self, fd: int) -> None:
        if os.name == "nt":
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def release(self) -> None:
        """Releases the exclusive lock."""
        if self._depth == 0:
            return

        self._depth -= 1
        if self._depth > 0:
            return

        fd = self._fd
        self._fd = None

        if fd is not None:
            try:
                self._unlock_os_lock(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

        if self._thread_acquired:
            self._thread_acquired = False
            self._thread_lock.release()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass
        _release_thread_lock(self.path)


@contextmanager
def file_lock(
    lock_path: Path | str,
    timeout: float | None = 10.0,
    poll_interval: float = 0.02,
) -> Iterator[FileLock]:
    """Convenience context manager for acquiring a FileLock."""
    lock = FileLock(lock_path, timeout=timeout, poll_interval=poll_interval)
    with lock:
        yield lock
