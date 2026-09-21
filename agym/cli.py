from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cache import CacheManager
from .diagnostics import doctor_lines
from .launcher import (
    ALL_PERMISSIONS_ALIASES,
    AgyNotFound,
    persistent_profile_data_exists,
    resolve_agy,
    run_agy,
    run_auto_prompt,
)
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
from .tokens import run_tokens
from .usage import run_usage

USAGE = """agym — Explicit isolated-profile manager for Google Antigravity CLI

Usage:
  agym <command> [arguments...]
  agym <profile> [--] [agy args...]
  agym <profile> --auto-prompt "<prompt>"
  agym rotate [--file <file>] [--status] [--reset] [--simulate [N]] [-- [agy args...]]
  agym config <profile> [--model <model>|default] [-y|--dsp|--skip-perms|--[no-]dangerously-skip-permissions]

Commands:
  setup <profile>                     Create a new profile and complete Google sign-in
  config <profile>                    Configure profile model and permission settings
  edit <profile>                      Edit profile settings (e.g. subscription renewal date)
  list                                List all configured profiles and subscription status
  rotate                              Rotate through accounts/profiles sequentially and launch
  usage [profiles...]                 Show live model quota usage and subscription health
  tokens [profiles...]                Show token consumption graphs and fleet summary (aliases: token, token-usage)
  remove <profile>                    Delete a profile and its isolated data
  doctor [profile]                    Check environment, executable, permissions, and state

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

General Options:
  -h, --help                          Show this help message and exit

Command Options:
  agym setup <profile> [-s, --subscription-date DATE]
      -s, --subscription-date DATE    Renewal/expiration date (DD/MM/YYYY or YYYY-MM-DD)

  agym config <profile> [--model MODEL] [-y|--dsp|--skip-perms|--[no-]dangerously-skip-permissions]
      --model MODEL                   Set default model (or 'default' to clear)
      -y, --dsp, --skip-perms         Enable auto-skipping tool permissions
      --no-dsp, --no-skip-perms       Disable auto-skipping tool permissions

  agym edit <profile> [-s, --subscription-date DATE | --clear-subscription-date]
      -s, --subscription-date DATE    Set renewal/expiration date (DD/MM/YYYY or YYYY-MM-DD)
      --clear-subscription-date       Remove stored subscription date
  agym usage [--json] [-f, --refresh] [--timeout SECONDS] [profiles...]
      --json                          Output quota and subscription data in JSON format
      -f, --refresh                   Bypass cache and force live query
      --timeout SECONDS               Per-profile query timeout in seconds (default: 30)

  agym tokens [--json] [-b, --breakdown] [-f, --refresh] [profiles...]
      --json                          Output token metrics and summary in JSON format
      -b, --breakdown                 Show detailed token composition breakdown table
      -f, --refresh                   Bypass cache and re-scan conversation databases

  agym remove <profile> [-y, --yes]
      -y, --yes                       Delete without interactive confirmation prompt

Examples:
  agym setup personal                 Create profile and authenticate with Google
  agym setup work -s 14/03/2027       Create profile with known subscription renewal date
  agym personal                       Open an interactive Antigravity session
  agym personal -p "write tests"      Run non-interactive Antigravity command
  agym list                           Check status and renewal timeline of all profiles
  agym usage                          View live quota table and subscription health
  agym tokens                         View token consumption and fleet statistics
  agym tokens --breakdown             Show detailed token composition breakdown table
  agym tokens --json                  Export token consumption metrics as JSON
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
    code = run_agy(agy, profile, replace_process=False)
    if code != 0:
        _print_err(f"agy exited with status {code}; profile was kept for inspection/retry")
        return code

    if not persistent_profile_data_exists(profile):
        _print_err(
            "agy exited successfully, but no persistent data was detected under the isolated .gemini directory"
        )
        _print_err("run 'agym doctor %s' and the manual integration test before relying on this profile" % profile.name)
        return 1

    print(f"Profile '{profile.name}' is ready (persistent Antigravity state detected).")
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


def _edit(argv: list[str], store: ProfileStore) -> int:
    parser = argparse.ArgumentParser(
        prog="agym edit",
        description="Update profile configuration or subscription renewal date.",
        add_help=True,
    )
    parser.add_argument("profile", help="Name of the profile to edit")
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
    kwargs = {
        "json_mode": ns.json_mode,
        "breakdown": ns.breakdown,
        "timeout": ns.timeout,
    }
    if ns.refresh:
        kwargs["refresh"] = True
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


def _launch(profile_name: str, argv: list[str], store: ProfileStore) -> int:
    validate_profile_name(profile_name)
    # Resolve before environment construction so PATH lookup uses the host environment.
    agy = resolve_agy()
    profile = store.get(profile_name)

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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE.rstrip())
        return 0 if args else 2

    store = ProfileStore()
    command, rest = args[0], args[1:]
    try:
        if command == "setup":
            return _setup(rest, store)
        if command == "config":
            return _config(rest, store)
        if command == "edit":
            return _edit(rest, store)
        if command == "list":
            return _list(rest, store)
        if command == "rotate":
            return _rotate(rest, store)
        if command == "usage":
            return _usage(rest, store)
        if command in {"token", "tokens", "token-usage"}:
            return _tokens(rest, store)
        if command == "remove":
            return _remove(rest, store)
        if command == "doctor":
            return _doctor(rest, store)
        return _launch(command, rest, store)
    except (InvalidProfileName, ProfileExists, ProfileNotFound, ProfileError, AgyNotFound, SubscriptionError) as exc:
        _print_err(str(exc))
        return 2
    except KeyboardInterrupt:
        _print_err("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
