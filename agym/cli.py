from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .cache import CacheManager, USAGE_CACHE_TTL_SECONDS, format_duration, parse_duration_seconds
from .diagnostics import doctor_lines
from .git.auto_pr import AutoPrError, build_auto_pr_parser, handle_auto_pr, parse_auto_pr_args
from .launcher import (
    ALL_PERMISSIONS_ALIASES,
    AgyNotFound,
    persistent_profile_data_exists,
    resolve_agy,
    run_agy,
    run_auto_prompt,
)
from .picker import prepare_accounts_for_picker, run_picker
from .profiles import (
    InvalidProfileName,
    ProfileError,
    ProfileExists,
    ProfileNotFound,
    ProfileSettings,
    ProfileStore,
    validate_profile_name,
)
from .rotator import AccountRotator, RotationError
from .subscription import (
    SubscriptionError,
    calculate_subscription_health,
    format_iso_date,
    format_user_date,
    prompt_subscription_date,
)
from .statusline import get_statusline_status, render_statusline, sync_all_profiles
from .tokens import run_tokens
from .updater import build_update_parser, maybe_prompt_startup_update, run_update_cli
from .usage import fetch_and_cache_usage, run_usage
from .wincred import get_profile_email


def build_setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agym setup",
        usage="setup <profile> [-s DATE]",
        description="Create a new isolated Antigravity profile and complete Google sign-in.",
        add_help=True,
    )
    parser.add_argument("profile", help="Name for the new profile")
    parser.add_argument(
        "--subscription-date",
        "-s",
        metavar="DATE",
        help="Subscription renewal/expiration date (DD/MM/YYYY or YYYY-MM-DD)",
    )
    return parser


def build_config_parser() -> argparse.ArgumentParser:
    usage = """config [profile]
  [--model MODEL]
  [-y | --dsp | --skip-perms | --no-dsp | --no-skip-perms]
  [--cache-duration DURATION]"""
    parser = argparse.ArgumentParser(
        prog="agym config",
        usage=usage,
        description="Configure profile model, permissions, or global cache duration.",
        add_help=True,
    )
    parser.add_argument("profile", nargs="?", default=None, help="Name of the profile to configure (optional when setting global options)")
    parser.add_argument("--model", dest="model", default=None, metavar="MODEL", help="Set default model (or 'default' to clear)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-y",
        "--yes",
        "--dsp",
        "--skip-perms",
        "--dangerously-skip-permissions",
        "--dangerously-skip-permission",
        dest="dangerously_skip_permissions",
        action="store_true",
        default=None,
        help="Enable auto-skipping tool permissions for this profile",
    )
    group.add_argument(
        "--no-dangerously-skip-permissions",
        "--no-dangerously-skip-permission",
        "--no-dsp",
        "--no-skip-perms",
        dest="dangerously_skip_permissions",
        action="store_false",
        help="Disable auto-skipping tool permissions for this profile",
    )
    parser.add_argument(
        "--cache-duration",
        dest="cache_duration",
        default=None,
        metavar="DURATION",
        help="Set usage cache duration (e.g. 30s, 5m, 1h, or 'default')",
    )
    return parser


def build_edit_parser() -> argparse.ArgumentParser:
    usage = """edit <profile>
  [--name NEW_NAME]
  [-s DATE | --clear-subscription-date]"""
    parser = argparse.ArgumentParser(
        prog="agym edit",
        usage=usage,
        description="Update profile configuration or subscription renewal date.",
        add_help=True,
    )
    parser.add_argument("profile", help="Name of the profile to edit")
    parser.add_argument(
        "--name",
        "--rename",
        dest="new_name",
        metavar="NEW_NAME",
        help="Rename the profile to a new name",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--subscription-date",
        "-s",
        metavar="DATE",
        help="Subscription renewal/expiration date (DD/MM/YYYY or YYYY-MM-DD)",
    )
    group.add_argument(
        "--clear-subscription-date",
        action="store_true",
        help="Clear the stored subscription date",
    )
    return parser


def build_rename_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agym rename",
        usage="rename <profile> <new-name>",
        description="Rename a profile, its isolated data directory, and associated caches.",
        add_help=True,
    )
    parser.add_argument("old_profile", metavar="<profile>", help="Current name of the profile")
    parser.add_argument("new_name", metavar="<new-name>", help="New name for the profile")
    return parser


def build_list_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="agym list",
        usage="list",
        description="List all configured profiles, state, and subscription renewal status.",
        add_help=True,
    )


def build_select_parser() -> argparse.ArgumentParser:
    usage = """select
  [-f | --fresh]
  [-- <agy args...>]"""
    parser = argparse.ArgumentParser(
        prog="agym select",
        usage=usage,
        description="Interactively select an account based on cached 5h quota and launch Antigravity.",
        add_help=True,
    )
    parser.add_argument(
        "-f",
        "--fresh",
        action="store_true",
        help="Force fetch fresh usage data, ignoring 5-minute cache",
    )
    return parser


