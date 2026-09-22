#!/usr/bin/env python3
"""Release Version Manager for agym CI/CD.

Determines the target release version for pushes to main:
1. Reads base version from pyproject.toml / agym/__init__.py.
2. Checks existing git tags and/or GitHub Releases for tag collision.
3. If v<base> already exists, automatically auto-increments the patch version
   (e.g., 0.1.0 -> 0.1.1 -> 0.1.2) until a unique, unreleased tag is found.
4. Updates pyproject.toml and agym/__init__.py in-place.
5. Writes step outputs for GitHub Actions ($GITHUB_OUTPUT).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def parse_semver(ver_str: str) -> tuple[int, int, int]:
    clean = ver_str.strip().lstrip("v")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", clean)
    if not m:
        raise ValueError(f"Invalid semver version: {ver_str}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def format_semver(major: int, minor: int, patch: int) -> str:
    return f"{major}.{minor}.{patch}"


def get_local_version(root_dir: Path) -> str:
    init_path = root_dir / "agym" / "__init__.py"
    if init_path.exists():
        content = init_path.read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
        if m:
            return m.group(1).strip()

    pyproject_path = root_dir / "pyproject.toml"
    if pyproject_path.exists():
        content = pyproject_path.read_text(encoding="utf-8")
        m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
        if m:
            return m.group(1).strip()

    raise RuntimeError("Could not determine local version from agym/__init__.py or pyproject.toml")


def get_existing_tags_from_git() -> set[str]:
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "tag", "-l"],
            capture_output=True,
            text=True,
            check=True,
        )
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    except Exception:
        return set()


def get_existing_tags_from_github(repo: str, token: str | None = None) -> set[str]:
    tags = set()
    url = f"https://api.github.com/repos/{repo}/tags?per_page=100"
    headers = {"User-Agent": "agym-release-manager"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                for item in data:
                    tag_name = item.get("name")
                    if tag_name:
                        tags.add(tag_name)
    except Exception as exc:
        sys.stderr.write(f"Warning: could not query GitHub tags ({exc})\n")
    return tags


def compute_next_version(base_version: str, existing_tags: set[str]) -> str:
    major, minor, patch = parse_semver(base_version)
    candidate_ver = format_semver(major, minor, patch)
    candidate_tag = f"v{candidate_ver}"

    while candidate_tag in existing_tags or candidate_ver in existing_tags:
        patch += 1
        candidate_ver = format_semver(major, minor, patch)
        candidate_tag = f"v{candidate_ver}"

    return candidate_ver


def update_source_version(root_dir: Path, new_version: str) -> None:
    # Update agym/__init__.py
    init_path = root_dir / "agym" / "__init__.py"
    if init_path.exists():
        content = init_path.read_text(encoding="utf-8")
        updated = re.sub(
            r'(__version__\s*=\s*["\'])[^"\']+(["\'])',
            rf"\g<1>{new_version}\g<2>",
            content,
        )
        init_path.write_text(updated, encoding="utf-8")

    # Update pyproject.toml
    pyproject_path = root_dir / "pyproject.toml"
    if pyproject_path.exists():
        content = pyproject_path.read_text(encoding="utf-8")
        updated = re.sub(
            r'(version\s*=\s*["\'])[^"\']+(["\'])',
            rf"\g<1>{new_version}\g<2>",
            content,
        )
        pyproject_path.write_text(updated, encoding="utf-8")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    base_version = get_local_version(repo_root)

    repo_slug = os.environ.get("GITHUB_REPOSITORY", "Tiago-0liveira/agy-manager")
    token = os.environ.get("GITHUB_TOKEN")

    existing_tags = get_existing_tags_from_git()
    gh_tags = get_existing_tags_from_github(repo_slug, token=token)
    all_existing = existing_tags.union(gh_tags)

    final_version = compute_next_version(base_version, all_existing)
    final_tag = f"v{final_version}"

    update_source_version(repo_root, final_version)

    print(f"Base version:     {base_version}")
    print(f"Resolved version: {final_version}")
    print(f"Resolved tag:     {final_tag}")

    # Set GitHub Actions step outputs if running in Actions environment
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output and os.path.exists(github_output):
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"version={final_version}\n")
            f.write(f"tag_name={final_tag}\n")
            f.write(f"is_bumped={'true' if final_version != base_version else 'false'}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
