from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestCreatePrScript(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_create_pr_")
        self.bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(self.bin_dir, exist_ok=True)
        self.repo_dir = os.path.join(self.temp_dir, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)

        # Locate script
        self.script_path = Path(__file__).resolve().parent.parent / "scripts" / "create-pr.sh"
        self.assertTrue(self.script_path.exists(), f"{self.script_path} does not exist")

        # Create git repo in repo_dir
        self.run_git(["init", "-b", "main"], cwd=self.repo_dir)
        self.run_git(["config", "user.name", "Test User"], cwd=self.repo_dir)
        self.run_git(["config", "user.email", "test@example.com"], cwd=self.repo_dir)

        # Initial commit on main
        readme = Path(self.repo_dir) / "README.md"
        readme.write_text("initial")
        self.run_git(["add", "README.md"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "Initial commit"], cwd=self.repo_dir)

        # Create remote repo
        self.remote_dir = os.path.join(self.temp_dir, "remote.git")
        self.run_git(["init", "--bare", self.remote_dir])
        self.run_git(["remote", "add", "origin", self.remote_dir], cwd=self.repo_dir)
        self.run_git(["push", "-u", "origin", "main"], cwd=self.repo_dir)

        # Configure local git alias
        self.run_git(["config", "alias.pr-create", f"!{self.script_path}"], cwd=self.repo_dir)

        # Default mock gh script
        self.mock_gh_log = os.path.join(self.temp_dir, "gh_calls.log")
        self.mock_gh_body_log = os.path.join(self.temp_dir, "gh_body.log")
        self.create_mock_gh(authenticated=True, existing_pr="")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_git(self, args: list[str], cwd: str | None = None) -> str:
        cmd = ["git"] + args
        res = subprocess.run(
            cmd, cwd=cwd or self.repo_dir, capture_output=True, text=True, check=True
        )
        return res.stdout.strip()

    def create_mock_gh(self, authenticated: bool = True, existing_pr: str = "", pr_url: str = "https://github.com/test/repo/pull/1"):
        mock_gh = os.path.join(self.bin_dir, "gh")
        lines = [
            "#!/usr/bin/env bash",
            f'echo "$@" >> "{self.mock_gh_log}"',
        ]
        if not authenticated:
            lines.append('if [[ "$1" == "auth" ]]; then exit 1; fi')
        else:
            lines.append('if [[ "$1" == "auth" ]]; then exit 0; fi')

        lines.extend([
            'if [[ "$1" == "repo" && "$2" == "view" ]]; then',
            '  echo "main"',
            '  exit 0',
            'fi',
            'if [[ "$1" == "pr" && "$2" == "view" ]]; then',
        ])
        if existing_pr:
            lines.append(f'  echo "{existing_pr}"')
            lines.append("  exit 0")
        else:
            lines.append("  exit 1")
        lines.extend([
            "fi",
            'if [[ "$1" == "pr" && "$2" == "create" ]]; then',
            '  for i in "$@"; do',
            f'    echo "$i" >> "{self.mock_gh_body_log}"',
            '  done',
            f'  echo "{pr_url}"',
            "  exit 0",
            "fi",
            "exit 0",
        ])
        with open(mock_gh, "w") as f:
            f.write("\n".join(lines) + "\n")
        st = os.stat(mock_gh)
        os.chmod(mock_gh, st.st_mode | stat.S_IEXEC)

    def run_script(self, args: list[str], cwd: str | None = None, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}:{env['PATH']}"
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [str(self.script_path)] + args,
            cwd=cwd or self.repo_dir,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_help_flag(self):
        res = self.run_script(["--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("Usage:", res.stdout)
        self.assertIn("Automated Pull Request Creation Command", res.stdout)

    def test_gh_unauthenticated_error(self):
        self.create_mock_gh(authenticated=False)
        res = self.run_script([])
        self.assertEqual(res.returncode, 1)
        self.assertIn("GitHub CLI is not logged in", res.stderr)

    def test_single_commit_title_and_base_detection(self):
        # Create branch
        self.run_git(["checkout", "-b", "feat/test-single-commit"], cwd=self.repo_dir)
        test_file = Path(self.repo_dir) / "test.txt"
        test_file.write_text("feature content")
        self.run_git(["add", "test.txt"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "feat: add test file"], cwd=self.repo_dir)

        res = self.run_script(["--base", "main"])
        self.assertEqual(res.returncode, 0, f"Script failed: {res.stderr}")
        self.assertEqual(res.stdout.strip(), "https://github.com/test/repo/pull/1")

        # Verify gh pr create was called with correct title and base
        with open(self.mock_gh_log) as f:
            calls = f.read()
        self.assertIn("pr create", calls)
        self.assertIn("--title feat: add test file", calls)
        self.assertIn("--base main", calls)

        # Verify PR body format
        with open(self.mock_gh_body_log) as f:
            body_content = f.read()
        self.assertIn("## Overview", body_content)
        self.assertIn("## Changes Included", body_content)
        self.assertIn("feat: add test file", body_content)
        self.assertIn("## Verification", body_content)

    def test_multi_commit_formatted_branch_title(self):
        self.run_git(["checkout", "-b", "feat/multi-feature-test"], cwd=self.repo_dir)
        test_file1 = Path(self.repo_dir) / "test1.txt"
        test_file1.write_text("content 1")
        self.run_git(["add", "test1.txt"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "feat: step one"], cwd=self.repo_dir)

        test_file2 = Path(self.repo_dir) / "test2.txt"
        test_file2.write_text("content 2")
        self.run_git(["add", "test2.txt"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "feat: step two"], cwd=self.repo_dir)

        res = self.run_script(["--base", "main"])
        self.assertEqual(res.returncode, 0, f"Script failed: {res.stderr}")
        self.assertEqual(res.stdout.strip(), "https://github.com/test/repo/pull/1")

        with open(self.mock_gh_log) as f:
            calls = f.read()
        self.assertIn("pr create", calls)
        # multi-commit: should use formatted branch title "feat: multi feature test"
        self.assertIn("--title feat: multi feature test", calls)

    def test_positional_base_argument(self):
        self.run_git(["checkout", "-b", "feat/positional-base-test"], cwd=self.repo_dir)
        test_file = Path(self.repo_dir) / "pos.txt"
        test_file.write_text("content")
        self.run_git(["add", "pos.txt"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "feat: positional base"], cwd=self.repo_dir)

        res = self.run_script(["main"])
        self.assertEqual(res.returncode, 0)
        with open(self.mock_gh_log) as f:
            calls = f.read()
        self.assertIn("--base main", calls)

    def test_branch_targeting_itself_error(self):
        # On main branch targeting main
        res = self.run_script(["--base", "main"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("cannot target itself as base", res.stderr)

    def test_existing_pr_short_circuit(self):
        self.create_mock_gh(existing_pr="https://github.com/test/repo/pull/42")
        self.run_git(["checkout", "-b", "feat/existing"], cwd=self.repo_dir)
        test_file = Path(self.repo_dir) / "existing.txt"
        test_file.write_text("content")
        self.run_git(["add", "existing.txt"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "feat: existing test"], cwd=self.repo_dir)

        res = self.run_script(["--base", "main"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "https://github.com/test/repo/pull/42")

        with open(self.mock_gh_log) as f:
            calls = f.read()
        self.assertNotIn("pr create", calls)

    def test_dry_run_mode(self):
        self.run_git(["checkout", "-b", "feat/dry-run-test"], cwd=self.repo_dir)
        test_file = Path(self.repo_dir) / "dry.txt"
        test_file.write_text("dry run content")
        self.run_git(["add", "dry.txt"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "feat: dry run test"], cwd=self.repo_dir)

        res = self.run_script(["--dry-run", "--base", "main"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("[dry-run]", res.stderr)
        # Check that remote did not receive the branch push in dry run
        remote_branches = self.run_git(["branch", "-r"], cwd=self.repo_dir)
        self.assertNotIn("feat/dry-run-test", remote_branches)

    def test_git_pr_create_alias(self):
        self.run_git(["checkout", "-b", "feat/alias-test"], cwd=self.repo_dir)
        test_file = Path(self.repo_dir) / "alias.txt"
        test_file.write_text("alias content")
        self.run_git(["add", "alias.txt"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "feat: test alias invocation"], cwd=self.repo_dir)

        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}:{env['PATH']}"
        res = subprocess.run(
            ["git", "pr-create", "main"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(res.returncode, 0, f"git alias failed: {res.stderr}")
        self.assertEqual(res.stdout.strip(), "https://github.com/test/repo/pull/1")


if __name__ == "__main__":
    unittest.main()