def build_rotate_parser() -> argparse.ArgumentParser:
    usage = """rotate
  [--file FILE]
  [--status]
  [--reset]
  [--simulate [N]]
  [-- <agy args...>]"""
    parser = argparse.ArgumentParser(
        prog="agym rotate",
        usage=usage,
        description="Rotate through configured profiles or an account file sequentially and launch Antigravity.",
        add_help=True,
    )
    parser.add_argument(
        "--file",
        "-f",
        metavar="FILE",
        help="Custom account file (txt or json) to rotate through instead of configured profiles",
    )
    parser.add_argument("--status", action="store_true", help="Show current rotation status and history")
    parser.add_argument("--reset", action="store_true", help="Reset rotation state back to the first account")
    parser.add_argument(
        "--simulate",
        nargs="?",
        const=3,
        type=int,
        metavar="N",
        help="Simulate N rotation steps without launching agy (default: 3)",
    )
    return parser


def build_usage_parser() -> argparse.ArgumentParser:
    usage = """usage [profiles...]
  [--json]
  [-c | --claude]
  [-f | --refresh]
  [-v | --view VIEW]
  [-g | --grid]
  [-m | --matrix]
  [-t | --telemetry]
  [--sort CRITERION]
  [--no-summary]
  [--timeout SECONDS]"""
    parser = argparse.ArgumentParser(
        prog="agym usage",
        usage=usage,
        description="Show quota limits, remaining capacity, reset times, and subscription health.",
        add_help=True,
    )
    parser.add_argument("profiles", nargs="*", help="Optional specific profiles to query")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="Output in JSON format")
    parser.add_argument(
        "--claude",
        "-c",
        action="store_true",
        dest="show_claude",
        help="Include Claude & GPT quotas in the usage view",
    )
    parser.add_argument(
        "--refresh",
        "-f",
        "--no-cache",
        action="store_true",
        dest="refresh",
        help="Bypass cache and force live query",
    )
    parser.add_argument(
        "--view",
        "-v",
        choices=["table", "grid", "matrix", "telemetry"],
        default="table",
        metavar="VIEW",
        help="Visual graph layout style: table (default), grid, matrix, or telemetry",
    )
    parser.add_argument(
        "--grid",
        "-g",
        action="store_const",
        dest="view",
        const="grid",
        help="Shortcut for --view grid (multi-column card dashboard)",
    )
    parser.add_argument(
        "--matrix",
        "-m",
        action="store_const",
        dest="view",
        const="matrix",
        help="Shortcut for --view matrix (ultra-dense heatmap for dozens of accounts)",
    )
    parser.add_argument(
        "--telemetry",
        "-t",
        action="store_const",
        dest="view",
        const="telemetry",
        help="Shortcut for --view telemetry (executive tiered view & recommendations)",
    )
    parser.add_argument(
        "--sort",
        choices=["usage", "quota", "reset", "name", "sub", "default"],
        default="usage",
        metavar="CRITERION",
        help="Sort order for profiles (default: usage; options: usage, quota, reset, name, sub, default)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        dest="include_summary",
        default=None,
        help="Show top fleet capacity summary banner",
    )
    parser.add_argument(
        "--no-summary",
        action="store_false",
        dest="include_summary",
        help="Hide top fleet capacity summary banner",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Per-profile timeout in seconds (default: 30)",
    )
    return parser


def build_tokens_parser() -> argparse.ArgumentParser:
    usage = """tokens [profiles...]
  [--json]
  [-b | --breakdown]
  [-f | --refresh]
  [-v | --view VIEW]
  [-m | --matrix]
  [-t | --telemetry]
  [--sort CRITERION]"""
    parser = argparse.ArgumentParser(
        prog="agym tokens",
        usage=usage,
        description="Show token usage breakdowns, comparison charts, and fleet statistics.",
        add_help=True,
    )
    parser.add_argument("profiles", nargs="*", help="Optional specific profiles to query")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="Output in JSON format")
    parser.add_argument(
        "--breakdown",
        "-b",
        action="store_true",
        dest="breakdown",
        help="Show detailed token composition breakdown table per profile",
    )
    parser.add_argument(
        "--refresh",
        "-f",
        "--no-cache",
        action="store_true",
        dest="refresh",
        help="Bypass cache and re-scan conversation databases",
    )
    parser.add_argument(
        "--view",
        "-v",
        choices=["table", "matrix", "telemetry", "classic"],
        default="table",
        metavar="VIEW",
        help="Visual layout style: table (default), matrix, telemetry, classic",
    )
    parser.add_argument(
        "--matrix",
        "-m",
        action="store_const",
        dest="view",
        const="matrix",
        help="Shortcut for --view matrix (ultra-dense heatmap for dozens of accounts)",
    )
    parser.add_argument(
        "--telemetry",
        "-t",
        action="store_const",
        dest="view",
        const="telemetry",
        help="Shortcut for --view telemetry (executive tiered view & analytics)",
    )
    parser.add_argument(
        "--sort",
        choices=["default", "volume", "cache", "name"],
        default="default",
        metavar="CRITERION",
        help="Sort order for profiles (default, volume, cache, name)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Per-profile timeout in seconds (default: 30)",
    )
    return parser


