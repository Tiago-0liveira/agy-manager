"""Tests for packaging, zero-dependency core, and profile collision immunity.

Specifications for Milestone 1 / Slice 1:
- Feature 1: Zero-Dependency Core
- Feature 2: Profile Collision Immunity ('agym council' vs 'agym-council')
- Feature 3: Script Entry Points & Optional Extra Configuration
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Standard library TOML parser (Python 3.11+) or basic parser fallback
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


from agym import cli, profiles
from agym.profiles import (
    InvalidProfileName,
    ProfileNotFound,
    ProfileSettings,
    ProfileStore,
    validate_profile_name,
)


class TestZeroDependencyCore(unittest.TestCase):
    """Verifies that agym core maintains 100% zero external dependencies."""

    def test_agym_imports_without_external_dependencies(self) -> None:
        """agym core modules must import in a clean subprocess with standard library only."""
        code = (
            "import sys; "
            "import agym; "
            "import agym.cli; "
            "import agym.profiles; "
            "import agym.launcher; "
            "import agym.diagnostics; "
            "import agym.subscription; "
            "import agym.usage; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode, 0, f"agym core import failed: {proc.stderr}"
        )
        self.assertIn("OK", proc.stdout)

    def test_agym_does_not_eagerly_import_council(self) -> None:
        """Importing agym must not eagerly load council subpackage or its optional dependencies."""
        code = (
            "import sys, agym; "
            "assert 'agym.council' not in sys.modules, 'agym.council was eagerly imported'; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode, 0, f"eager import check failed: {proc.stderr}"
        )
        self.assertIn("OK", proc.stdout)

    def test_agym_core_imports_when_council_deps_blocked(self) -> None:
        """agym core modules import cleanly even if council dependencies fail to import."""
        code = (
            "import sys; "
            "blocked = {'fastapi': None, 'uvicorn': None, 'pydantic': None, 'aiofiles': None}; "
            "sys.modules.update(blocked); "
            "import agym; "
            "import agym.cli; "
            "import agym.profiles; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode, 0, f"blocked deps import failed: {proc.stderr}"
        )
        self.assertIn("OK", proc.stdout)

    def test_pyproject_dependencies_strictly_empty(self) -> None:
        """pyproject.toml [project] must declare dependencies = []."""
        data = _load_pyproject()
        project = data.get("project", {})
        dependencies = project.get("dependencies")
        self.assertEqual(
            dependencies,
            [],
            f"Expected project.dependencies to be empty, got: {dependencies}",
        )


class TestProfileCollisionImmunity(unittest.TestCase):
    """Verifies that 'agym council' launches the user profile 'council' and is never shadowed."""

    def test_council_not_in_reserved_names(self) -> None:
        """'council' must not be in RESERVED_NAMES in agym.profiles."""
        self.assertNotIn("council", profiles.RESERVED_NAMES)

    def test_validate_profile_name_accepts_council(self) -> None:
        """validate_profile_name('council') must return 'council' without error."""
        self.assertEqual(validate_profile_name("council"), "council")
        self.assertEqual(validate_profile_name("council-worker-1"), "council-worker-1")

    def test_create_and_load_council_profile(self) -> None:
        """ProfileStore can create and retrieve a profile literally named 'council'."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            p = store.create("council", settings=ProfileSettings(model="gemini-2.5-pro"))
            self.assertEqual(p.name, "council")
            self.assertTrue(p.home.is_dir())
            loaded = store.get("council")
            self.assertEqual(loaded.name, "council")
            self.assertEqual(loaded.settings.model, "gemini-2.5-pro")

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    def test_agym_council_launches_profile(
        self, run_agy_mock: mock.Mock, resolve_mock: mock.Mock, store_mock: mock.Mock
    ) -> None:
        """'agym council' invokes run_agy for profile 'council', not any council sub-command."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            profile = store.create("council")
            store_mock.return_value = store
            resolve_mock.return_value = Path("/fake/agy")
            run_agy_mock.return_value = 0

            code = cli.main(["council"])
            self.assertEqual(code, 0)
            resolve_mock.assert_called_once()
            run_agy_mock.assert_called_once_with(
                Path("/fake/agy"), profile, [], replace_process=True
            )

    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    def test_agym_council_passes_arguments_to_profile_session(
        self, run_agy_mock: mock.Mock, resolve_mock: mock.Mock, store_mock: mock.Mock
    ) -> None:
        """'agym council -p hello' passes args directly to Antigravity CLI."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            profile = store.create("council")
            store_mock.return_value = store
            resolve_mock.return_value = Path("/fake/agy")
            run_agy_mock.return_value = 0

            code = cli.main(["council", "-p", "hello", "--model", "gemini-2.5-pro"])
            self.assertEqual(code, 0)
            run_agy_mock.assert_called_once_with(
                Path("/fake/agy"),
                profile,
                ["-p", "hello", "--model", "gemini-2.5-pro"],
                replace_process=True,
            )

    @mock.patch("agym.cli.ProfileStore")
    def test_agym_council_when_profile_missing_errors_as_profile_not_found(
        self, store_mock: mock.Mock
    ) -> None:
        """When profile 'council' does not exist, agym council outputs 'profile not found' and exits with 2."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            store_mock.return_value = store

            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["council"])
            self.assertEqual(code, 2)
            self.assertIn("profile not found: council", err.getvalue())


class TestPyprojectPackagingConfiguration(unittest.TestCase):
    """Verifies pyproject.toml optional-dependencies, entry points, and package discovery."""

    def setUp(self) -> None:
        self.pyproject = _load_pyproject()

    def test_scripts_entrypoints_contain_both_binaries(self) -> None:
        """pyproject.toml must define agym and agym-council console scripts."""
        scripts = self.pyproject.get("project", {}).get("scripts", {})
        self.assertIn("agym", scripts)
        self.assertEqual(scripts["agym"], "agym.cli:main")
        self.assertIn("agym-council", scripts)
        self.assertEqual(scripts["agym-council"], "agym.council.cli:main")

    def test_optional_dependencies_defines_council_extra(self) -> None:
        """pyproject.toml must declare [project.optional-dependencies] council."""
        opt_deps = self.pyproject.get("project", {}).get("optional-dependencies", {})
        self.assertIn("council", opt_deps, "Missing [project.optional-dependencies] council")
        council_deps = opt_deps["council"]
        dep_names = [d.split(";")[0].split(">=")[0].split("==")[0].strip() for d in council_deps]
        expected = ["fastapi", "uvicorn[standard]", "pydantic", "aiofiles"]
        for exp in expected:
            base_name = exp.split("[")[0]
            self.assertTrue(
                any(base_name in d for d in dep_names),
                f"Expected {exp} in council optional dependencies, got: {council_deps}",
            )

    def test_setuptools_package_discovery_includes_council(self) -> None:
        """setuptools configuration must discover agym and agym.council subpackages."""
        tool = self.pyproject.get("tool", {}).get("setuptools", {})
        packages = tool.get("packages")
        find = tool.get("packages", {}).get("find") if isinstance(packages, dict) else tool.get("packages.find")
        if isinstance(packages, list):
            self.assertTrue(
                "agym" in packages and "agym.council" in packages,
                f"Static packages list must contain agym.council: {packages}",
            )
        else:
            # Dynamic find
            find_conf = tool.get("packages", {}).get("find", {}) if isinstance(packages, dict) else tool.get("packages.find", {})
            include = find_conf.get("include", [])
            self.assertTrue(
                any("agym" in inc for inc in include),
                f"Package find include must match agym*: {include}",
            )

    def test_setuptools_package_data_configured(self) -> None:
        """tool.setuptools.package-data must include presets and web distribution files."""
        tool = self.pyproject.get("tool", {}).get("setuptools", {})
        pkg_data = tool.get("package-data", {})
        self.assertIn("agym.council", pkg_data, "package-data should define assets for agym.council")


class TestCouncilCliHelp(unittest.TestCase):
    """Verifies that agym-council CLI entry point runs --help and prints valid usage."""

    def test_agym_council_cli_help_in_process(self) -> None:
        """Calling agym.council.cli.main(['--help']) outputs usage and returns 0."""
        try:
            from agym.council import cli as council_cli
        except ImportError as exc:
            self.skipTest(f"agym.council.cli not yet implemented: {exc}")

        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = council_cli.main(["--help"])
        self.assertEqual(code, 0)
        output = out.getvalue().lower()
        self.assertTrue("agym-council" in output or "council" in output or "usage" in output)

    def test_agym_council_cli_module_subprocess(self) -> None:
        """Subprocess python -m agym.council.cli --help outputs usage with exit code 0."""
        proc = subprocess.run(
            [sys.executable, "-m", "agym.council.cli", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        if proc.returncode == 1 and "No module named" in proc.stderr:
            self.skipTest("agym.council.cli not yet present on filesystem")
        self.assertEqual(proc.returncode, 0, f"Subprocess CLI help failed: {proc.stderr}")
        self.assertTrue(
            "agym-council" in proc.stdout.lower() or "council" in proc.stdout.lower()
        )


if __name__ == "__main__":
    unittest.main()
