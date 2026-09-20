from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .diagnostics import doctor_lines
from .launcher import AgyNotFound, agy_version, persistent_profile_data_exists, resolve_agy, run_agy
from .profiles import (
    InvalidProfileName,
    ProfileError,
    ProfileExists,
    ProfileNotFound,
    ProfileStore,
    validate_profile_name,
)
from .usage import run_usage

USAGE = """usage:
  agym setup <profile>
  agym <profile> [--] [agy args...]
  agym list
  agym usage [--json] [--timeout SECONDS] [profiles...]
  agym remove <profile> [--yes]
  agym doctor [profile]
"""


def _print_err(message: str) -> None:
    print(f"agym: {message}", file=sys.stderr)


def _setup(argv: list[str], store: ProfileStore) -> int:
    parser = argparse.ArgumentParser(prog="agym setup", add_help=True)
    parser.add_argument("profile")
    ns = parser.parse_args(argv)
    validate_profile_name(ns.profile)

    # Resolve the host binary before constructing or using the isolated HOME.
    agy = resolve_agy()
    version = agy_version(agy)
    profile = store.create(ns.profile, agy_version=version)

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


def _list(argv: list[str], store: ProfileStore) -> int:
    if argv:
        raise ProfileError("'agym list' takes no arguments")
    profiles = store.list()
    if not profiles:
        print("No profiles.")
        return 0
    for profile in profiles:
        state = "ready" if persistent_profile_data_exists(profile) else "no-state"
        version = f"; created with {profile.agy_version}" if profile.agy_version else ""
        print(f"{profile.name}\t{state}{version}")
    return 0


def _remove(argv: list[str], store: ProfileStore) -> int:
    parser = argparse.ArgumentParser(prog="agym remove", add_help=True)
    parser.add_argument("profile")
    parser.add_argument("--yes", action="store_true")
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
    parser = argparse.ArgumentParser(prog="agym doctor", add_help=True)
    parser.add_argument("profile", nargs="?")
    ns = parser.parse_args(argv)
    for line in doctor_lines(store, ns.profile):
        print(line)
    return 0


def _usage(argv: list[str], store: ProfileStore) -> int:
    parser = argparse.ArgumentParser(prog="agym usage", add_help=True)
    parser.add_argument("--json", action="store_true", dest="json_mode", help="output in JSON format")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-profile timeout in seconds (default: 30)")
    parser.add_argument("profiles", nargs="*", help="optional specific profiles to query")
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
    try:
        asyncio.run(
            run_usage(
                agy,
                profiles,
                json_mode=ns.json_mode,
                timeout=ns.timeout,
            )
        )
        return 0
    except KeyboardInterrupt:
        _print_err("interrupted")
        return 130


def _launch(profile_name: str, argv: list[str], store: ProfileStore) -> int:
    validate_profile_name(profile_name)
    # Resolve before environment construction so PATH lookup uses the host environment.
    agy = resolve_agy()
    profile = store.get(profile_name)
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
        if command == "list":
            return _list(rest, store)
        if command == "usage":
            return _usage(rest, store)
        if command == "remove":
            return _remove(rest, store)
        if command == "doctor":
            return _doctor(rest, store)
        return _launch(command, rest, store)
    except (InvalidProfileName, ProfileExists, ProfileNotFound, ProfileError, AgyNotFound) as exc:
        _print_err(str(exc))
        return 2
    except KeyboardInterrupt:
        _print_err("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