def build_statusline_parser() -> argparse.ArgumentParser:
    usage = """statusline
  [--preview [PROFILE]]
  [--sync]
  [--status]
  [--enable]
  [--disable]"""
    parser = argparse.ArgumentParser(
        prog="agym statusline",
        usage=usage,
        description="Manage and preview the Antigravity statusline across all registered profiles.",
        add_help=True,
    )
    parser.add_argument(
        "--preview",
        "-p",
        nargs="?",
        const="",
        metavar="PROFILE",
        default=None,
        help="Preview rendered statusline for the active or specified profile",
    )
    parser.add_argument(
        "--sync",
        "-s",
        action="store_true",
        help="Ensure statusline runner is installed and configured across all registered profiles",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check statusline configuration status across all profiles",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Enable statusline across all profiles",
    )
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Disable statusline across all profiles",
    )
    return parser


def build_remove_parser() -> argparse.ArgumentParser:
    usage = """remove <profile>
  [-y | --yes]"""
    parser = argparse.ArgumentParser(
        prog="agym remove",
        usage=usage,
        description="Delete a profile and its isolated data directory.",
        add_help=True,
    )
    parser.add_argument("profile", metavar="<profile>", help="Name of the profile to remove")
    parser.add_argument("--yes", "-y", action="store_true", help="Delete without interactive confirmation prompt")
    return parser


def build_doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agym doctor",
        usage="doctor [profile]",
        description="Inspect Antigravity executable, paths, permissions, and profile health.",
        add_help=True,
    )
    parser.add_argument("profile", nargs="?", metavar="[profile]", help="Optional specific profile to diagnose")
    return parser


def format_main_help() -> str:
    lines = [
        "Accounts",
        "  setup       Create a profile",
        "  list        List profiles",
        "  edit        Edit profile settings",
        "  config      Configure profile defaults",
        "  rename      Rename a profile",
        "  remove      Delete a profile",
        "",
        "Usage & Monitoring",
        "  usage       Show quota usage",
        "  tokens      Show token usage",
        "  statusline  Manage statusline",
        "",
        "Launching",
        "  select      Pick an account",
        "  rotate      Rotate accounts",
        "  <profile>   Launch Antigravity",
        "",
        "Tools",
        "  doctor",
        "  auto-pr",
        "  update",
        "  integration",
        "",
        "Run `agym help <command>` for details.",
    ]
    return "\n".join(lines)


def format_launch_help() -> str:
    lines = [
        "Launching Antigravity:",
        "  agym <profile>                      Launch Antigravity under the specified profile.",
        "                                      Replaces the current process on POSIX, preserving native",
        "                                      terminal, TTY, working directory, and signal handling.",
        "",
        "  agym rotate [agy args...]           Rotate to next account/profile and launch Antigravity.",
        "                                      Ensures non-repeating execution, atomic state updates,",
        "                                      and cross-platform Chromium lock cleanup on Windows.",
        "",
        "  agym <profile> [agy args...]        Pass arguments directly to Antigravity.",
        "                                      Example: agym personal -p \"explain this codebase\"",
        "",
        "  agym <profile> -- [agy args...]     Use '--' separator before arguments if needed to",
        "                                      prevent agym from parsing flags intended for agy.",
        "",
        "  agym <profile> --auto-prompt \"<prompt>\"",
        "                                      Two-stage prompt workflow: run non-interactively to generate",
        "                                      a plan, then continue interactively in the same profile session.",
        "",
        "  agym <profile> --auto-pr            Automated pull request creation: inspects commits and diff,",
        "                                      pushes current branch, and opens a PR via GitHub CLI.",
    ]
    return "\n".join(lines)


def _has_help_flag(argv: list[str]) -> bool:
    for arg in argv:
        if arg == "--":
            return False
        if arg in {"-h", "--help", "help"}:
            return True
    return False


def _print_err(message: str) -> None:
    print(f"agym: {message}", file=sys.stderr)


