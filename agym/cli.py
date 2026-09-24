from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cache import CacheManager, USAGE_CACHE_TTL_SECONDS
from .diagnostics import doctor_lines
from .git.auto_pr import AutoPrError, handle_auto_pr, parse_auto_pr_args
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
from .updater import maybe_prompt_startup_update, run_update_cli
from .usage import fetch_and_cache_usage, run_usage
from .wincred import get_profile_email
from .orchestration.contracts import RunId, RunMode, RunState, RunStatus
from .orchestration.ui import format_duration
from .orchestration.wiring import (
    OrchestrationDependencies,
    build_orchestration_dependencies,
)

USAGE = """agym — Explicit isolated-profile manager for Google Antigravity CLI

Usage:
  agym <command> [arguments...]
  agym <profile> [--] [agy args...]
  agym <profile> --auto-prompt "<prompt>"
  agym <profile> --auto-pr [-b <branch>] [--title <title>] [--body <body>] [--draft] [--no-push]
  agym orchestrate "<task>" [--mode plan|implement] [--dry-run]
  agym orchestrate status <run-id>
  agym orchestrate resume <run-id>
  agym orchestrate inspect <run-id> [--worker <id>] [--attempt <id>] [--timeline] [--json]
  agym orchestrate logs <run-id> [--follow] [--worker <id>] [--attempt <id>] [--stream stdout|stderr|all] [--json]
  agym select [-f|--fresh] [-- [agy args...]]
  agym rotate [--file <file>] [--status] [--reset] [--simulate [N]] [-- [agy args...]]
  agym config <profile> [--model <model>|default] [-y|--dsp|--skip-perms|--[no-]dangerously-skip-permissions]
  agym update [--check] [-f|--force]

Commands:
  setup <profile>                     Create a new profile and complete Google sign-in
  orchestrate <task>                  Orchestrate multi-agent reasoning or implementation
  config <profile>                    Configure profile model and permission settings
  edit <profile>                      Edit profile settings (e.g. subscription renewal date, rename)
  rename <profile> <new-name>         Rename a profile and its isolated directory (alias: mv)
  list                                List all configured profiles and subscription status
  select                              Interactively select account by quota health and launch (alias: pick)
  rotate                              Rotate through accounts/profiles sequentially and launch
  usage [profiles...]                 Show live model quota usage and subscription health
  tokens [profiles...]                Show token consumption graphs and fleet summary (aliases: token, token-usage)
  statusline                          Manage and preview statusline across all registered profiles
  remove <profile>                    Delete a profile and its isolated data
  doctor [profile]                    Check environment, executable, permissions, and state
  auto-pr [profile]                   Create a pull request from current branch into base branch
  update                              Check for and install updates to agym

Launching Antigravity:
  agym <profile>                      Launch Antigravity under the specified profile.
                                      Replaces the current process on POSIX, preserving native
                                      terminal, TTY, working directory, and signal handling.

  agym rotate [agy args...]           Rotate to next account/profile and launch Antigravity.
                                      Ensures non-repeating execution, atomic state updates,
                                      and cross-platform Chromium lock cleanup on Windows.

  agym <profile> [agy args...]        Pass arguments directly to Antigravity.
                                      Example: agym personal -p "explain this codebase"

  agym <profile> -- [agy args...]     Use '--' separator before arguments if needed to
                                      prevent agym from parsing flags intended for agy.

  agym <profile> --auto-prompt "<prompt>"
                                      Two-stage prompt workflow: run non-interactively to generate
                                      a plan, then continue interactively in the same profile session.

  agym <profile> --auto-pr            Automated pull request creation: inspects commits and diff,
                                      pushes current branch, and opens a PR via GitHub CLI.

General Options:
  -h, --help                          Show this help message and exit

Command Options:
  agym setup <profile> [-s, --subscription-date DATE]
      -s, --subscription-date DATE    Renewal/expiration date (DD/MM/YYYY or YYYY-MM-DD)

  agym config <profile> [--model MODEL] [-y|--dsp|--skip-perms|--[no-]dangerously-skip-permissions]
      --model MODEL                   Set default model (or 'default' to clear)
      -y, --dsp, --skip-perms         Enable auto-skipping tool permissions
      --no-dsp, --no-skip-perms       Disable auto-skipping tool permissions

  agym rename <old-profile> <new-name>
      Rename a profile, its isolated data directory, and associated caches (alias: mv)

  agym edit <profile> [--name NEW_NAME] [-s, --subscription-date DATE | --clear-subscription-date]
      --name, --rename NEW_NAME       Rename the profile to a new name
      -s, --subscription-date DATE    Set renewal/expiration date (DD/MM/YYYY or YYYY-MM-DD)
      --clear-subscription-date       Remove stored subscription date
  agym usage [--json] [-c, --claude] [-f, --refresh] [-v, --view {table,grid,matrix,telemetry}] [--sort {usage,quota,reset,name,sub,default}] [--timeout SECONDS] [profiles...]
      --json                          Output quota and subscription data in JSON format
      -c, --claude                    Include Claude & GPT quotas in the usage view
      -f, --refresh                   Bypass cache and force live query
      -v, --view VIEW                 Visual graph layout: table (default), grid, matrix, or telemetry
      -g, --grid                      Shortcut for --view grid (borderless account view)
      -m, --matrix                    Shortcut for --view matrix (ultra-dense heatmap for dozens of accounts)
      -t, --telemetry                 Shortcut for --view telemetry (executive tiered view & recommendation)
      --sort CRITERION                Sort accounts by: usage (default), quota, reset, name, or sub
      --no-summary                    Hide top fleet capacity summary banner
      --timeout SECONDS               Per-profile query timeout in seconds (default: 30)

  agym tokens [--json] [-b, --breakdown] [-f, --refresh] [-v, --view {table,matrix,telemetry,classic}] [--sort {default,volume,cache,name}] [profiles...]
      --json                          Output token metrics and summary in JSON format
      -b, --breakdown                 Show detailed token composition breakdown table
      -f, --refresh                   Bypass cache and re-scan conversation databases
      -v, --view VIEW                 Visual layout: table (default), matrix, telemetry, classic
      -m, --matrix                    Shortcut for --view matrix (ultra-dense heatmap for dozens of accounts)
      -t, --telemetry                 Shortcut for --view telemetry (executive tiered view & analytics)
      --sort CRITERION                Sort accounts by: default, volume, cache, or name

  agym statusline [--preview [profile] | --sync | --status | --enable | --disable]
      --preview, -p [PROFILE]         Preview rendered statusline for active or specified profile
      --sync, -s                      Install and configure statusline across all profiles
      --status                        Show statusline configuration status across all profiles
      --enable                        Enable statusline for all profiles
      --disable                       Disable statusline for all profiles

  agym select [-f, --fresh] [-- [agy args...]]
      -f, --fresh                     Force fetch fresh usage data, ignoring 5-minute cache (alias: pick)

  agym remove <profile> [-y, --yes]
      -y, --yes                       Delete without interactive confirmation prompt

  agym <profile> --auto-pr [-b, --base BRANCH] [--title TITLE] [--body BODY] [--draft] [--no-push] [-n]
      -b, --base BRANCH               Target base branch for PR (default: main)
      --title TITLE                   Custom PR title (auto-generated from commits if omitted)
      --body BODY                     Custom PR description (auto-generated from diff if omitted)
      --draft                         Create pull request as a draft
      --no-push                       Skip pushing current branch to origin before PR creation
      -n, --dry-run                   Preview PR creation without pushing or creating a PR

  agym orchestrate "<task>" [--mode {plan,implement}] [-n, --dry-run]
      --mode {plan,implement}         Operating mode: plan (default) or implement
      -n, --dry-run                   Preview execution plan without worker execution
  agym orchestrate status <run-id>    Show status and progress of an orchestration run
  agym orchestrate resume <run-id>    Resume an interrupted or failed orchestration run

Examples:
  agym setup personal                 Create profile and authenticate with Google
  agym setup work -s 14/03/2027       Create profile with known subscription renewal date
  agym orchestrate "Refactor auth"    Plan orchestration for a task
  agym orchestrate status run-123     Check status of a run
  agym select                         Interactively pick an account based on quota health
  agym select -f                      Force live refresh of quotas before selection
  agym personal                       Open an interactive Antigravity session
  agym personal -p "write tests"      Run non-interactive Antigravity command
  agym personal --auto-pr             Create PR from current branch into main
  agym work --auto-pr -b develop      Create PR targeting develop branch
  agym work --auto-pr --draft         Create draft PR from current branch
  agym rename personal main           Rename profile 'personal' to 'main'
  agym rename jmcar AI1               Rename profile 'jmcar' to 'AI1'
  agym list                           Check status and renewal timeline of all profiles
  agym usage                          View live quota table and subscription health
  agym tokens                         View token consumption and fleet statistics
  agym tokens --breakdown             Show detailed token composition breakdown table
  agym tokens --json                  Export token consumption metrics as JSON
  agym statusline                     Show statusline status and preview across profiles
  agym statusline --sync              Ensure statusline is configured for all accounts
  agym statusline --preview personal  Preview statusline rendering for profile 'personal'
  agym edit personal -s 01/06/2027    Update subscription date for an existing profile
  agym remove old-account --yes       Remove profile without prompting
"""


