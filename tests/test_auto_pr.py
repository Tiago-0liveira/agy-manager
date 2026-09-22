from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym import cli
from agym.git.auto_pr import (
    AutoPrError,
    check_uncommitted_changes,
    create_pull_request,
    generate_pr_content,
    get_branch_changes,
    get_current_branch,
    handle_auto_pr,
    parse_auto_pr_args,
    push_branch_if_needed,
    validate_branches,
)
from agym.profiles import ProfileStore


class TestAutoPr(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_auto_pr_")
        self.bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(self.bin_dir, exist_ok=True)
        self.repo_dir = os.path.join(self.temp_dir, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)

        # Initialize git repo in repo_dir
        self.run_git(["init", "-b", "main"], cwd=self.repo_dir)
        self.run_git(["config", "user.name", "Test User"], cwd=self.repo_dir)
        self.run_git(["config", "user.email", "test@example.com"], cwd=self.repo_dir)

        # Initial commit on main
        readme = Path(self.repo_dir) / "README.md"
        readme.write_text("initial repo content\n")
        self.run_git(["add", "README.md"], cwd=self.repo_dir)
        self.run_git(["commit", "-m", "Initial commit"], cwd=self.repo_dir)

        # Create remote bare repo
        self.remote_dir = os.path.join(self.temp_dir, "remote.git")
        self.run_git(["init", "--bare", self.remote_dir])
        self.run_git(["remote", "add", "origin", self.remote_dir], cwd=self.repo_dir)
        self.run_git(["push", "-u", "origin", "main"], cwd=self.repo_dir)

        # Default mock gh script
        self.mock_gh_log = os.path.join(self.temp_dir, "gh_calls.log")
        self.mock_gh_body_log = os.path.join(self.temp_dir, "gh_body.log")
        self.create_mock_gh(authenticated=True, existing_pr="")

        # Setup isolated profile store
        self.profile_config = Path(self.temp_dir) / "config"
        self.profile_data = Path(self.temp_dir) / "data"
        self.store = ProfileStore(self.profile_config, self.profile_data)
        self.profile = self.store.create("testprof")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_git(self, args: list[str], cwd: str | None = None) -> str:
        cmd = ["git"] + args
        res = subprocess.run(
            cmd, cwd=cwd or self.repo_dir, capture_output=True, text=True, check=True
        )
        return res.stdout.strip()

    def create_mock_gh(
        self,
        authenticated: bool = True,
        existing_pr: str = "",
        pr_url: str = "https://github.com/example/repo/pull/42",
        fail_create: bool = False,
    ):
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
        ])
        if fail_create:
            lines.append('  echo "fatal: error creating pull request" >&2')
            lines.append("  exit 1")
        else:
            lines.append(f'  echo "{pr_url}"')
            lines.append("  exit 0")
        lines.extend([
            "fi",
            "exit 0",
        ])
        with open(mock_gh, "w") as f:
            f.write("\n".join(lines) + "\n")
        st = os.stat(mock_gh)
        os.chmod(mock_gh, st.st_mode | stat.S_IEXEC)

    def get_test_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}:{env['PATH']}"
        return env

    # -------------------------------------------------------------------------
    # Unit Tests: get_current_branch
    # -------------------------------------------------------------------------
    def test_get_current_branch_success(self):
        env = self.get_test_env()
        self.assertEqual(get_current_branch(self.repo_dir, env=env), "main")

        self.run_git(["checkout", "-b", "feat/cool-feature"], cwd=self.repo_dir)
        self.assertEqual(get_current_branch(self.repo_dir, env=env), "feat/cool-feature")

    def test_get_current_branch_not_in_repo(self):
        env = self.get_test_env()
        outside_dir = os.path.join(self.temp_dir, "outside")
        os.makedirs(outside_dir, exist_ok=True)
        with self.assertRaises(AutoPrError) as ctx:
            get_current_branch(outside_dir, env=env)
        self.assertIn("Not inside a git repository", str(ctx.exception))

    def test_get_current_branch_detached_head(self):
        env = self.get_test_env()
        # Create commit and checkout hash
        f = Path(self.repo_dir) / "f.txt"
        f.write_text("hello")
        self.run_git(["add", "f.txt"])
        self.run_git(["commit", "-m", "temp commit"])
        rev = self.run_git(["rev-parse", "HEAD"])
        self.run_git(["checkout", rev])

        with self.assertRaises(AutoPrError) as ctx:
            get_current_branch(self.repo_dir, env=env)
        self.assertIn("Detached HEAD", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Unit Tests: validate_branches
    # -------------------------------------------------------------------------
    def test_validate_branches_same_branch_fails(self):
        env = self.get_test_env()
        with self.assertRaises(AutoPrError) as ctx:
            validate_branches("main", "main", self.repo_dir, env=env)
        self.assertIn("cannot target itself as base", str(ctx.exception))

    def test_validate_branches_success(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/branch-1"], cwd=self.repo_dir)
        resolved = validate_branches("feat/branch-1", "main", self.repo_dir, env=env)
        self.assertIn("main", resolved)

    def test_validate_branches_nonexistent_base(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/branch-1"], cwd=self.repo_dir)
        with self.assertRaises(AutoPrError) as ctx:
            validate_branches("feat/branch-1", "nonexistent-base", self.repo_dir, env=env)
        self.assertIn("does not exist", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Unit Tests: check_uncommitted_changes
    # -------------------------------------------------------------------------
    def test_check_uncommitted_changes_clean(self):
        env = self.get_test_env()
        stderr_buf = io.StringIO()
        with mock.patch("sys.stderr", stderr_buf):
            dirty = check_uncommitted_changes(self.repo_dir, env=env)
        self.assertFalse(dirty)
        self.assertEqual(stderr_buf.getvalue(), "")

    def test_check_uncommitted_changes_dirty(self):
        env = self.get_test_env()
        (Path(self.repo_dir) / "untracked.txt").write_text("unstaged")
        stderr_buf = io.StringIO()
        with mock.patch("sys.stderr", stderr_buf):
            dirty = check_uncommitted_changes(self.repo_dir, env=env)
        self.assertTrue(dirty)
        self.assertIn("warning: working tree has uncommitted changes", stderr_buf.getvalue())

    # -------------------------------------------------------------------------
    # Unit Tests: get_branch_changes & generate_pr_content
    # -------------------------------------------------------------------------
    def test_get_branch_changes_zero_commits_fails(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/no-commits"], cwd=self.repo_dir)
        with self.assertRaises(AutoPrError) as ctx:
            get_branch_changes(self.repo_dir, "main", env=env)
        self.assertIn("has no commits ahead of", str(ctx.exception))

    def test_single_commit_content_generation(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/single-item"], cwd=self.repo_dir)
        f = Path(self.repo_dir) / "item.py"
        f.write_text("item = 1\n")
        self.run_git(["add", "item.py"])
        self.run_git(["commit", "-m", "feat: add item implementation"])

        changes = get_branch_changes(self.repo_dir, "main", env=env)
        self.assertEqual(changes["commit_count"], 1)
        self.assertIn("item.py", changes["stat"])

        title, body = generate_pr_content(
            current_branch="feat/single-item",
            commits=changes["commits"],
            diff_stat=changes["stat"],
            base_branch="main",
            commit_titles=changes["commit_titles"],
        )
        self.assertEqual(title, "feat: add item implementation")
        self.assertIn("## Summary", body)
        self.assertIn("feat: add item implementation", body)
        self.assertIn("### Changes Overview", body)
        self.assertIn("item.py", body)

    def test_multi_commit_formatted_title_and_body(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/multi-item-feature"], cwd=self.repo_dir)
        f1 = Path(self.repo_dir) / "step1.txt"
        f1.write_text("1")
        self.run_git(["add", "step1.txt"])
        self.run_git(["commit", "-m", "feat: step one"])

        f2 = Path(self.repo_dir) / "step2.txt"
        f2.write_text("2")
        self.run_git(["add", "step2.txt"])
        self.run_git(["commit", "-m", "feat: step two"])

        changes = get_branch_changes(self.repo_dir, "main", env=env)
        self.assertEqual(changes["commit_count"], 2)

        title, body = generate_pr_content(
            current_branch="feat/multi-item-feature",
            commits=changes["commits"],
            diff_stat=changes["stat"],
            base_branch="main",
            commit_titles=changes["commit_titles"],
        )
        self.assertEqual(title, "feat: multi item feature")
        self.assertIn("- feat: step one", body)
        self.assertIn("- feat: step two", body)

    def test_custom_title_and_body_override(self):
        title, body = generate_pr_content(
            current_branch="feat/test",
            commits=["commit 1"],
            diff_stat="1 file changed",
            base_branch="main",
            custom_title="Custom PR Title",
            custom_body="Custom PR Description",
        )
        self.assertEqual(title, "Custom PR Title")
        self.assertEqual(body, "Custom PR Description")

    # -------------------------------------------------------------------------
    # Unit Tests: push_branch_if_needed
    # -------------------------------------------------------------------------
    def test_push_branch_if_needed(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/push-test"], cwd=self.repo_dir)
        (Path(self.repo_dir) / "push.txt").write_text("content")
        self.run_git(["add", "push.txt"])
        self.run_git(["commit", "-m", "feat: push test"])

        push_branch_if_needed(self.repo_dir, "feat/push-test", env=env)
        # Verify branch is on remote
        remote_refs = self.run_git(["branch", "-r"], cwd=self.repo_dir)
        self.assertIn("origin/feat/push-test", remote_refs)

    # -------------------------------------------------------------------------
    # Unit Tests: create_pull_request
    # -------------------------------------------------------------------------
    def test_create_pull_request_missing_gh(self):
        # Empty PATH
        env = {"PATH": "/nonexistent"}
        with self.assertRaises(AutoPrError) as ctx:
            create_pull_request(self.repo_dir, "feat/test", "main", "Title", "Body", env=env)
        self.assertIn("GitHub CLI ('gh') is required", str(ctx.exception))

    def test_create_pull_request_unauthenticated_gh(self):
        self.create_mock_gh(authenticated=False)
        env = self.get_test_env()
        with self.assertRaises(AutoPrError) as ctx:
            create_pull_request(self.repo_dir, "feat/test", "main", "Title", "Body", env=env)
        self.assertIn("GitHub CLI is not logged in", str(ctx.exception))

    def test_create_pull_request_existing_pr(self):
        self.create_mock_gh(authenticated=True, existing_pr="https://github.com/example/repo/pull/99")
        env = self.get_test_env()
        stderr_buf = io.StringIO()
        with mock.patch("sys.stderr", stderr_buf):
            url = create_pull_request(self.repo_dir, "feat/test", "main", "Title", "Body", env=env)
        self.assertEqual(url, "https://github.com/example/repo/pull/99")
        self.assertIn("PR already open", stderr_buf.getvalue())

    def test_create_pull_request_success_with_draft(self):
        self.create_mock_gh(authenticated=True, pr_url="https://github.com/example/repo/pull/101")
        env = self.get_test_env()
        url = create_pull_request(
            self.repo_dir,
            "feat/test",
            "main",
            "My Title",
            "My Body",
            draft=True,
            env=env,
        )
        self.assertEqual(url, "https://github.com/example/repo/pull/101")
        with open(self.mock_gh_log) as f:
            log_data = f.read()
        self.assertIn("--draft", log_data)
        self.assertIn("--base main", log_data)
        self.assertIn("--head feat/test", log_data)

    def test_create_pull_request_dry_run(self):
        env = self.get_test_env()
        stderr_buf = io.StringIO()
        with mock.patch("sys.stderr", stderr_buf):
            url = create_pull_request(
                self.repo_dir,
                "feat/test",
                "main",
                "My Title",
                "My Body",
                draft=False,
                env=env,
                dry_run=True,
            )
        self.assertIn("[dry-run]", stderr_buf.getvalue())
        self.assertIn("pull", url)

    # -------------------------------------------------------------------------
    # Integration Tests: handle_auto_pr
    # -------------------------------------------------------------------------
    def test_handle_auto_pr_full_flow(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/full-flow"], cwd=self.repo_dir)
        (Path(self.repo_dir) / "app.py").write_text("print('hello')\n")
        self.run_git(["add", "app.py"])
        self.run_git(["commit", "-m", "feat: implement app logic"])

        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            err = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                code = handle_auto_pr(
                    profile=self.profile,
                    base_branch="main",
                    repo_path=self.repo_dir,
                    store=self.store,
                )
            self.assertEqual(code, 0)
            self.assertIn("https://github.com/example/repo/pull/42", out.getvalue())
            self.assertIn("Pushing branch 'feat/full-flow' to origin", err.getvalue())
            self.assertIn("Creating pull request into 'main'", err.getvalue())

    def test_handle_auto_pr_with_no_push_flag(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/no-push-branch"], cwd=self.repo_dir)
        (Path(self.repo_dir) / "np.txt").write_text("no push")
        self.run_git(["add", "np.txt"])
        self.run_git(["commit", "-m", "feat: test no-push flag"])

        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            err = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                code = handle_auto_pr(
                    profile=self.profile,
                    base_branch="main",
                    no_push=True,
                    repo_path=self.repo_dir,
                    store=self.store,
                )
            self.assertEqual(code, 0)
            self.assertNotIn("Pushing branch", err.getvalue())

    # -------------------------------------------------------------------------
    # CLI Tests: cli.main with --auto-pr
    # -------------------------------------------------------------------------
    def test_cli_main_profile_auto_pr(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/cli-auto-pr"], cwd=self.repo_dir)
        (Path(self.repo_dir) / "cli_test.txt").write_text("cli pr test")
        self.run_git(["add", "cli_test.txt"])
        self.run_git(["commit", "-m", "feat: cli test pr creation"])

        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            err = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), mock.patch("pathlib.Path.cwd", return_value=Path(self.repo_dir)):
                with mock.patch("agym.cli.ProfileStore", return_value=self.store):
                    code = cli.main(["testprof", "--auto-pr", "-b", "main"])
            self.assertEqual(code, 0)
            self.assertIn("https://github.com/example/repo/pull/42", out.getvalue())

    def test_cli_main_auto_pr_subcommand(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/subcmd-pr"], cwd=self.repo_dir)
        (Path(self.repo_dir) / "sub.txt").write_text("sub")
        self.run_git(["add", "sub.txt"])
        self.run_git(["commit", "-m", "feat: subcommand auto pr test"])

        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            err = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), mock.patch("pathlib.Path.cwd", return_value=Path(self.repo_dir)):
                with mock.patch("agym.cli.ProfileStore", return_value=self.store):
                    code = cli.main(["auto-pr", "testprof", "--base", "main"])
            self.assertEqual(code, 0)
            self.assertIn("https://github.com/example/repo/pull/42", out.getvalue())

    def test_cli_main_auto_pr_custom_title_and_draft(self):
        env = self.get_test_env()
        self.run_git(["checkout", "-b", "feat/custom-title-draft"], cwd=self.repo_dir)
        (Path(self.repo_dir) / "custom.txt").write_text("custom")
        self.run_git(["add", "custom.txt"])
        self.run_git(["commit", "-m", "feat: commit message"])

        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            err = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), mock.patch("pathlib.Path.cwd", return_value=Path(self.repo_dir)):
                with mock.patch("agym.cli.ProfileStore", return_value=self.store):
                    code = cli.main([
                        "testprof",
                        "--auto-pr",
                        "--title", "Overridden PR Title",
                        "--body", "Overridden PR Body",
                        "--draft",
                    ])
            self.assertEqual(code, 0)
            with open(self.mock_gh_body_log) as f:
                body_log = f.read()
            self.assertIn("Overridden PR Title", body_log)
            self.assertIn("Overridden PR Body", body_log)
            self.assertIn("--draft", body_log)

    def test_cli_main_auto_pr_branch_targeting_itself_error(self):
        # On main targeting main
        env = self.get_test_env()
        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            err = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), mock.patch("pathlib.Path.cwd", return_value=Path(self.repo_dir)):
                with mock.patch("agym.cli.ProfileStore", return_value=self.store):
                    code = cli.main(["testprof", "--auto-pr", "-b", "main"])
            self.assertEqual(code, 1)
            self.assertIn("cannot target itself as base", err.getvalue())

    def test_cli_passthrough_not_triggered_by_double_dash(self):
        # agym <profile> -- --auto-pr should pass --auto-pr to agy launcher
        with mock.patch("agym.cli.resolve_agy") as mock_agy, mock.patch("agym.cli.run_agy") as mock_run:
            mock_agy.return_value = Path("/usr/bin/agy")
            mock_run.return_value = 0
            with mock.patch("agym.cli.ProfileStore", return_value=self.store):
                code = cli.main(["testprof", "--", "--auto-pr"])
            self.assertEqual(code, 0)
            mock_run.assert_called_once_with(
                Path("/usr/bin/agy"),
                self.profile,
                ["--", "--auto-pr"],
                replace_process=True,
            )


if __name__ == "__main__":
    unittest.main()
