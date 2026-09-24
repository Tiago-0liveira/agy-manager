from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from ..launcher import build_profile_env
from ..profiles import Profile, ProfileError, ProfileStore, validate_profile_name


class AutoPrError(ProfileError):
    """Raised when an error occurs during the automated pull request workflow."""
    pass


def build_auto_pr_parser() -> argparse.ArgumentParser:
    """Builds and returns the ArgumentParser for auto-pr."""
    usage = """auto-pr [profile]
  [-b | --base BRANCH]
  [--title TITLE]
  [--body BODY]
  [--draft]
  [--no-push]
  [-n | --dry-run]"""
    parser = argparse.ArgumentParser(
        prog="agym auto-pr",
        usage=usage,
        description="Automatically create a pull request from the current branch into the target base branch.",
        add_help=True,
    )
    parser.add_argument("profile", nargs="?", help="Optional specific profile to use")
    parser.add_argument(
        "--auto-pr",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-b", "--base",
        metavar="BRANCH",
        default="main",
        help="Target base branch for PR (default: main).",
    )
    parser.add_argument(
        "--title",
        metavar="TITLE",
        default=None,
        help="Custom PR title (if omitted, auto-generated from commit history).",
    )
    parser.add_argument(
        "--body",
        metavar="BODY",
        default=None,
        help="Custom PR description (if omitted, auto-generated from diff and commits).",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        default=False,
        help="Opens the PR as a draft.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        default=False,
        help="Skips pushing current branch to remote before PR creation.",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        default=False,
        help="Preview PR creation without pushing changes or creating a PR.",
    )
    return parser


def parse_auto_pr_args(argv: list[str]) -> argparse.Namespace:
    """Parses CLI arguments for the --auto-pr command workflow."""
    return build_auto_pr_parser().parse_args(argv)


def get_current_branch(
    repo_path: Path | str = ".",
    env: Mapping[str, str] | None = None,
) -> str:
    """Returns the current git branch name for repo_path.

    Raises AutoPrError if not inside a git repository or in detached HEAD state.
    """
    repo = Path(repo_path).resolve()
    proc_check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_check.returncode != 0:
        raise AutoPrError("Not inside a git repository.")

    proc_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    branch = proc_branch.stdout.strip()
    if not branch:
        proc_sym = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        branch = proc_sym.stdout.strip()

    if not branch:
        raise AutoPrError("Detached HEAD state. Please checkout a branch.")

    return branch


