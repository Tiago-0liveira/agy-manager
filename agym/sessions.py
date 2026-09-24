from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .profiles import _chmod_private_dir, _default_data_root, _profile_lock, _write_json_private


def _is_windows_pid_alive(pid: int) -> bool:
    """Checks Windows process liveness without using os.kill(), which is destructive there."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            close_handle(handle)
    except Exception:
        return False


def is_pid_alive(pid: int | None) -> bool:
    """Returns True if the process with the given PID is currently alive."""
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def get_process_start_id(pid: int | None) -> str | None:
    """Returns a stable process-start fingerprint when the platform exposes one."""
    if pid is None or pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            get_process_times = kernel32.GetProcessTimes
            get_process_times.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
            get_process_times.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            handle = open_process(0x1000, False, pid)
            if not handle:
                return None
            try:
                creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
                if not get_process_times(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return None
                return f"win:{(creation.dwHighDateTime << 32) | creation.dwLowDateTime}"
            finally:
                close_handle(handle)
        except Exception:
            return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close_paren = raw.rfind(")")
        if close_paren >= 0:
            fields = raw[close_paren + 2:].split()
            if len(fields) > 19:
                return f"proc:{fields[19]}"
    except OSError:
        pass
    if os.name == "posix":
        try:
            completed = subprocess.run(["ps","-o","lstart=","-p",str(pid)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", check=False, env={**os.environ,"LC_ALL":"C"})
            start = " ".join((completed.stdout or "").split())
            if completed.returncode == 0 and start:
                return f"ps:{start}"
        except OSError:
            pass
    return None


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    profile: str
    pid: int
    started_at: str
    process_start_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "profile": self.profile,
            "pid": self.pid,
            "started_at": self.started_at,
            "process_start_id": self.process_start_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionRecord | None:
        if not isinstance(data, dict):
            return None
        sid = data.get("session_id")
        prof = data.get("profile")
        pid = data.get("pid")
        started = data.get("started_at")
        if not sid or not prof or not isinstance(pid, int):
            return None
        return cls(
            session_id=str(sid),
            profile=str(prof),
            pid=pid,
            started_at=str(started or ""),
            process_start_id=(str(data["process_start_id"]) if data.get("process_start_id") is not None else None),
        )


def _session_process_is_current(record: SessionRecord) -> bool:
    if not is_pid_alive(record.pid):
        return False
    if record.process_start_id is None:
        return True
    current_start_id = get_process_start_id(record.pid)
    return True if current_start_id is None else current_start_id == record.process_start_id


def _get_sessions_dir(data_root: Path | None = None) -> Path:
    root = Path(data_root) if data_root else _default_data_root()
    return root / "sessions"


def _get_sessions_lock(data_root: Path | None = None) -> Path:
    root = Path(data_root) if data_root else _default_data_root()
    return root / "sessions.lock"


def register_session(
    profile_name: str,
    pid: int | None = None,
    *,
    session_id: str | None = None,
    data_root: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Registers an active interactive session and returns the session_id."""
    effective_pid = os.getpid() if pid is None else pid
    effective_sid = session_id or f"sess-{uuid.uuid4().hex[:12]}"
    now_dt = now or datetime.now(timezone.utc)
    started_at = now_dt.isoformat()

    record = SessionRecord(
        session_id=effective_sid,
        profile=profile_name,
        pid=effective_pid,
        started_at=started_at,
        process_start_id=get_process_start_id(effective_pid),
    )

    sessions_dir = _get_sessions_dir(data_root)
    lock_path = _get_sessions_lock(data_root)

    with _profile_lock(lock_path):
        sessions_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(sessions_dir)

        # Prune any stale/dead sessions while holding the lock
        _prune_dead_sessions_locked(sessions_dir)

        session_file = sessions_dir / f"{effective_sid}.json"
        _write_json_private(session_file, record.to_dict())

    return effective_sid


def unregister_session(
    session_id: str,
    *,
    data_root: Path | None = None,
) -> bool:
    """Unregisters an active session by session_id. Returns True if removed."""
    sessions_dir = _get_sessions_dir(data_root)
    lock_path = _get_sessions_lock(data_root)
    session_file = sessions_dir / f"{session_id}.json"

    with _profile_lock(lock_path):
        if session_file.exists():
            try:
                session_file.unlink()
                return True
            except OSError:
                return False
    return False


def _prune_dead_sessions_locked(sessions_dir: Path) -> None:
    if not sessions_dir.exists():
        return
    for item in sessions_dir.glob("*.json"):
        try:
            with item.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            rec = SessionRecord.from_dict(data)
            if rec is None or not _session_process_is_current(rec):
                item.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            item.unlink(missing_ok=True)


def list_active_sessions(
    *,
    data_root: Path | None = None,
    prune_dead: bool = True,
) -> list[SessionRecord]:
    """Lists all currently active sessions, pruning dead PIDs if requested."""
    sessions_dir = _get_sessions_dir(data_root)
    lock_path = _get_sessions_lock(data_root)

    if not sessions_dir.exists():
        return []

    active: list[SessionRecord] = []
    with _profile_lock(lock_path):
        if not sessions_dir.exists():
            return []

        for item in sorted(sessions_dir.glob("*.json")):
            try:
                with item.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                rec = SessionRecord.from_dict(data)
                if rec is None:
                    if prune_dead:
                        item.unlink(missing_ok=True)
                    continue

                if _session_process_is_current(rec):
                    active.append(rec)
                elif prune_dead:
                    item.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                if prune_dead:
                    item.unlink(missing_ok=True)

    return active


def get_session_counts(
    profiles: Sequence[str] | None = None,
    *,
    data_root: Path | None = None,
) -> dict[str, int]:
    """Returns a mapping of profile name to active session count.

    If a list of profiles is provided, every profile will be present in the returned dict
    (with 0 if no active sessions exist).
    """
    active = list_active_sessions(data_root=data_root, prune_dead=True)
    counts: dict[str, int] = {p: 0 for p in profiles} if profiles is not None else {}
    for sess in active:
        counts[sess.profile] = counts.get(sess.profile, 0) + 1
    return counts
