from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agym.profiles import (
    InvalidProfileName,
    ProfileExists,
    ProfileNotFound,
    ProfileStore,
    validate_profile_name,
)


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(root / "config", root / "data")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_profile_name_validation(self) -> None:
        for good in ["personal", "work", "client-a", "google2", "a.b_c-1"]:
            self.assertEqual(validate_profile_name(good), good)
        for bad in ["../foo", "/foo", "foo/bar", ".", "..", "", " space", "x" * 65]:
            with self.subTest(bad=bad), self.assertRaises(InvalidProfileName):
                validate_profile_name(bad)

    def test_create_duplicate_list_remove(self) -> None:
        p = self.store.create("personal", "agy 1.2.7")
        self.assertTrue(p.home.is_dir())
        with self.assertRaises(ProfileExists):
            self.store.create("personal")
        self.store.create("work")
        self.assertEqual([p.name for p in self.store.list()], ["personal", "work"])
        removed = self.store.remove("personal")
        self.assertFalse(removed.exists())
        with self.assertRaises(ProfileNotFound):
            self.store.get("personal")

    def test_metadata_contains_no_credential_material(self) -> None:
        self.store.create("personal", "1.0")
        raw = self.store.config_path.read_text(encoding="utf-8")
        lowered = raw.lower()
        for forbidden in ["access_token", "refresh_token", "authorization_code", "password"]:
            self.assertNotIn(forbidden, lowered)

    @unittest.skipIf(os.name == "nt", "POSIX mode check")
    def test_private_directory_modes(self) -> None:
        p = self.store.create("personal")
        self.assertEqual(p.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.profile_dir("personal").stat().st_mode & 0o777, 0o700)