def validate_branches(
    current_branch: str,
    base_branch: str,
    repo_path: Path | str = ".",
    env: Mapping[str, str] | None = None,
) -> str:
    """Validates that current_branch can open a PR into base_branch.

    Ensures current_branch != base_branch and that base_branch exists locally or on remote.
    Fetches base_branch from origin beforehand if possible.
    Returns the resolved base ref (e.g. 'origin/<base_branch>' or '<base_branch>').
    """
    repo = Path(repo_path).resolve()

    if current_branch == base_branch:
        raise AutoPrError(
            f"Current branch '{current_branch}' cannot target itself as base."
        )

    # Best-effort fetch of target base branch and prune origin refs
    try:
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    # Check origin/<base_branch> then <base_branch>
    proc_remote = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{base_branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_remote.returncode == 0:
        return f"origin/{base_branch}"

    proc_local = subprocess.run(
        ["git", "rev-parse", "--verify", base_branch],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_local.returncode == 0:
        return base_branch

    raise AutoPrError(f"Target base branch '{base_branch}' does not exist.")


def check_uncommitted_changes(
    repo_path: Path | str = ".",
    env: Mapping[str, str] | None = None,
) -> bool:
    """Checks for uncommitted working tree changes.

    Prints a warning to sys.stderr if working tree is dirty.
    Returns True if dirty, False otherwise.
    """
    repo = Path(repo_path).resolve()
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        print(
            "warning: working tree has uncommitted changes; these will not be included in the PR.",
            file=sys.stderr,
        )
        return True
    return False


def get_branch_changes(
    repo_path: Path | str,
    base_branch: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Inspects commits and file diff between target base branch and HEAD.

    Returns dict containing:
    - 'commits': list of formatted commit strings: ["title (short_hash)", ...]
    - 'commit_titles': list of raw commit subject titles: ["title", ...]
    - 'commit_count': integer number of commits ahead of base
    - 'stat': diffstat summary string
    - 'name_status': list of touched file status lines: ["M file1.py", "A file2.py", ...]
    - 'base_ref': resolved reference used for comparison
    """
    repo = Path(repo_path).resolve()

    # Determine base_ref: prefer origin/<base_branch> if it exists, else local <base_branch>
    proc_remote = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{base_branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    base_ref = f"origin/{base_branch}" if proc_remote.returncode == 0 else base_branch

    # Count commits ahead
    proc_count = subprocess.run(
        ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_count.returncode != 0 and base_ref != base_branch:
        base_ref = base_branch
        proc_count = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )

    commit_count = 0
    if proc_count.returncode == 0 and proc_count.stdout.strip().isdigit():
        commit_count = int(proc_count.stdout.strip())

    if commit_count == 0:
        current = get_current_branch(repo, env=env)
        raise AutoPrError(f"Branch '{current}' has no commits ahead of '{base_branch}'.")

    # Get formatted commit messages
    proc_log = subprocess.run(
        ["git", "log", f"{base_ref}..HEAD", "--pretty=format:%s (%h)"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    commits = [line.strip() for line in proc_log.stdout.splitlines() if line.strip()]
    if not commits:
        proc_fallback = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s (%h)"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        commits = [line.strip() for line in proc_fallback.stdout.splitlines() if line.strip()]

    # Get raw commit subject titles
    proc_titles = subprocess.run(
        ["git", "log", f"{base_ref}..HEAD", "--pretty=format:%s"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    commit_titles = [line.strip() for line in proc_titles.stdout.splitlines() if line.strip()]
    if not commit_titles:
        proc_fallback_t = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        commit_titles = [line.strip() for line in proc_fallback_t.stdout.splitlines() if line.strip()]

    # Get diff stat
    proc_stat = subprocess.run(
        ["git", "diff", "--stat", f"{base_ref}...HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    diff_stat = proc_stat.stdout.strip()
    if not diff_stat:
        proc_stat2 = subprocess.run(
            ["git", "diff", "--stat", f"{base_ref}..HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        diff_stat = proc_stat2.stdout.strip()

    # Get name-status
    proc_names = subprocess.run(
        ["git", "diff", "--name-status", f"{base_ref}...HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    name_status = [line.strip() for line in proc_names.stdout.splitlines() if line.strip()]
    if not name_status:
        proc_names2 = subprocess.run(
            ["git", "diff", "--name-status", f"{base_ref}..HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        name_status = [line.strip() for line in proc_names2.stdout.splitlines() if line.strip()]

    return {
        "commits": commits,
        "commit_titles": commit_titles,
        "commit_count": commit_count,
        "stat": diff_stat,
        "name_status": name_status,
        "base_ref": base_ref,
    }


def generate_pr_content(
    current_branch: str,
    commits: list[str],
    diff_stat: str,
    base_branch: str = "main",
    custom_title: str | None = None,
    custom_body: str | None = None,
    commit_titles: list[str] | None = None,
) -> tuple[str, str]:
    """Generates PR title and markdown body from branch name, commits, and diff statistics."""
    # 1. PR Title
    if custom_title is not None and custom_title.strip():
        pr_title = custom_title.strip()
    elif len(commits) == 1:
        if commit_titles and commit_titles[0]:
            pr_title = commit_titles[0]
        else:
            pr_title = re.sub(r"\s+\([0-9a-fA-F]+\)$", "", commits[0]).strip()
    else:
        # Multi-commit: derive from branch name
        prefix_pattern = r"^(feat|feature|fix|chore|docs|refactor|test|perf|style|ci|build)/(.+)$"
        match = re.match(prefix_pattern, current_branch, re.IGNORECASE)
        if match:
            prefix = match.group(1).lower()
            if prefix == "feature":
                prefix = "feat"
            desc = match.group(2).replace("-", " ").replace("_", " ").strip()
            pr_title = f"{prefix}: {desc}"
        else:
            clean_name = current_branch.replace("-", " ").replace("_", " ").strip()
            pr_title = f"Changes from {clean_name}"

    # 2. PR Body
    if custom_body is not None and custom_body.strip():
        pr_body = custom_body
    else:
        commit_items = []
        for c in commits:
            item = c if c.startswith("- ") else f"- {c}"
            commit_items.append(item)
        commits_block = "\n".join(commit_items) if commit_items else "- No commits"

        if diff_stat:
            diff_block = f"```text\n{diff_stat}\n```"
        else:
            diff_block = "```text\n(no file changes)\n```"

        pr_body = f"""## Summary
Automated PR generated from branch `{current_branch}` into `{base_branch}`.

### Commits
{commits_block}

### Changes Overview
{diff_block}"""

    return pr_title, pr_body


def push_branch_if_needed(
    repo_path: Path | str,
    current_branch: str,
    env: Mapping[str, str] | None = None,
) -> None:
    """Pushes current_branch to origin, setting upstream tracking if not already set."""
    repo = Path(repo_path).resolve()

    # Check if upstream tracking branch is already set
    proc_upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_upstream.returncode != 0:
        cmd = ["git", "push", "-u", "origin", current_branch]
    else:
        cmd = ["git", "push"]

    proc_push = subprocess.run(
        cmd,
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_push.returncode != 0:
        err = proc_push.stderr.strip() or proc_push.stdout.strip()
        raise AutoPrError(f"Failed to push branch '{current_branch}' to origin: {err}")


def create_pull_request(
    repo_path: Path | str,
    current_branch: str,
    base_branch: str,
    title: str,
    body: str,
    draft: bool = False,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> str:
    """Creates a pull request using GitHub CLI (gh).

    Checks gh presence, authentication, and existing open PRs before creating.
    Returns the created (or existing) PR URL.
    """
    repo = Path(repo_path).resolve()
    path_val = env.get("PATH") if env else None

    if not shutil.which("gh", path=path_val):
        raise AutoPrError(
            "GitHub CLI ('gh') is required to open pull requests automatically.\n"
            "Please install 'gh' or authenticate using 'gh auth login'."
        )

    proc_auth = subprocess.run(
        ["gh", "auth", "status"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_auth.returncode != 0:
        raise AutoPrError("GitHub CLI is not logged in. Run 'gh auth login' first.")

    # Check if a PR is already open for this branch
    proc_view = subprocess.run(
        ["gh", "pr", "view", current_branch, "--json", "url", "-q", ".url"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_view.returncode == 0 and proc_view.stdout.strip():
        existing_url = proc_view.stdout.strip()
        print(f"PR already open for this branch: {existing_url}", file=sys.stderr)
        return existing_url

    if dry_run:
        print(
            f"[dry-run] Would execute: gh pr create --base \"{base_branch}\" "
            f"--head \"{current_branch}\" --title \"{title}\"",
            file=sys.stderr,
        )
        proc_dry = subprocess.run(
            [
                "gh", "pr", "create",
                "--base", base_branch,
                "--head", current_branch,
                "--title", title,
                "--body", body,
                "--dry-run",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        if proc_dry.returncode == 0 and proc_dry.stdout.strip():
            return proc_dry.stdout.strip()
        return "https://github.com/dry-run/pull/dry-run"

    cmd = [
        "gh", "pr", "create",
        "--base", base_branch,
        "--head", current_branch,
        "--title", title,
        "--body", body,
    ]
    if draft:
        cmd.append("--draft")

    proc_create = subprocess.run(
        cmd,
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc_create.returncode != 0:
        # Check if error is because PR already exists or was opened concurrently
        proc_recheck = subprocess.run(
            ["gh", "pr", "view", current_branch, "--json", "url", "-q", ".url"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        if proc_recheck.returncode == 0 and proc_recheck.stdout.strip():
            existing_url = proc_recheck.stdout.strip()
            print(f"PR already open for this branch: {existing_url}", file=sys.stderr)
            return existing_url

        err = proc_create.stderr.strip() or proc_create.stdout.strip()
        raise AutoPrError(f"Failed to create pull request: {err}")

    return proc_create.stdout.strip()


def handle_auto_pr(
    profile: Profile | str,
    base_branch: str = "main",
    title: str | None = None,
    body: str | None = None,
    draft: bool = False,
    no_push: bool = False,
    dry_run: bool = False,
    repo_path: Path | str | None = None,
    store: ProfileStore | None = None,
) -> int:
    """Executes the automated pull request workflow under the profile context.

    Returns 0 on success, 1 on failure.
    """
    try:
        if isinstance(profile, str):
            if store is None:
                store = ProfileStore()
            validate_profile_name(profile)
            profile_obj = store.get(profile)
        else:
            profile_obj = profile

        env = build_profile_env(profile_obj.home, profile_name=profile_obj.name)
        target_dir = Path.cwd().resolve() if repo_path is None else Path(repo_path).resolve()

        # Step 1: Pre-flight checks
        current_branch = get_current_branch(target_dir, env=env)
        check_uncommitted_changes(target_dir, env=env)
        validate_branches(current_branch, base_branch, target_dir, env=env)

        # Step 2: Diff and commit change inspection
        changes = get_branch_changes(target_dir, base_branch, env=env)

        # Step 3: PR metadata synthesis
        pr_title, pr_body = generate_pr_content(
            current_branch=current_branch,
            commits=changes["commits"],
            diff_stat=changes["stat"],
            base_branch=base_branch,
            custom_title=title,
            custom_body=body,
            commit_titles=changes.get("commit_titles"),
        )

        # Step 4: Push branch if needed
        if not no_push and not dry_run:
            print(f"Pushing branch '{current_branch}' to origin...", file=sys.stderr)
            push_branch_if_needed(target_dir, current_branch, env=env)
        elif dry_run:
            print(f"[dry-run] Skipping git push for '{current_branch}'", file=sys.stderr)

        # Step 5: Create pull request
        print(f"Creating pull request into '{base_branch}'...", file=sys.stderr)
        pr_url = create_pull_request(
            repo_path=target_dir,
            current_branch=current_branch,
            base_branch=base_branch,
            title=pr_title,
            body=pr_body,
            draft=draft,
            env=env,
            dry_run=dry_run,
        )

        print(pr_url)
        return 0

    except AutoPrError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
