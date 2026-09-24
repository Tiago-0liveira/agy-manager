from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from agym import __version__

from . import events, runs
from .errors import IntegrationError, INVALID_REQUEST, INTERNAL_ERROR
from .profiles import list_profiles
from .protocol import MAX_EVENT_BYTES, MAX_PAGE_EVENTS, failure, success, validate_protocol, write_json, write_ndjson
from .store import Store
from .usage import list_usage

CAPABILITIES = ["profiles.read", "usage.read", "runs.headless", "runs.durable",
                "runs.stop", "runs.events", "runs.events.follow", "profiles.auto", "leases.read"]

INTEGRATION_TREE = """integration
├── info
├── profiles
├── usage
├── run
│   ├── start
│   ├── get
│   ├── list
│   ├── stop
│   └── events
└── lease
    └── get"""

INTEGRATION_RUN_TREE = """run
├── start
├── get
├── list
├── stop
└── events"""

INTEGRATION_LEASE_TREE = """lease
└── get"""

INTEGRATION_ARG_REFS = {
    "info": """integration info
  [--protocol N]
  [--json]""",
    "profiles": """integration profiles
  [--protocol N]
  [--json]""",
    "usage": """integration usage
  [--profile PROFILE]
  [--refresh]
  [--protocol N]
  [--json]""",
    "run start": """integration run start
  --request-json -
  [--protocol N]
  [--json]""",
    "run get": """integration run get
  --id RUN_ID
  [--protocol N]
  [--json]""",
    "run list": """integration run list
  [--client CLIENT]
  [--client-id ID]
  [--request-id ID]
  [--workspace-key KEY]
  [--protocol N]
  [--json]""",
    "run stop": """integration run stop
  --id RUN_ID
  [--protocol N]
  [--json]""",
    "run events": """integration run events
  --id RUN_ID
  [--after N]
  [--limit N]
  [--follow]
  [--protocol N]
  [--json | --ndjson]""",
    "lease get": """integration lease get
  --id LEASE_ID
  [--protocol N]
  [--json]""",
}


def format_integration_help(subcommand_path: list[str] | None = None) -> str | None:
    path = [p for p in (subcommand_path or []) if p not in {"-h", "--help", "help"}]
    if not path:
        return INTEGRATION_TREE + "\n\n" + "\n\n".join(INTEGRATION_ARG_REFS.values())
    if path == ["run"]:
        run_refs = [
            INTEGRATION_ARG_REFS["run start"],
            INTEGRATION_ARG_REFS["run get"],
            INTEGRATION_ARG_REFS["run list"],
            INTEGRATION_ARG_REFS["run stop"],
            INTEGRATION_ARG_REFS["run events"],
        ]
        return INTEGRATION_RUN_TREE + "\n\n" + "\n\n".join(run_refs)
    if path == ["lease"]:
        return INTEGRATION_LEASE_TREE + "\n\n" + INTEGRATION_ARG_REFS["lease get"]

    key = " ".join(path)
    if key in INTEGRATION_ARG_REFS:
        return INTEGRATION_ARG_REFS[key]
    return None


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise IntegrationError(INVALID_REQUEST, message)