def _setup(argv: list[str], store: ProfileStore) -> int:
    parser = build_setup_parser()
    ns = parser.parse_args(argv)
    validate_profile_name(ns.profile)
    if store.exists(ns.profile):
        raise ProfileExists(f"profile already exists: {ns.profile}")

    subscription_date = None
    if ns.subscription_date is not None:
        try:
            subscription_date = format_iso_date(ns.subscription_date)
        except SubscriptionError as exc:
            _print_err(str(exc))
            return 2
    elif sys.stdin.isatty():
        subscription_date = prompt_subscription_date(existing=None)

    # Resolve the host binary before constructing or using the isolated HOME.
    agy = resolve_agy()
    profile = store.create(ns.profile, subscription_date=subscription_date)

    print(f"Launching Antigravity to set up profile '{profile.name}'.")
    print(f"Profile home: {profile.home}")
    print("Complete the normal Google sign-in flow, then exit Antigravity.")
    code = run_agy(agy, profile, replace_process=False, is_setup=True)
    if code != 0:
        _print_err(f"agy exited with status {code}; profile was kept for inspection/retry")
        return code

    if not persistent_profile_data_exists(profile):
        _print_err(
            "agy exited successfully, but no persistent data was detected under the isolated .gemini directory"
        )
        _print_err("run 'agym doctor %s' and the manual integration test before relying on this profile" % profile.name)
        return 1

    email = get_profile_email(profile.home)
    auth_info = f" ({email})" if email else ""
    print(f"Profile '{profile.name}' is ready (persistent Antigravity state detected{auth_info}).")
    try:
        synced = sync_all_profiles(store)
        print(f"Statusline configured for all registered accounts ({len(synced)} profiles).")
    except Exception as exc:
        _print_err(f"warning: failed to configure statusline: {exc}")
    return 0


def _config(argv: list[str], store: ProfileStore) -> int:
    parser = build_config_parser()
    ns = parser.parse_args(argv)

    if ns.cache_duration is not None:
        try:
            if ns.cache_duration.strip().lower() == "default":
                store.set_usage_cache_ttl(None)
                print("cache-duration: default (5m)")
            else:
                secs = parse_duration_seconds(ns.cache_duration)
                store.set_usage_cache_ttl(secs)
                human = format_duration(secs)
                print(f"cache-duration: {human} ({int(secs)}s)")
        except ValueError as exc:
            raise ProfileError(str(exc)) from exc

        if ns.profile is None:
            return 0

    if ns.profile is None:
        if ns.model is not None or ns.dangerously_skip_permissions is not None:
            raise ProfileError("a profile name is required to configure model or permissions")
        ttl = store.get_usage_cache_ttl()
        human = format_duration(ttl)
        print(f"cache-duration: {human} ({int(ttl)}s)")
        return 0

    profile = store.get(ns.profile)

    changed = False
    new_model = profile.settings.model
    new_danger = profile.settings.dangerously_skip_permissions

    if ns.model is not None:
        if ns.model == "default":
            new_model = None
        elif not ns.model.strip():
            raise ProfileError("model name cannot be empty")
        else:
            new_model = ns.model.strip()
        changed = True

    if ns.dangerously_skip_permissions is not None:
        new_danger = ns.dangerously_skip_permissions
        changed = True

    if changed:
        new_settings = ProfileSettings(
            model=new_model,
            dangerously_skip_permissions=new_danger,
        )
        profile = store.update_settings(profile.name, new_settings)

    model_display = profile.settings.model if profile.settings.model else "default"
    danger_display = "true" if profile.settings.dangerously_skip_permissions else "false"

    print(f"profile: {profile.name}")
    print(f"model: {model_display}")
    print(f"dangerously-skip-permissions: {danger_display}")

    if profile.settings.dangerously_skip_permissions:
        print("warning: --dangerously-skip-permissions is enabled for this profile")

    return 0


def _rename(argv: list[str], store: ProfileStore) -> int:
    parser = build_rename_parser()
    ns = parser.parse_args(argv)
    validate_profile_name(ns.old_profile)
    validate_profile_name(ns.new_name)
    profile = store.rename(ns.old_profile, ns.new_name)
    print(f"Renamed profile '{ns.old_profile}' to '{profile.name}'.")
    print(f"Profile home: {profile.home}")
    return 0


def _edit(argv: list[str], store: ProfileStore) -> int:
    parser = build_edit_parser()
    ns = parser.parse_args(argv)
    validate_profile_name(ns.profile)
    profile = store.get(ns.profile)

    renamed = False
    if ns.new_name is not None:
        validate_profile_name(ns.new_name)
        profile = store.rename(profile.name, ns.new_name)
        print(f"Renamed profile '{ns.profile}' to '{profile.name}'.")
        renamed = True

    if ns.clear_subscription_date:
        store.set_subscription_date(profile.name, None)
        print(f"Cleared subscription date for profile '{profile.name}'.")
        return 0

    if ns.subscription_date is not None:
        try:
            canonical = format_iso_date(ns.subscription_date)
        except SubscriptionError as exc:
            _print_err(str(exc))
            return 2
        store.set_subscription_date(profile.name, canonical)
        print(f"Updated subscription date for profile '{profile.name}' to {format_user_date(canonical)}.")
        return 0

    if renamed:
        return 0

    # Interactive prompt if no flag supplied
    if not sys.stdin.isatty():
        _print_err("interactive editing requires a terminal; use --subscription-date or --clear-subscription-date")
        return 2

    new_date = prompt_subscription_date(existing=profile.subscription_date)
    if new_date == profile.subscription_date:
        print("Subscription date unchanged.")
        return 0

    store.set_subscription_date(profile.name, new_date)
    if new_date is None:
        print(f"Subscription date for profile '{profile.name}' cleared.")
    else:
        print(f"Updated subscription date for profile '{profile.name}' to {format_user_date(new_date)}.")
    return 0


