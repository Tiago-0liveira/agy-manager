from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

from agym.launcher import build_agy_args, build_profile_env, cleanup_profile_locks, resolve_agy
from agym.profiles import ProfileStore
from agym.wincred import profile_credential_context

from . import events, leases
from .protocol import MAX_EVENT_BYTES
from .runs import now, TERMINAL, _terminate_child
from .store import Store


def _drain(stream, name: str, output: queue.Queue) -> None:
    try:
        for line in iter(stream.readline, ""):
            output.put((name, line))
    finally:
        output.put((name, None))


def _output(store: Store, run_id: str, stream: str, line: str) -> None:
    # A Unicode code point can take four UTF-8 bytes. 32K characters plus
    # JSON escaping always leaves room beneath the 256 KiB frame limit.
    for pos in range(0, len(line), 32 * 1024):
        events.emit(store, run_id, "output", {"stream": stream, "text": line[pos:pos + 32 * 1024]})


def execute(run_id: str, store: Store | None = None) -> None:
    store = store or Store()
    proc = None
    try:
        with store.locked():
            run = store.get_run(run_id)
            if run["status"] != "starting":
                if run["status"] == "stopping":
                    store.update_run(run_id, status="stopped", finished_at=now())
                    events.emit(store, run_id, "run_finished", {"status": "stopped"})
                return
        profile = ProfileStore().get(run["selected_profile"])
        agy = resolve_agy()
        cleanup_profile_locks(profile.home)
        env = build_profile_env(profile.home, profile_name=profile.name)
        argv = [str(agy), *build_agy_args(profile, operation_args=["--output-format", "stream-json", "--print", run["task"]], env=env)]
        kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                  else {"start_new_session": True})
        with profile_credential_context(profile.home):
            with store.locked():
                if store.get_run(run_id)["status"] == "stopping":
                    store.update_run(run_id, status="stopped", finished_at=now())
                    events.emit(store, run_id, "run_finished", {"status": "stopped"})
                    return
                proc = subprocess.Popen(argv, cwd=run["workspace_cwd"], env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="replace", bufsize=1, **kwargs)
                store.update_run(run_id, status="running", started_at=now(), child_pid=proc.pid)
                events.emit(store, run_id, "run_started", {"status": "running"})
            output: queue.Queue = queue.Queue()
            for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                threading.Thread(target=_drain, args=(stream, name, output), daemon=True).start()
            finished = 0
            while finished < 2 or proc.poll() is None:
                if store.get_run(run_id)["status"] == "stopping" and proc.poll() is None:
                    _terminate_child(proc.pid)
                try:
                    name, line = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    finished += 1
                    continue
                if name == "stdout":
                    try:
                        item = json.loads(line)
                        result = item.get("result") if isinstance(item.get("result"), dict) else {}
                        session = (item.get("session_id") or item.get("sessionId")
                                   or item.get("conversation_id") or item.get("conversationId")
                                   or result.get("conversation_id"))
                        if session and store.get_run(run_id).get("session_id") != session:
                            store.update_run(run_id, session_id=str(session))
                            events.emit(store, run_id, "session_created", {"session_id": str(session)})
                    except (ValueError, AttributeError):
                        pass
                _output(store, run_id, name, line)
            code = proc.wait()
        current = store.get_run(run_id)
        final = "stopped" if current["status"] == "stopping" else "succeeded" if code == 0 else "failed"
        store.update_run(run_id, status=final, exit_code=code, finished_at=now(), child_pid=None)
        events.emit(store, run_id, "run_finished", {"status": final, "exit_code": code})
    except BaseException:
        if proc is not None and proc.poll() is None:
            _terminate_child(proc.pid)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        current = store.get_run(run_id)
        if current["status"] not in TERMINAL:
            final = "stopped" if current["status"] == "stopping" else "failed"
            store.update_run(run_id, status=final, finished_at=now(), child_pid=None,
                             error=None if final == "stopped" else {"code": "INTERNAL_ERROR", "message": "Worker failed", "retryable": False})
            events.emit(store, run_id, "run_finished", {"status": final})
    finally:
        current = store.get_run(run_id)
        if current.get("lease_id"):
            leases.release(store, current["lease_id"])


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    execute(sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