def _parser() -> Parser:
    parser = Parser(prog="agym integration", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    def common(p, *, info=False, ndjson=False):
        p.add_argument("--protocol", type=int, default=1 if info else None)
        fmt = p.add_mutually_exclusive_group()
        fmt.add_argument("--json", action="store_true")
        if ndjson:
            fmt.add_argument("--ndjson", action="store_true")
    p = commands.add_parser("info", add_help=False); common(p, info=True)
    p = commands.add_parser("profiles", add_help=False); common(p)
    p = commands.add_parser("usage", add_help=False); common(p)
    p.add_argument("--profile"); p.add_argument("--refresh", action="store_true")
    run = commands.add_parser("run", add_help=False)
    sub = run.add_subparsers(dest="action", required=True, parser_class=Parser)
    p = sub.add_parser("start", add_help=False); common(p)
    p.add_argument("--request-json", required=True)
    for action in ("get", "stop", "events", "list"):
        p = sub.add_parser(action, add_help=False); common(p, ndjson=action == "events")
        if action != "list": p.add_argument("--id", required=True)
        if action == "list":
            for key in ("client", "client-id", "request-id", "workspace-key"):
                p.add_argument("--" + key)
        if action == "events":
            p.add_argument("--after", type=int, default=0)
            p.add_argument("--limit", type=int, default=MAX_PAGE_EVENTS)
            p.add_argument("--follow", action="store_true")
    lease = commands.add_parser("lease", add_help=False)
    sub = lease.add_subparsers(dest="action", required=True, parser_class=Parser)
    p = sub.add_parser("get", add_help=False); common(p); p.add_argument("--id", required=True)
    return parser


def _exit_code(code: str) -> int:
    if code in {"INVALID_REQUEST", "UNSUPPORTED_PROTOCOL"}: return 2
    if code in {"PROFILE_UNAVAILABLE", "NO_CAPACITY"}: return 3
    if code in {"WORKSPACE_BUSY", "IDEMPOTENCY_CONFLICT"}: return 4
    if code == "RUN_NOT_FOUND": return 2
    return 5


def _follow(store: Store, run_id: str, after: int, limit: int) -> None:
    while True:
        page = events.page(store, run_id, after, limit)
        for event in page["events"]:
            write_ndjson(event)
            after = event["seq"]
        if page["has_more"]:
            continue
        if page["snapshot"]["status"] in runs.TERMINAL:
            return
        time.sleep(0.15)


def build_integration_parser() -> Parser:
    return _parser()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    is_help = any(arg in {"-h", "--help"} for arg in args) or (bool(args) and args[0] == "help")
    if is_help:
        sub_path = [a for a in args if a not in {"-h", "--help", "help"}]
        help_text = format_integration_help(sub_path)
        if help_text is not None:
            print(help_text)
            return 0
        else:
            cmd_name = " ".join(sub_path)
            print(f"agym: unknown integration command '{cmd_name}'. Run 'agym help integration' for available commands.", file=sys.stderr)
            return 2

    try:
        ns = _parser().parse_args(args)
        validate_protocol(ns.protocol)
        store = Store()
        if ns.command == "info":
            data: Any = {"agym_version": __version__, "instance_id": store.get_instance_id(),
                         "supported_protocol_majors": [1], "capabilities": CAPABILITIES,
                         "limits": {"max_event_bytes": MAX_EVENT_BYTES, "max_page_events": MAX_PAGE_EVENTS}}
        elif ns.command == "profiles":
            data = list_profiles(store)
        elif ns.command == "usage":
            data = list_usage(ns.profile, ns.refresh)
        elif ns.command == "lease":
            data = store.get_lease(ns.id)
        elif ns.action == "start":
            if ns.request_json != "-":
                raise IntegrationError(INVALID_REQUEST, "Use --request-json -")
            try:
                request = json.load(sys.stdin)
            except (ValueError, UnicodeError) as exc:
                raise IntegrationError(INVALID_REQUEST, "Invalid request JSON") from exc
            data = runs.public(runs.start(request, store))
        elif ns.action == "get":
            data = runs.public(runs.get(ns.id, store))
        elif ns.action == "list":
            data = [runs.public(run) for run in runs.list_runs(store, client=ns.client,
                    client_id=ns.client_id, request_id=ns.request_id, workspace_key=ns.workspace_key)]
        elif ns.action == "stop":
            data = runs.public(runs.stop(ns.id, store))
        else:
            if ns.after < 0 or not 1 <= ns.limit <= MAX_PAGE_EVENTS:
                raise IntegrationError(INVALID_REQUEST, "Invalid event cursor or limit")
            if ns.follow:
                if not ns.ndjson:
                    raise IntegrationError(INVALID_REQUEST, "Follow requires --ndjson")
                _follow(store, ns.id, ns.after, ns.limit)
                return 0
            if ns.ndjson:
                raise IntegrationError(INVALID_REQUEST, "NDJSON requires --follow")
            data = events.page(store, ns.id, ns.after, ns.limit)
        write_json(success(data))
        return 0
    except IntegrationError as exc:
        write_json(failure(exc.code, str(exc), retryable=exc.retryable,
                           retry_after_seconds=exc.retry_after_seconds, run_id=exc.run_id))
        return _exit_code(exc.code)
    except Exception:
        write_json(failure(INTERNAL_ERROR, "Internal integration error"))
        return 5