def _list(argv: list[str], store: ProfileStore) -> int:
    build_list_parser().parse_args(argv)
    profiles = store.list()
    if not profiles:
        print("No profiles.")
        return 0
    for profile in profiles:
        state = "ready" if persistent_profile_data_exists(profile) else "no-state"
        if profile.subscription_date:
            health = calculate_subscription_health(profile.subscription_date)
            date_disp = format_user_date(profile.subscription_date)
            if health.status == "expired":
                sub_info = f"; {health.human_remaining} ({date_disp})"
            elif health.status == "critical" and health.days_remaining == 0:
                sub_info = f"; expires today ({date_disp})"
            else:
                sub_info = f"; renews {date_disp} ({health.human_remaining})"
        else:
            sub_info = "; subscription: unknown"
        model_str = profile.settings.model or "default"
        perm_str = "skip" if profile.settings.dangerously_skip_permissions else "normal"
        print(f"{profile.name}\t{state}{sub_info}\tmodel={model_str}\tpermissions={perm_str}")
    return 0


def _remove(argv: list[str], store: ProfileStore) -> int:
    parser = build_remove_parser()
    ns = parser.parse_args(argv)
    profile = store.get(ns.profile)
    target = store.profile_dir(profile.name).resolve()
    print(f"Profile directory to remove: {target}")
    if not ns.yes:
        answer = input(f"Remove profile '{profile.name}'? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Not removed.")
            return 0
    store.remove(profile.name)
    print(f"Removed profile '{profile.name}'.")
    return 0


def _doctor(argv: list[str], store: ProfileStore) -> int:
    parser = build_doctor_parser()
    ns = parser.parse_args(argv)
    for line in doctor_lines(store, ns.profile):
        print(line)
    return 0


def _usage(argv: list[str], store: ProfileStore) -> int:
    parser = build_usage_parser()
    ns = parser.parse_args(argv)

    if ns.profiles:
        profiles = []
        for name in ns.profiles:
            validate_profile_name(name)
            profiles.append(store.get(name))
    else:
        profiles = store.list()

    if not profiles and not ns.profiles:
        if ns.json_mode:
            print(json.dumps({"accounts": []}, indent=2))
        else:
            print("No profiles configured. Run 'agym setup <profile>' first.")
        return 0

    agy = resolve_agy()
    kwargs: dict[str, Any] = {
        "json_mode": ns.json_mode,
        "timeout": ns.timeout,
    }
    if ns.refresh:
        kwargs["refresh"] = True
    if ns.show_claude:
        kwargs["show_claude"] = True
    if ns.view != "table":
        kwargs["view"] = ns.view
    if ns.sort != "usage":
        kwargs["sort_by"] = ns.sort
    if ns.include_summary is not None:
        kwargs["include_summary"] = ns.include_summary
    try:
        asyncio.run(run_usage(agy, profiles, **kwargs))
        return 0
    except KeyboardInterrupt:
        _print_err("interrupted")
        return 130


def _tokens(argv: list[str], store: ProfileStore) -> int:
    parser = build_tokens_parser()
    ns = parser.parse_args(argv)

    if ns.profiles:
        profiles = []
        for name in ns.profiles:
            validate_profile_name(name)
            profiles.append(store.get(name))
    else:
        profiles = store.list()

    if not profiles and not ns.profiles:
        if ns.json_mode:
            print(json.dumps({"summary": {}, "accounts": []}, indent=2))
        else:
            print("No profiles configured. Run 'agym setup <profile>' first.")
        return 0

    agy = resolve_agy()
    resolved_view = ns.view
    if ns.breakdown and not any(arg in argv for arg in ("--view", "-v", "-g", "--grid", "-m", "--matrix", "-t", "--telemetry")):
        resolved_view = "classic"

    kwargs: dict[str, Any] = {
        "json_mode": ns.json_mode,
        "breakdown": ns.breakdown,
        "timeout": ns.timeout,
    }
    if ns.refresh:
        kwargs["refresh"] = True
    if resolved_view != "classic":
        kwargs["view"] = resolved_view
    if ns.sort != "default":
        kwargs["sort_by"] = ns.sort
    try:
        asyncio.run(run_tokens(agy, profiles, **kwargs))
        return 0
    except KeyboardInterrupt:
        _print_err("interrupted")
        return 130


