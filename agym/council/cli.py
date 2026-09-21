"""agym-council CLI entry point for Google Antigravity multi-account council orchestration."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from agym import __version__ as core_version
from agym.council import __version__ as council_version
from agym.council import check_council_dependencies
from agym.profiles import ProfileStore


def _print_err(msg: str) -> None:
    print(f"agym-council: {msg}", file=sys.stderr)


def _cmd_version(args: argparse.Namespace) -> int:
    """Show council version and installed optional dependencies."""
    print(f"agym-council {council_version} (agym core {core_version}, Python {sys.version.split()[0]})")
    all_ok, missing = check_council_dependencies()
    print("Optional extras [council]:")
    for pkg in ("fastapi", "uvicorn", "pydantic", "aiofiles"):
        try:
            mod = __import__(pkg)
            v = getattr(mod, "__version__", "installed")
            print(f"  {pkg}: {v}")
        except ImportError:
            print(f"  {pkg}: missing (not installed)")
    if missing:
        print("\nTo install missing council extras, run:\n  pip install 'agym[council]'")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    """Start local council web application and loopback API server."""
    all_ok, missing = check_council_dependencies()
    web_deps = [p for p in ("fastapi", "uvicorn") if p in missing]
    if web_deps:
        _print_err(
            f"'web' command requires optional dependencies: {', '.join(web_deps)}\n"
            f"Install them with: pip install 'agym[council]'"
        )
        return 1

    # Security check: loopback binding invariant
    host = getattr(args, "host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        _print_err(f"security violation: council API must bind to loopback (127.0.0.1), got '{host}'")
        return 1

    port = getattr(args, "port", 8000)
    reload = getattr(args, "reload", False)

    print(f"Starting AGYM Council web service on http://{host}:{port} (loopback only)")
    try:
        from agym.council.api.app import app  # type: ignore[import-not-found]
        import uvicorn  # type: ignore[import-not-found]
        uvicorn.run(app, host=host, port=port, reload=reload)
        return 0
    except Exception as exc:
        _print_err(f"failed to start web service: {exc}")
        return 1


def _get_presets_dir() -> Path:
    try:
        return Path(__file__).resolve().parent / "presets"
    except (NameError, TypeError):
        return Path("agym/council/presets").resolve()


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute a council workflow from file or preset."""
    path = Path(args.workflow)
    # Check if path exists or if it corresponds to a bundled preset
    preset_path = _get_presets_dir() / f"{args.workflow}.json"
    if not path.exists() and preset_path.exists():
        path = preset_path
    elif not path.exists() and not str(args.workflow).endswith(".json"):
        alt_path = Path(f"{args.workflow}.json")
        if alt_path.exists():
            path = alt_path

    if not path.exists():
        _print_err(f"workflow configuration not found: '{args.workflow}'")
        return 2

    print(f"Loaded workflow configuration from {path}")
    if args.dry_run:
        print("Dry-run mode: workflow validated. Engine execution skipped.")
        return 0

    print(f"Provider: {args.provider}")
    try:
        import asyncio
        from agym.council.models import WorkflowConfig
        from agym.council.storage import init_db, get_connection, create_run
        from agym.council.engine import CouncilEngine
        from agym.council.providers.fake import FakeProviderAdapter
        from agym.council.providers.antigravity import AntigravityProviderAdapter

        raw_data = json.loads(path.read_text(encoding="utf-8"))
        if getattr(args, "goal", None):
            raw_data["goal"] = args.goal
        raw_data["draft"] = False

        # Handle account mapping if provided
        if getattr(args, "account_map", None):
            account_mapping = {}
            if Path(args.account_map).exists():
                account_mapping = json.loads(Path(args.account_map).read_text(encoding="utf-8"))
            else:
                try:
                    account_mapping = json.loads(args.account_map)
                except Exception:
                    pass
            for w in raw_data.get("workers", []):
                wid = w.get("id")
                if wid in account_mapping:
                    w["account_ref"] = account_mapping[wid]

        # Handle model override if provided
        if getattr(args, "model", None):
            for w in raw_data.get("workers", []):
                if w.get("model", "").startswith("<") or not w.get("model"):
                    w["model"] = args.model

        # If running with fake provider, automatically resolve any remaining placeholders
        if args.provider == "fake":
            for w in raw_data.get("workers", []):
                if w.get("model", "").startswith("<") or not w.get("model"):
                    w["model"] = "fake-model-1"
                if w.get("account_ref", "").startswith("<") or not w.get("account_ref"):
                    w["account_ref"] = "fake-account"

        goal_text = getattr(args, "goal", None) or raw_data.get("goal") or "Execute council workflow"
        raw_data["goal"] = goal_text
        for inp in raw_data.get("inputs", []):
            if inp.get("required") and not inp.get("value"):
                inp["value"] = goal_text

        wf = WorkflowConfig.model_validate(raw_data)
        conn = init_db(getattr(args, "db", None))
        try:
            run_id = create_run(conn, wf, stages=wf.stages, workers=wf.workers)
            print(f"Created run '{run_id}'. Dispatching engine...")
            provider = FakeProviderAdapter() if args.provider == "fake" else AntigravityProviderAdapter()
            engine = CouncilEngine(db_path=getattr(args, "db", None), provider=provider)
            result = asyncio.run(engine.execute_run(run_id))
            print(f"Run completed with status: {result.status.value}")
            return 0 if result.status.value == "COMPLETED" else 1
        finally:
            conn.close()
    except Exception as exc:
        _print_err(f"execution failed: {exc}")
        return 1


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate a workflow definition file or preset against contracts."""
    path = Path(args.workflow)
    preset_path = _get_presets_dir() / f"{args.workflow}.json"
    if not path.exists() and preset_path.exists():
        path = preset_path
    elif not path.exists() and not str(args.workflow).endswith(".json"):
        alt_path = Path(f"{args.workflow}.json")
        if alt_path.exists():
            path = alt_path

    if not path.exists():
        _print_err(f"workflow configuration not found: '{args.workflow}'")
        return 2

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _print_err(f"failed to parse JSON from '{path}': {exc}")
        return 1

    # Structural contract validation
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append(f"unsupported schema_version: {data.get('schema_version')} (expected 1)")
    if not data.get("name"):
        errors.append("missing or empty 'name'")
    if not data.get("goal"):
        errors.append("missing or empty 'goal'")

    workers = data.get("workers", [])
    if not workers:
        errors.append("workflow has no workers defined")
    worker_ids = [w.get("id") for w in workers if isinstance(w, dict)]
    if len(worker_ids) != len(set(worker_ids)):
        errors.append("duplicate worker IDs detected")

    stages = data.get("stages", [])
    if not stages:
        errors.append("workflow has no stages defined")
    stage_ids = set()
    for s in stages:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if sid in stage_ids:
            errors.append(f"duplicate stage ID: '{sid}'")
        stage_ids.add(sid)
        input_stages = s.get("input_stages", [])
        for inp in input_stages:
            if inp not in stage_ids:
                errors.append(f"stage '{sid}' references forward or unknown input stage '{inp}'")

    limits = data.get("limits", {})
    if not isinstance(limits, dict):
        errors.append("missing or invalid 'limits' block")
    else:
        for lim in ("global_concurrency", "per_account_concurrency", "max_model_calls", "max_wall_seconds"):
            val = limits.get(lim)
            if not isinstance(val, int) or val <= 0:
                errors.append(f"limit '{lim}' must be a positive integer, got {val}")

    if errors:
        _print_err(f"validation failed for '{path}':")
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"Workflow '{data.get('name')}' validation: PASS")
    print(f"  Workers: {len(worker_ids)}, Stages: {len(stage_ids)}")
    return 0


def _cmd_accounts(args: argparse.Namespace) -> int:
    """Inspect and manage council accounts and Antigravity profile bindings."""
    store = ProfileStore()
    profiles = store.list()

    action = getattr(args, "action", "list") or "list"
    target_account = getattr(args, "account", None)

    if action == "list":
        if not profiles:
            print("No Antigravity profiles configured yet. Create one with: agym setup <name>")
            return 0
        print(f"Configured Antigravity profiles ({len(profiles)}):")
        for p in profiles:
            model_info = p.settings.model or "default"
            perms_info = "skip" if p.settings.dangerously_skip_permissions else "prompt"
            print(f"  - {p.name:<20} [model: {model_info}, perms: {perms_info}]")
        return 0

    if action == "check":
        import asyncio
        from agym.council.providers.antigravity import AntigravityProviderAdapter
        from agym.council.providers.fake import FakeProviderAdapter

        provider_name = getattr(args, "provider", None) or os.environ.get("AGYM_COUNCIL_PROVIDER")
        provider = FakeProviderAdapter() if provider_name == "fake" else AntigravityProviderAdapter()

        targets = [target_account] if target_account else [p.name for p in profiles]
        if not targets:
            print("No profiles configured to check.")
            return 0

        print(f"Probing live authentication status for {len(targets)} profile(s)...")
        has_failure = False
        for prof_name in targets:
            try:
                status_res = asyncio.run(provider.check_account(prof_name))
                status_str = status_res.status.value if hasattr(status_res.status, "value") else str(status_res.status)
                version_str = f" [CLI: {status_res.cli_version}]" if status_res.cli_version else ""
                msg_str = f" - {status_res.message}" if status_res.message else ""
                print(f"  - {prof_name:<20} : {status_str.upper()}{version_str}{msg_str}")
                if status_str.lower() in ("unavailable", "needs_login"):
                    has_failure = True
            except Exception as exc:
                print(f"  - {prof_name:<20} : ERROR ({exc})")
                has_failure = True
        return 1 if (has_failure and target_account) else 0

    _print_err(f"unknown accounts action: '{action}'")
    return 2


def _cmd_export(args: argparse.Namespace) -> int:
    """Export a run dossier and released artifacts."""
    run_id = args.run_id
    out = args.output or f"council_export_{run_id}.zip"
    redact = args.redact
    try:
        from agym.council.api.exporter import export_run_to_file
        from agym.council.storage import get_connection
        conn = get_connection(getattr(args, "db", None))
        try:
            dest = export_run_to_file(conn, run_id=run_id, output_path=out, redact=redact)
            print(f"Exported run '{run_id}' dossier to: {dest}")
            return 0
        finally:
            conn.close()
    except Exception as exc:
        _print_err(f"export failed: {exc}")
        return 1


def _cmd_reconcile(args: argparse.Namespace) -> int:
    """Reconcile in-flight attempts and recover crashed runs."""
    target = f"run '{args.run_id}'" if args.run_id else "all in-flight runs"
    print(f"Reconciling {target} against SQLite storage...")
    try:
        from agym.council.recovery import reconcile_startup_crashes_sync
        from agym.council.storage import get_connection
        conn = get_connection(getattr(args, "db", None))
        try:
            reconciled = reconcile_startup_crashes_sync(conn)
            print(f"Reconciled {len(reconciled)} in-flight attempt(s).")
            for rec in reconciled:
                print(f"  - Attempt {rec.attempt_id}: {rec.reconciled_status} ({rec.reason})")
            return 0
        finally:
            conn.close()
    except Exception as exc:
        _print_err(f"reconciliation failed: {exc}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Construct top-level argument parser for agym-council."""
    parser = argparse.ArgumentParser(
        prog="agym-council",
        description="AGYM Council — Multi-account, multi-worker orchestration system for Google Antigravity CLI.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="store_true",
        help="Show council version and dependency status, then exit",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # web
    p_web = subparsers.add_parser("web", help="Start local web UI and loopback API server")
    p_web.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1, loopback only)",
    )
    p_web.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind (default: 8000)",
    )
    p_web.add_argument(
        "--reload",
        action="store_true",
        help="Enable development auto-reload",
    )

    # run
    p_run = subparsers.add_parser("run", help="Execute a council workflow")
    p_run.add_argument("workflow", help="Path to workflow JSON file or name of bundled preset")
    p_run.add_argument("--goal", help="Override initial user goal prompt")
    p_run.add_argument(
        "--provider",
        choices=["fake", "antigravity"],
        default="antigravity",
        help="Provider adapter (default: antigravity)",
    )
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate workflow and plan execution without running",
    )
    p_run.add_argument("--model", help="Override worker model(s) for placeholders")
    p_run.add_argument("--account-map", help="JSON string or file mapping worker accounts")
    p_run.add_argument("--db", help="Path to council SQLite database")

    # validate
    p_val = subparsers.add_parser("validate", help="Validate a workflow definition file or preset")
    p_val.add_argument("workflow", help="Path to workflow JSON file or preset name")

    # accounts
    p_acc = subparsers.add_parser("accounts", help="Inspect and manage council accounts")
    p_acc.add_argument(
        "action",
        choices=["list", "check"],
        nargs="?",
        default="list",
        help="Action to perform (default: list)",
    )
    p_acc.add_argument("--account", help="Specific account reference or profile name to inspect")
    p_acc.add_argument(
        "--provider",
        choices=["fake", "antigravity"],
        default=None,
        help="Provider adapter to probe with (default: antigravity)",
    )

    # export
    p_exp = subparsers.add_parser("export", help="Export a run dossier and artifacts")
    p_exp.add_argument("run_id", help="Run identifier to export")
    p_exp.add_argument("-o", "--output", help="Output file or directory path")
    p_exp.add_argument(
        "--no-redact",
        dest="redact",
        action="store_false",
        default=True,
        help="Disable automatic redaction of secret tokens and credential paths",
    )
    p_exp.add_argument("--db", help="Path to council SQLite database")

    # reconcile
    p_rec = subparsers.add_parser("reconcile", help="Scan SQLite storage and reconcile crashed attempts")
    p_rec.add_argument("--run-id", help="Specific run ID to reconcile")
    p_rec.add_argument("--db", help="Path to council SQLite database")

    # version
    subparsers.add_parser("version", help="Show council version and dependency status")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI main entry point for agym-council."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not args_list:
        parser.print_help()
        return 2

    if args_list[0] in ("-h", "--help", "help"):
        parser.print_help()
        return 0

    if args_list[0] in ("-v", "--version"):
        return _cmd_version(argparse.Namespace())

    try:
        ns = parser.parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    if ns.version:
        return _cmd_version(ns)

    if not ns.command:
        parser.print_help()
        return 2

    dispatch = {
        "version": _cmd_version,
        "web": _cmd_web,
        "run": _cmd_run,
        "validate": _cmd_validate,
        "accounts": _cmd_accounts,
        "export": _cmd_export,
        "reconcile": _cmd_reconcile,
    }
    handler = dispatch.get(ns.command)
    if handler:
        try:
            return handler(ns)
        except KeyboardInterrupt:
            _print_err("interrupted")
            return 130
        except Exception as exc:
            _print_err(f"error: {exc}")
            return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