def _print_err(message: str) -> None:
    print(f"agym: {message}", file=sys.stderr)


def _setup(argv: list[str], store: ProfileStore) -> int:
    parser = argparse.ArgumentParser(
        prog="agym setup",
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
    parser = argparse.ArgumentParser(prog="agym config", add_help=True)
    parser.add_argument("profile")
    parser.add_argument("--model", dest="model", default=None)
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
    ns = parser.parse_args(argv)
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
    parser = argparse.ArgumentParser(
        prog="agym rename",
        description="Rename a profile, its isolated data directory, and associated caches.",
        add_help=True,
    )
    parser.add_argument("old_profile", help="Current name of the profile")
    parser.add_argument("new_name", help="New name for the profile")
    ns = parser.parse_args(argv)
    validate_profile_name(ns.old_profile)
    validate_profile_name(ns.new_name)
    profile = store.rename(ns.old_profile, ns.new_name)
    print(f"Renamed profile '{ns.old_profile}' to '{profile.name}'.")
    print(f"Profile home: {profile.home}")
    return 0


def _edit(argv: list[str], store: ProfileStore) -> int:
    parser = argparse.ArgumentParser(
        prog="agym edit",
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
    parser = argparse.ArgumentParser(
        prog="agym list",
        description="List all configured profiles, state, and subscription renewal status.",
        add_help=True,
    )
    parser.parse_args(argv)
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
    parser = argparse.ArgumentParser(
        prog="agym remove",
        description="Delete a profile and its isolated data directory.",
        add_help=True,
    )
    parser.add_argument("profile", help="Name of the profile to remove")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
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
    parser = argparse.ArgumentParser(
        prog="agym doctor",
        description="Inspect Antigravity executable, paths, permissions, and profile health.",
        add_help=True,
    )
    parser.add_argument("profile", nargs="?", help="Optional specific profile to diagnose")
    ns = parser.parse_args(argv)
    for line in doctor_lines(store, ns.profile):
        print(line)
    return 0


def _usage(argv: list[str], store: ProfileStore) -> int:
    parser = argparse.ArgumentParser(
        prog="agym usage",
        description="Show quota limits, remaining capacity, reset times, and subscription health.",
        add_help=True,
    )
    parser.add_argument("--json", action="store_true", dest="json_mode", help="Output in JSON format")
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
        help="Sort order for profiles (default: usage; options: usage, quota, reset, name, sub, default)",
    )
    parser.add_argument(
        "--claude",
        "-c",
        action="store_true",
        dest="show_claude",
        help="Include Claude & GPT quotas in the usage view",
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
    parser.add_argument("profiles", nargs="*", help="Optional specific profiles to query")
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
    parser = argparse.ArgumentParser(
        prog="agym tokens",
        description="Show token usage breakdowns, comparison charts, and fleet statistics.",
        add_help=True,
    )
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
        help="Sort order for profiles (default, volume, cache, name)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Per-profile timeout in seconds (default: 30)",
    )
    parser.add_argument("profiles", nargs="*", help="Optional specific profiles to query")
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
    parser = argparse.ArgumentParser(
        prog="agym rotate",
        description="Rotate through configured profiles or an account file sequentially and launch Antigravity.",
        add_help=True,
    )
    parser.add_argument("--status", action="store_true", help="Show current rotation status and history")
    parser.add_argument("--reset", action="store_true", help="Reset rotation state back to the first account")
    parser.add_argument(
        "--simulate",
        nargs="?",
        const=3,
        type=int,
        metavar="COUNT",
        help="Simulate COUNT rotation steps without launching agy (default: 3)",
    )
    parser.add_argument(
        "--file",
        "-f",
        metavar="FILE",
        help="Custom account file (txt or json) to rotate through instead of configured profiles",
    )
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
    parser = argparse.ArgumentParser(
        prog="agym statusline",
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
    parser = argparse.ArgumentParser(
        prog="agym select",
        description="Interactively select an account based on cached 5h quota and launch Antigravity.",
        add_help=True,
    )
    parser.add_argument(
        "-f",
        "--fresh",
        action="store_true",
        help="Force fetch fresh usage data, ignoring 5-minute cache",
    )
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
    if any(arg in {"-h", "--help"} for arg in argv):
        parse_auto_pr_args(["--help"])
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
    ns = parse_auto_pr_args(remaining_argv)
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


ORCHESTRATE_USAGE = """agym orchestrate — Multi-agent orchestration for Google Antigravity CLI

Usage:
  agym orchestrate "<task>" [--mode plan|implement] [--dry-run]
  agym orchestrate status <run-id>
  agym orchestrate resume <run-id>
  agym orchestrate inspect <run-id> [--worker <id>] [--attempt <id>] [--timeline] [--json]
  agym orchestrate logs <run-id> [--follow] [--worker <id>] [--attempt <id>] [--stream stdout|stderr|all] [--json]

Options:
  --mode {plan,implement}   Operating mode: plan (default) or implement
  -n, --dry-run             Preview execution plan without worker execution
  -h, --help                Show this help message and exit

Commands:
  status <run-id>           Display status of a run from persisted state
  resume <run-id>           Resume an interrupted or failed run
  inspect <run-id>          Inspect run failure details, workers, attempts, and stored response
  logs <run-id>             View or follow execution logs and milestones
"""


def _format_run_status(state: RunState, results: Sequence[Any] | None = None) -> str:
    lines = [
        f"Run ID:        {state.run_id}",
        f"Status:        {state.status.value}",
        f"Mode:          {state.mode.value}",
        f"Task:          {state.task}",
        f"Rounds:        {state.round_number}",
        f"Invocations:   {state.budget_usage.invocations}",
        f"Runtime:       {format_duration(state.budget_usage.runtime_seconds) if state.budget_usage.runtime_seconds else '0s'}",
    ]
    if state.created_at:
        lines.append(f"Created:       {state.created_at}")
    if state.updated_at:
        lines.append(f"Updated:       {state.updated_at}")
    if state.assessment:
        lines.append(f"Complexity:    {state.assessment.complexity.value}")
    if results:
        lines.append(f"Results ({len(results)}):")
        for r in results:
            wid = getattr(r, "worker_id", "unknown")
            role = getattr(r, "role", "")
            r_val = role.value if hasattr(role, "value") else str(role)
            st = getattr(r, "status", "")
            st_val = st.value if hasattr(st, "value") else str(st)
            lines.append(f"  - [{wid}] {r_val}: {st_val}")
            if getattr(r, "error", None):
                lines.append(f"    Error: {r.error}")
    if state.final_result:
        lines.append("")
        lines.append("Final Result:")
        lines.append(state.final_result)
    return "\n".join(lines)


def _orchestrate(
    argv: list[str],
    store: ProfileStore,
    *,
    deps: OrchestrationDependencies | None = None,
) -> int:
    if not argv:
        _print_err("orchestrate requires a task or subcommand (status, resume, inspect, logs)")
        return 2

    if argv[0] not in {"inspect", "logs"} and any(arg in {"-h", "--help", "help"} for arg in argv):
        print(ORCHESTRATE_USAGE.rstrip())
        return 0

    subcommand = argv[0]

    # Subcommand: status
    if subcommand == "status":
        if len(argv) < 2 or not argv[1].strip():
            _print_err("missing run ID for status")
            return 2
        if len(argv) > 2:
            _print_err("status takes exactly one run ID")
            return 2
        run_id = argv[1].strip()
        d = deps or build_orchestration_dependencies(profile_store=store)
        state = d.run_store.get_run(RunId(run_id))
        if state is None:
            _print_err(f"run not found: {run_id}")
            return 1
        results = (
            d.run_store.get_results(RunId(run_id))
            if hasattr(d.run_store, "get_results")
            else None
        )
        print(_format_run_status(state, results))
        return 0

    # Subcommand: resume
    if subcommand == "resume":
        if len(argv) < 2 or not argv[1].strip():
            _print_err("missing run ID for resume")
            return 2
        if len(argv) > 2:
            _print_err("resume takes exactly one run ID")
            return 2
        run_id = argv[1].strip()
        d = deps or build_orchestration_dependencies(profile_store=store)
        try:
            state = d.engine.resume(RunId(run_id))
            run_dir = (
                d.run_store.run_dir(state.run_id)
                if hasattr(d.run_store, "run_dir") and callable(d.run_store.run_dir)
                else None
            )
            if run_dir:
                print(f"\nRun artifacts: {run_dir}")
                print(f"Inspect with: agym orchestrate inspect {state.run_id}")
            if state.status == RunStatus.COMPLETED:
                return 0
            if state.status == RunStatus.INTERRUPTED:
                _print_err("interrupted")
                return 130
            return 1
        except KeyboardInterrupt:
            run_dir = (
                d.run_store.run_dir(RunId(run_id))
                if hasattr(d.run_store, "run_dir") and callable(d.run_store.run_dir)
                else None
            )
            if run_dir and Path(run_dir).exists():
                print(f"\nRun artifacts: {run_dir}")
                print(f"Inspect with: agym orchestrate inspect {run_id}")
            _print_err("interrupted")
            return 130
        except Exception as exc:
            run_dir = (
                d.run_store.run_dir(RunId(run_id))
                if hasattr(d.run_store, "run_dir") and callable(d.run_store.run_dir)
                else None
            )
            if run_dir and Path(run_dir).exists():
                print(f"\nRun artifacts: {run_dir}")
                print(f"Inspect with: agym orchestrate inspect {run_id}")
            _print_err(str(exc))
            return 1

    # Subcommand: inspect
    if subcommand == "inspect":
        if len(argv) >= 2 and argv[1] in ("-h", "--help"):
            d = deps or build_orchestration_dependencies(profile_store=store)
            from agym.orchestration.inspection import run_inspect_cli
            return run_inspect_cli(argv[1:], d.run_store)
        if len(argv) < 2 or not argv[1].strip() or argv[1].startswith("-"):
            _print_err("missing run ID for inspect")
            return 2
        d = deps or build_orchestration_dependencies(profile_store=store)
        from agym.orchestration.inspection import run_inspect_cli
        return run_inspect_cli(argv[1:], d.run_store)

    # Subcommand: logs
    if subcommand == "logs":
        if len(argv) >= 2 and argv[1] in ("-h", "--help"):
            d = deps or build_orchestration_dependencies(profile_store=store)
            from agym.orchestration.inspection import run_logs_cli
            return run_logs_cli(argv[1:], d.run_store)
        if len(argv) < 2 or not argv[1].strip() or argv[1].startswith("-"):
            _print_err("missing run ID for logs")
            return 2
        d = deps or build_orchestration_dependencies(profile_store=store)
        from agym.orchestration.inspection import run_logs_cli
        return run_logs_cli(argv[1:], d.run_store)

    # Check for unrecognized subcommand
    KNOWN_SUBCOMMANDS = {"status", "resume", "run", "inspect", "logs"}
    UNKNOWN_SUBCOMMANDS = {
        "cancel",
        "stop",
        "kill",
        "info",
        "show",
        "list",
        "delete",
        "remove",
        "get",
        "unknown",
        "invalid",
        "unknown-subcommand",
        "badsubcommand",
    }
    if subcommand in UNKNOWN_SUBCOMMANDS:
        _print_err(f"unknown subcommand: '{subcommand}'")
        return 2

    if (
        len(argv) >= 2
        and argv[0] not in KNOWN_SUBCOMMANDS
        and not argv[0].startswith("-")
        and not argv[1].startswith("-")
    ):
        _print_err(f"unknown subcommand: '{argv[0]}'")
        return 2

    # Task execution
    args_to_parse = argv[1:] if subcommand == "run" else argv
    parser = argparse.ArgumentParser(prog="agym orchestrate", add_help=False)
    parser.add_argument("task", nargs="?", default=None)
    parser.add_argument(
        "--mode",
        dest="mode",
        default="plan",
        choices=["plan", "implement"],
        type=str.lower,
    )
    parser.add_argument("-n", "--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--profile", dest="profile", default=None, help="Profile to use for the coordinator")

    try:
        ns = parser.parse_args(args_to_parse)
    except SystemExit:
        return 2

    if not ns.task or not ns.task.strip():
        _print_err("a task is required")
        return 2

    task = ns.task.strip()
    run_mode = RunMode.IMPLEMENT if ns.mode == "implement" else RunMode.PLAN
    d = deps or build_orchestration_dependencies(profile_store=store, coordinator_profile=ns.profile)

    if ns.dry_run:
        try:
            plan = d.engine.dry_run(task, mode=run_mode)
            print(plan.format_display())
            return 0 if plan.is_valid else 1
        except KeyboardInterrupt:
            _print_err("interrupted")
            return 130
        except Exception as exc:
            _print_err(str(exc))
            return 1

    try:
        state = d.engine.run(task, mode=run_mode)
        run_dir = (
            d.run_store.run_dir(state.run_id)
            if hasattr(d.run_store, "run_dir") and callable(d.run_store.run_dir)
            else None
        )
        if run_dir:
            print(f"\nRun artifacts: {run_dir}")
            print(f"Inspect with: agym orchestrate inspect {state.run_id}")
        if state.status == RunStatus.COMPLETED:
            return 0
        if state.status == RunStatus.INTERRUPTED:
            _print_err("interrupted")
            return 130
        return 1
    except KeyboardInterrupt:
        _print_err("interrupted")
        return 130
    except Exception as exc:
        failed_rid = getattr(exc, "run_id", None)
        if failed_rid and hasattr(d.run_store, "run_dir"):
            try:
                run_dir = d.run_store.run_dir(failed_rid)
                if run_dir and Path(run_dir).exists():
                    print(f"\nRun artifacts: {run_dir}")
                    print(f"Inspect with: agym orchestrate inspect {failed_rid}")
            except Exception:
                pass
        _print_err(str(exc))
        return 1


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
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE.rstrip())
        return 0 if args else 2

    store = ProfileStore()
    command, rest = args[0], args[1:]

    # Prompt startup update check if running interactively and not dismissed in 24h
    maybe_prompt_startup_update(args)

    try:
        if command == "update":
            return run_update_cli(rest)
        if command == "setup":
            return _setup(rest, store)
        if command == "config":
            return _config(rest, store)
        if command == "edit":
            return _edit(rest, store)
        if command in {"rename", "mv"}:
            return _rename(rest, store)
        if command == "list":
            return _list(rest, store)
        if command in {"select", "pick"}:
            return _select(rest, store)
        if command == "rotate":
            return _rotate(rest, store)
        if command == "usage":
            return _usage(rest, store)
        if command in {"token", "tokens", "token-usage"}:
            return _tokens(rest, store)
        if command == "statusline":
            return _statusline(rest, store)
        if command == "remove":
            return _remove(rest, store)
        if command == "doctor":
            return _doctor(rest, store)
        if command in {"auto-pr", "--auto-pr"}:
            return _auto_pr(rest, store)
        if command == "orchestrate":
            return _orchestrate(rest, store)
        return _launch(command, rest, store)
    except (InvalidProfileName, ProfileExists, ProfileNotFound, ProfileError, AgyNotFound, SubscriptionError, AutoPrError) as exc:
        _print_err(str(exc))
        return 2
    except KeyboardInterrupt:
        _print_err("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