def _rotate(argv: list[str], store: ProfileStore) -> int:
    parser = build_rotate_parser()
    ns, passthrough = parser.parse_known_args(argv)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    rotator = AccountRotator(store=store, accounts_file=ns.file)

    if ns.reset:
        rotator.reset()
        print("Rotation state reset to initial position.")
        return 0

    if ns.status:
        st = rotator.get_status()
        print(f"Total accounts: {st['total_accounts']}")
        print(f"Accounts: {', '.join(st['accounts']) if st['accounts'] else 'none'}")
        curr_str = str(st['current_index']) if st['current_index'] is not None else "not started"
        print(f"Current index: {curr_str}")
        print(f"Active account: {st['active_account'] or 'none'}")
        print(f"Next account: {st['next_account'] or 'none'}")
        print(f"State file: {st['state_file']}")
        return 0

    if ns.simulate is not None:
        count = ns.simulate
        accounts = rotator.get_accounts()
        if not accounts:
            _print_err("no accounts available to simulate rotation")
            return 1
        print(f"Simulating {count} account rotations across {len(accounts)} accounts:")
        for _ in range(count):
            idx, account_id, path = rotator.rotate(cleanup_locks=False)
            print(f"  Active Account: {account_id} | Resolved Profile Path: {path} | Rotation Index: {idx + 1}/{len(accounts)}")
        return 0

    accounts = rotator.get_accounts()
    if not accounts:
        _print_err("no profiles or accounts configured. Run 'agym setup <profile>' or provide '--file <accounts>'")
        return 1

    idx, account_id, profile_home = rotator.rotate(cleanup_locks=True)
    total = len(accounts)
    print(f"Active Account: {account_id}")
    print(f"Resolved Profile Path: {profile_home}")
    print(f"Rotation Index: {idx + 1}/{total}")

    agy = resolve_agy()
    try:
        profile = store.get(account_id)
        return _launch(profile.name, passthrough, store)
    except ProfileNotFound:
        from .profiles import Profile
        profile = Profile(name=account_id, home=profile_home, created_at="standalone")
        return run_agy(agy, profile, passthrough, replace_process=True)


def _statusline(argv: list[str], store: ProfileStore) -> int:
    parser = build_statusline_parser()
    ns = parser.parse_args(argv)

    if ns.enable:
        synced = sync_all_profiles(store, enabled=True)
        print(f"Enabled statusline across {len(synced)} profile(s): {', '.join(synced) if synced else 'none'}")
        return 0

    if ns.disable:
        synced = sync_all_profiles(store, enabled=False)
        print(f"Disabled statusline across {len(synced)} profile(s): {', '.join(synced) if synced else 'none'}")
        return 0

    if ns.sync:
        synced = sync_all_profiles(store, enabled=True)
        print(f"Synchronized statusline across {len(synced)} profile(s): {', '.join(synced) if synced else 'none'}")
        return 0

    if ns.preview is not None:
        target_profile = ns.preview.strip()
        if not target_profile:
            profiles = store.list()
            target_profile = profiles[0].name if profiles else "default"
        output = render_statusline(profile_name=target_profile, data_root=store.data_root)
        print(f"Statusline preview for '{target_profile}':")
        print(output)
        return 0

    status = get_statusline_status(store)
    installed_str = "installed" if status["installed"] else "not installed"
    print(f"Runner script: {status['script_path']} ({installed_str})")
    profiles = status["profiles"]
    if not profiles:
        print("No profiles configured. Run 'agym setup <profile>' first.")
        return 0

    print(f"\nConfigured profiles ({len(profiles)}):")
    for name, p_stat in profiles.items():
        cfg = "configured" if p_stat["configured"] else "missing"
        state = "enabled" if p_stat["enabled"] else "disabled"
        print(f"  • {name:<16} [{cfg}, {state}]")

    first_name = next(iter(profiles))
    print(f"\nLive preview ('{first_name}'):")
    print(render_statusline(profile_name=first_name, data_root=store.data_root))
    return 0


def _select(
    argv: list[str],
    store: ProfileStore,
    *,
    cache_manager: CacheManager | None = None,
    agy_path: Path | None = None,
    runner: Any = None,
    key_reader: Any = None,
    stdout: Any = None,
    stdin: Any = None,
) -> int:
    parser = build_select_parser()
    ns, passthrough = parser.parse_known_args(argv)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    profiles = store.list()
    out = stdout if stdout is not None else sys.stdout
    if not profiles:
        out.write("No profiles configured. Run 'agym setup <profile>' first.\n")
        out.flush()
        return 0

    cm = cache_manager if cache_manager is not None else CacheManager()

    needs_live = ns.fresh or any(
        cm.get_usage(p.name, max_age=USAGE_CACHE_TTL_SECONDS) is None
        for p in profiles
    )

    if needs_live:
        out.write("Fetching fresh usage data...\n")
        out.flush()

    usages = fetch_and_cache_usage(
        profiles=profiles,
        force=ns.fresh,
        agy_path=agy_path,
        cache_manager=cm,
        runner=runner,
    )

    use_color = hasattr(out, "isatty") and out.isatty()
    items = prepare_accounts_for_picker(usages, use_color=use_color)

    selected = run_picker(
        items,
        stdout=out,
        stdin=stdin,
        use_color=use_color,
        key_reader=key_reader,
    )

    if selected is None:
        return 0

    out.write(f"Opening agy with account '{selected}'...\n")
    out.flush()

    return _launch(selected, passthrough, store)


