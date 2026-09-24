from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym import cli
from agym.profiles import (
    InvalidProfileName,
    ProfileError,
    ProfileStore,
    validate_profile_name,
)


class TestSmartRename(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_root = Path(self.tmp_dir.name) / "config"
        self.data_root = Path(self.tmp_dir.name) / "data"
        self.store = ProfileStore(self.config_root, self.data_root)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_smartrename_reserved_profile_name(self) -> None:
        with self.assertRaises(InvalidProfileName):
            validate_profile_name("smartrename")

    def test_smartrename_no_accounts(self) -> None:
        with mock.patch("agym.cli.ProfileStore", return_value=self.store):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["smartrename", "num"])
            self.assertEqual(code, 0)
            self.assertIn("No accounts found to rename.", out.getvalue())

    def test_smartrename_invalid_arguments(self) -> None:
        with mock.patch("agym.cli.ProfileStore", return_value=self.store):
            # Missing argument
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["smartrename"])
            self.assertEqual(code, 2)
            self.assertIn("Usage: smartrename <num|letter>", err.getvalue())
            self.assertIn("Error: Invalid argument ''. Expected 'num' or 'letter'.", err.getvalue())

            # Empty string argument
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["smartrename", ""])
            self.assertEqual(code, 2)
            self.assertIn("Usage: smartrename <num|letter>", err.getvalue())
            self.assertIn("Error: Invalid argument ''. Expected 'num' or 'letter'.", err.getvalue())

            # Invalid argument 'foo'
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["smartrename", "foo"])
            self.assertEqual(code, 2)
            self.assertIn("Usage: smartrename <num|letter>", err.getvalue())
            self.assertIn("Error: Invalid argument 'foo'. Expected 'num' or 'letter'.", err.getvalue())

            # Extra flags
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli.main(["smartrename", "num", "--extra"])
            self.assertEqual(code, 2)
            self.assertIn("Usage: smartrename <num|letter>", err.getvalue())
            self.assertIn("Error: Invalid argument 'num --extra'. Expected 'num' or 'letter'.", err.getvalue())

    def test_smartrename_help(self) -> None:
        with mock.patch("agym.cli.ProfileStore", return_value=self.store):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["smartrename", "--help"])
            self.assertEqual(code, 0)
            self.assertIn("Usage: smartrename <num|letter>", out.getvalue())
            self.assertIn("Sequentially rename all accounts", out.getvalue())

    def test_smartrename_num(self) -> None:
        self.store.create("charlie")
        self.store.create("alpha")
        self.store.create("bravo")

        with mock.patch("agym.cli.ProfileStore", return_value=self.store):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["smartrename", "num"])
            self.assertEqual(code, 0)
            output = out.getvalue()
            self.assertIn("Renamed 3 accounts using 'num' sequence:", output)
            self.assertIn("[1/3] charlie -> 1", output)
            self.assertIn("[2/3] alpha -> 2", output)
            self.assertIn("[3/3] bravo -> 3", output)

        profiles = self.store.list()
        # Profiles in store are listed sorted by name
        names = [p.name for p in profiles]
        self.assertEqual(sorted(names), ["1", "2", "3"])

    def test_smartrename_letter_with_30_accounts(self) -> None:
        for i in range(30):
            self.store.create(f"user_{i:02d}")

        with mock.patch("agym.cli.ProfileStore", return_value=self.store):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["smartrename", "letter"])
            self.assertEqual(code, 0)
            output = out.getvalue()
            self.assertIn("Renamed 30 accounts using 'letter' sequence:", output)
            # Accounts 26 (index 25) -> 'Z', 27 (index 26) -> 'Aa'
            self.assertIn("[26/30] user_25 -> Z", output)
            self.assertIn("[27/30] user_26 -> Aa", output)

        self.assertTrue(self.store.exists("Z"))
        self.assertTrue(self.store.exists("Aa"))
        self.assertTrue(self.store.exists("Ab"))
        self.assertTrue(self.store.exists("Ac"))
        self.assertTrue(self.store.exists("Ad"))

    def test_smartrename_case_insensitivity(self) -> None:
        self.store.create("beta")
        self.store.create("alpha")

        with mock.patch("agym.cli.ProfileStore", return_value=self.store):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["smartrename", "NUM"])
            self.assertEqual(code, 0)
            self.assertIn("Renamed 2 accounts using 'num' sequence:", out.getvalue())

        names = [p.name for p in self.store.list()]
        self.assertEqual(sorted(names), ["1", "2"])

    def test_smartrename_collision_handling(self) -> None:
        # Create an account already named '1' and an account 'foo'
        self.store.create("1")
        self.store.create("foo")

        # In 'num' mode, accounts will be renamed to '1' and '2'.
        # Because '1' already exists, a naive rename of 'foo' -> '1' would collide.
        # Two-phase rename must succeed without collision.
        with mock.patch("agym.cli.ProfileStore", return_value=self.store):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.main(["smartrename", "num"])
            self.assertEqual(code, 0)

        names = [p.name for p in self.store.list()]
        self.assertEqual(sorted(names), ["1", "2"])

    def test_smartrename_collision_inverted(self) -> None:
        # Account '1' and '2' where order could swap
        p2 = self.store.create("2")
        p1 = self.store.create("1")

        # p2 created first, so p2 -> 1, p1 -> 2
        results = self.store.smart_rename("num")
        self.assertEqual(results, [("2", "1"), ("1", "2")])
        self.assertTrue(self.store.exists("1"))
        self.assertTrue(self.store.exists("2"))

    def test_smart_rename_accounts_alias(self) -> None:
        self.store.create("first")
        results = self.store.smart_rename_accounts("letter")
        self.assertEqual(results, [("first", "A")])
        self.assertTrue(self.store.exists("A"))

    def test_smart_rename_preserves_profile_data_and_settings(self) -> None:
        p = self.store.create("my_profile")
        # Put sample file in home
        sample_file = p.home / "data.txt"
        sample_file.write_text("important data", encoding="utf-8")

        self.store.smart_rename("num")
        new_p = self.store.get("1")
        self.assertEqual(new_p.name, "1")
        self.assertTrue((new_p.home / "data.txt").is_file())
        self.assertEqual((new_p.home / "data.txt").read_text(encoding="utf-8"), "important data")

    def test_smart_rename_staging_failure_rolls_back(self) -> None:
        self.store.create("acc1")
        self.store.create("acc2")

        original_rename = self.store.rename

        call_count = 0

        def flaky_rename(old: str, new: str):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("simulated disk error during staging")
            return original_rename(old, new)

        with mock.patch.object(self.store, "rename", side_effect=flaky_rename):
            with self.assertRaises(ProfileError) as ctx:
                self.store.smart_rename("num")
            self.assertIn("staging phase", str(ctx.exception))

        # Check that original names are restored
        self.assertTrue(self.store.exists("acc1"))
        self.assertTrue(self.store.exists("acc2"))


if __name__ == "__main__":
    unittest.main()
