from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym import cli
from agym.profiles import ProfileStore


class CliTests(unittest.TestCase):
    @mock.patch("agym.cli.ProfileStore")
    @mock.patch("agym.cli.resolve_agy")
    @mock.patch("agym.cli.run_agy")
    def test_profile_launch_resolves_before_launch_env(self, run: mock.Mock, resolve: mock.Mock, Store: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / "config", Path(tmp) / "data")
            profile = store.create("personal")
            Store.return_value = store
            resolve.return_value = Path("/real/agy")
            run.return_value = 0
            self.assertEqual(cli.main(["personal", "-p", "hello"]), 0)
            resolve.assert_called_once_with()
            run.assert_called_once_with(Path("/real/agy"), profile, ["-p", "hello"], replace_process=True)