def _parse_auto_prompt(args: list[str]) -> tuple[str | None, list[str]]:
    if not args:
        return None, args
    if args[0] == "--":
        return None, args
    for i, arg in enumerate(args):
        if arg == "--auto-prompt":
            if i + 1 >= len(args):
                raise ProfileError("--auto-prompt requires a prompt argument")
            prompt = args[i + 1]
            rem = args[:i] + args[i + 2:]
            return prompt, rem
        if arg.startswith("--auto-prompt="):
            prompt = arg.split("=", 1)[1]
            rem = args[:i] + args[i + 1:]
            return prompt, rem
    return None, args


def _is_auto_pr(args: list[str]) -> bool:
    if not args:
        return False
    pre_separator = []
    for arg in args:
        if arg == "--":
            break
        pre_separator.append(arg)
    return any(arg == "--auto-pr" or arg.startswith("--auto-pr=") for arg in pre_separator)


def _auto_pr(argv: list[str], store: ProfileStore) -> int:
    parser = build_auto_pr_parser()
    if any(arg in {"-h", "--help"} for arg in argv):
        parser.print_help()
        return 0

    profile_name = None
    remaining_argv = []
    if argv and not argv[0].startswith("-"):
        profile_name = argv[0]
        remaining_argv = argv[1:]
    else:
        profiles = store.list()
        if len(profiles) == 1:
            profile_name = profiles[0].name
            remaining_argv = argv
        elif not profiles:
            raise ProfileError("no profiles configured. Run 'agym setup <profile>' first.")
        else:
            raise ProfileError("profile name is required for auto-pr. Usage: agym <profile> --auto-pr [-b <base>]")

    validate_profile_name(profile_name)
    profile = store.get(profile_name)
    if not any(arg == "--auto-pr" or arg.startswith("--auto-pr=") for arg in remaining_argv):
        remaining_argv = ["--auto-pr"] + remaining_argv
    ns = parser.parse_args(remaining_argv)
    return handle_auto_pr(
        profile=profile,
        base_branch=ns.base,
        title=ns.title,
        body=ns.body,
        draft=ns.draft,
        no_push=ns.no_push,
        dry_run=ns.dry_run,
        store=store,
    )


def _launch(profile_name: str, argv: list[str], store: ProfileStore) -> int:
    validate_profile_name(profile_name)
    profile = store.get(profile_name)

    if _is_auto_pr(argv):
        ns = parse_auto_pr_args(argv)
        return handle_auto_pr(
            profile=profile,
            base_branch=ns.base,
            title=ns.title,
            body=ns.body,
            draft=ns.draft,
            no_push=ns.no_push,
            dry_run=ns.dry_run,
            store=store,
        )

    # Resolve before environment construction so PATH lookup uses the host environment.
    agy = resolve_agy()

    auto_prompt, rest = _parse_auto_prompt(argv)
    if auto_prompt is not None:
        perm_args = [arg for arg in rest if arg in ALL_PERMISSIONS_ALIASES]
        other_args = [arg for arg in rest if arg not in ALL_PERMISSIONS_ALIASES]
        if other_args:
            raise ProfileError(f"unexpected arguments with --auto-prompt: {' '.join(other_args)}")
        kwargs: dict[str, Any] = {"replace_process": True}
        if perm_args:
            kwargs["extra_args"] = perm_args
        return run_auto_prompt(agy, profile, auto_prompt, **kwargs)

    return run_agy(agy, profile, argv, replace_process=True)


@dataclass
class CommandSpec:
    name: str
    aliases: tuple[str, ...] = ()
    group: str = ""
    description: str = ""
    parser_builder: Callable[[], argparse.ArgumentParser] | None = None
    handler: Callable[..., int] | None = None


COMMAND_REGISTRY: list[CommandSpec] = [
    # Accounts
    CommandSpec(name="setup", group="Accounts", description="Create a profile", parser_builder=build_setup_parser, handler=_setup),
    CommandSpec(name="list", group="Accounts", description="List profiles", parser_builder=build_list_parser, handler=_list),
    CommandSpec(name="edit", group="Accounts", description="Edit profile settings", parser_builder=build_edit_parser, handler=_edit),
    CommandSpec(name="config", group="Accounts", description="Configure profile defaults", parser_builder=build_config_parser, handler=_config),
    CommandSpec(name="rename", aliases=("mv",), group="Accounts", description="Rename a profile", parser_builder=build_rename_parser, handler=_rename),
    CommandSpec(name="remove", group="Accounts", description="Delete a profile", parser_builder=build_remove_parser, handler=_remove),
    # Usage & Monitoring
    CommandSpec(name="usage", group="Usage & Monitoring", description="Show quota usage", parser_builder=build_usage_parser, handler=_usage),
    CommandSpec(name="tokens", aliases=("token", "token-usage"), group="Usage & Monitoring", description="Show token usage", parser_builder=build_tokens_parser, handler=_tokens),
    CommandSpec(name="statusline", group="Usage & Monitoring", description="Manage statusline", parser_builder=build_statusline_parser, handler=_statusline),
    # Launching
    CommandSpec(name="select", aliases=("pick",), group="Launching", description="Pick an account", parser_builder=build_select_parser, handler=_select),
    CommandSpec(name="rotate", group="Launching", description="Rotate accounts", parser_builder=build_rotate_parser, handler=_rotate),
    # Tools
    CommandSpec(name="doctor", group="Tools", description="", parser_builder=build_doctor_parser, handler=_doctor),
    CommandSpec(name="auto-pr", aliases=("--auto-pr",), group="Tools", description="", parser_builder=build_auto_pr_parser, handler=_auto_pr),
    CommandSpec(name="update", group="Tools", description="", parser_builder=build_update_parser, handler=lambda argv, store: run_update_cli(argv)),
    CommandSpec(
        name="integration",
        group="Tools",
        description="",
        parser_builder=lambda: __import__("agym.integration.cli", fromlist=["build_integration_parser"]).build_integration_parser(),
        handler=lambda argv, store: __import__("agym.integration.cli", fromlist=["main"]).main(argv),
    ),
]

COMMANDS_BY_NAME: dict[str, CommandSpec] = {}
ALIAS_MAP: dict[str, str] = {}
for cmd in COMMAND_REGISTRY:
    COMMANDS_BY_NAME[cmd.name] = cmd
    for alias in cmd.aliases:
        ALIAS_MAP[alias] = cmd.name


def resolve_command(name: str) -> CommandSpec | None:
    canonical = ALIAS_MAP.get(name, name)
    return COMMANDS_BY_NAME.get(canonical)


def _handle_help_command(rest: list[str]) -> int:
    clean_rest = [arg for arg in rest if arg not in {"-h", "--help"}]
    if not clean_rest:
        print(format_main_help())
        return 0

    topic = clean_rest[0]
    if topic in {"launch", "<profile>"}:
        print(format_launch_help())
        return 0

    if topic == "integration":
        from .integration import cli as integration_cli
        sub_path = clean_rest[1:]
        help_text = integration_cli.format_integration_help(sub_path)
        if help_text is not None:
            print(help_text)
            return 0
        else:
            cmd_name = " ".join(sub_path)
            _print_err(f"unknown integration command '{cmd_name}'. Run 'agym help integration' for available commands.")
            return 2

    cmd_spec = resolve_command(topic)
    if cmd_spec is not None:
        if cmd_spec.name == "integration":
            from .integration import cli as integration_cli
            sub_path = clean_rest[1:]
            help_text = integration_cli.format_integration_help(sub_path)
            if help_text is not None:
                print(help_text)
                return 0
            else:
                cmd_name = " ".join(sub_path)
                _print_err(f"unknown integration command '{cmd_name}'. Run 'agym help integration' for available commands.")
                return 2

        if cmd_spec.parser_builder is not None:
            cmd_spec.parser_builder().print_help()
            return 0

    _print_err(f"unknown command '{topic}'. Run 'agym help' for available commands.")
    return 2


def _handle_command_help(cmd_spec: CommandSpec, rest: list[str]) -> int:
    if cmd_spec.name == "integration":
        from .integration import cli as integration_cli
        sub_path = [a for a in rest if a not in {"-h", "--help", "help"}]
        help_text = integration_cli.format_integration_help(sub_path)
        if help_text is not None:
            print(help_text)
            return 0
        else:
            cmd_name = " ".join(sub_path)
            _print_err(f"unknown integration command '{cmd_name}'. Run 'agym help integration' for available commands.")
            return 2

    if cmd_spec.parser_builder is not None:
        cmd_spec.parser_builder().print_help()
        return 0

    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "integration":
        from .integration.cli import main as integration_main
        return integration_main(args[1:])
    if args in (["--version"], ["-v"]):
        from . import __version__
        print(__version__)
        return 0
    if args == ["--statusline-render"]:
        from .statusline import main as render_statusline_main
        return render_statusline_main()

    # 1. Main compact help
    if not args:
        print(format_main_help())
        return 2

    if args[0] in {"-h", "--help"} or args == ["help"]:
        print(format_main_help())
        return 0

    # 2. 'agym help ...'
    if args[0] == "help":
        return _handle_help_command(args[1:])

    # 3. Help for launch
    if args[0] == "launch" and _has_help_flag(args[1:]):
        print(format_launch_help())
        return 0

    # 4. '<command> --help'
    cmd_spec = resolve_command(args[0])
    if cmd_spec is not None and _has_help_flag(args[1:]):
        return _handle_command_help(cmd_spec, args[1:])

    store = ProfileStore()
    command, rest = args[0], args[1:]

    # Prompt startup update check if running interactively and not dismissed in 24h
    maybe_prompt_startup_update(args)

    try:
        if cmd_spec is not None and cmd_spec.handler is not None:
            return cmd_spec.handler(rest, store)
        return _launch(command, rest, store)
    except (InvalidProfileName, ProfileExists, ProfileNotFound, ProfileError, AgyNotFound, SubscriptionError, AutoPrError) as exc:
        _print_err(str(exc))
        return 2
    except KeyboardInterrupt:
        _print_err("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
