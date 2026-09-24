from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.profiles import (
    InvalidProfileName,
    ProfileError,
    ProfileExists,
    ProfileNotFound,
    ProfileSettings,
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
        for bad in [
            "../foo",
            "/foo",
            "foo/bar",
            ".",
            "..",
            "",
            " space",
            "x" * 65,
            "setup",
            "list",
            "remove",
            "doctor",
            "config",
        ]:
            with self.subTest(bad=bad), self.assertRaises(InvalidProfileName):
                validate_profile_name(bad)

    def test_create_duplicate_list_remove(self) -> None:
        p = self.store.create("personal")
        self.assertTrue(p.home.is_dir())
        with self.assertRaises(ProfileExists):
            self.store.create("personal")
        self.store.create("work")
        self.assertEqual([p.name for p in self.store.list()], ["personal", "work"])
        removed = self.store.remove("personal")
        self.assertFalse(removed.exists())
        with self.assertRaises(ProfileNotFound):
            self.store.get("personal")

    def test_create_does_not_store_agy_version(self) -> None:
        self.store.create("personal")
        raw = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("agy_version", raw["profiles"]["personal"])

    def test_metadata_contains_no_credential_material(self) -> None:
        self.store.create("personal")
        raw = self.store.config_path.read_text(encoding="utf-8")
        lowered = raw.lower()
        for forbidden in ["access_token", "refresh_token", "authorization_code", "password"]:
            self.assertNotIn(forbidden, lowered)

    @unittest.skipIf(os.name == "nt", "POSIX mode check")
    def test_private_directory_modes(self) -> None:
        p = self.store.create("personal")
        self.assertEqual(p.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.profile_dir("personal").stat().st_mode & 0o777, 0o700)

    def test_legacy_profile_metadata_loading(self) -> None:
        legacy_data = {
            "version": 1,
            "profiles": {
                "oldprof": {
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "home": str((Path(self.tmp.name) / "data" / "profiles" / "oldprof" / "home").resolve()),
                    "agy_version": "1.0.0",
                }
            },
        }
        self.store.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.config_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        p = self.store.get("oldprof")
        self.assertIsNone(p.settings.model)
        self.assertFalse(p.settings.dangerously_skip_permissions)
        self.assertEqual(p.settings.validation_errors, ())

    def test_reserved_profile_names(self) -> None:
        for reserved in ["setup", "list", "remove", "doctor", "usage", "edit", "help", "rotate", "rename", "mv", "smartrename"]:
            with self.subTest(reserved=reserved), self.assertRaises(InvalidProfileName):
                validate_profile_name(reserved)

    def test_subscription_date_persistence_and_editing(self) -> None:
        # Create with subscription date
        p1 = self.store.create("with-date", subscription_date="2027-03-14")
        self.assertEqual(p1.subscription_date, "2027-03-14")
        self.assertEqual(self.store.get("with-date").subscription_date, "2027-03-14")

        # Create without subscription date
        p2 = self.store.create("no-date")
        self.assertIsNone(p2.subscription_date)
        self.assertIsNone(self.store.get("no-date").subscription_date)

        # Update / add date to profile that had none
        updated = self.store.set_subscription_date("no-date", "2026-12-01")
        self.assertEqual(updated.subscription_date, "2026-12-01")
        self.assertEqual(self.store.get("no-date").subscription_date, "2026-12-01")

        # Edit existing date
        updated2 = self.store.set_subscription_date("with-date", "2028-01-01")
        self.assertEqual(updated2.subscription_date, "2028-01-01")
        self.assertEqual(self.store.get("with-date").subscription_date, "2028-01-01")

        # Clear date back to None
        cleared = self.store.set_subscription_date("with-date", None)
        self.assertIsNone(cleared.subscription_date)
        self.assertIsNone(self.store.get("with-date").subscription_date)

    def test_legacy_profile_config_compatibility(self) -> None:
        # Simulate an older config.json written before this feature
        legacy_data = {
            "version": 1,
            "profiles": {
                "legacy-account": {
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "home": str(self.store.profiles_root / "legacy-account" / "home"),
                    "agy_version": "agy 1.2.0",
                }
            },
        }
        self.store.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.config_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        p = self.store.get("legacy-account")
        self.assertEqual(p.name, "legacy-account")
        self.assertIsNone(p.subscription_date)

        all_profiles = self.store.list()
        self.assertEqual(len(all_profiles), 1)
        self.assertIsNone(all_profiles[0].subscription_date)

    def test_explicit_settings_loading(self) -> None:
        data = {
            "version": 1,
            "profiles": {
                "custom": {
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "home": str((Path(self.tmp.name) / "data" / "profiles" / "custom" / "home").resolve()),
                    "agy_version": "1.0.0",
                    "settings": {
                        "model": "gemini-ultra",
                        "dangerously_skip_permissions": True,
                    },
                }
            },
        }
        self.store.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.config_path.write_text(json.dumps(data), encoding="utf-8")

        p = self.store.get("custom")
        self.assertEqual(p.settings.model, "gemini-ultra")
        self.assertTrue(p.settings.dangerously_skip_permissions)
        self.assertEqual(p.settings.validation_errors, ())

    def test_malformed_settings_loading(self) -> None:
        # Non-dict settings
        s_bad_dict = ProfileSettings.from_dict("invalid")
        self.assertIn("settings must be a dictionary", s_bad_dict.validation_errors)

        # Invalid model type
        s_bad_model = ProfileSettings.from_dict({"model": 12345, "dangerously_skip_permissions": False})
        self.assertIn("model must be null or a string", s_bad_model.validation_errors)

        # Invalid dangerously_skip_permissions type
        s_bad_perm = ProfileSettings.from_dict({"model": "valid", "dangerously_skip_permissions": "yes"})
        self.assertIn("dangerously_skip_permissions must be a boolean", s_bad_perm.validation_errors)

    def test_settings_aliases(self) -> None:
        for key in ["dsp", "skip_perms", "dangerously_skip_permission"]:
            s = ProfileSettings.from_dict({"model": "test", key: True})
            self.assertTrue(s.dangerously_skip_permissions, f"Failed for {key}")
            self.assertEqual(s.validation_errors, ())

            s_invalid = ProfileSettings.from_dict({"model": "test", key: "invalid"})
            self.assertFalse(s_invalid.dangerously_skip_permissions)
            self.assertIn("dangerously_skip_permissions must be a boolean", s_invalid.validation_errors)

    def test_update_settings_persistence(self) -> None:
        p = self.store.create("personal")
        self.assertIsNone(p.settings.model)
        self.assertFalse(p.settings.dangerously_skip_permissions)

        updated = self.store.update_settings(
            "personal",
            ProfileSettings(model="gemini-pro", dangerously_skip_permissions=True),
        )
        self.assertEqual(updated.settings.model, "gemini-pro")
        self.assertTrue(updated.settings.dangerously_skip_permissions)

        # Re-load from disk to verify persistence
        reloaded = self.store.get("personal")
        self.assertEqual(reloaded.settings.model, "gemini-pro")
        self.assertTrue(reloaded.settings.dangerously_skip_permissions)
        self.assertEqual(reloaded.home, p.home)
        self.assertEqual(reloaded.created_at, p.created_at)

    def test_rename_profile_success(self) -> None:
        p = self.store.create("personal")
        self.store.update_settings(
            "personal",
            ProfileSettings(model="gemini-2.5-flash", dangerously_skip_permissions=True),
        )
        self.store.set_subscription_date("personal", "2027-03-14")

        # Create dummy file inside profile home
        dummy_dir = p.home / ".gemini"
        dummy_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = dummy_dir / "oauth_sample.txt"
        dummy_file.write_text("sample-data", encoding="utf-8")

        renamed = self.store.rename("personal", "AI1")

        self.assertEqual(renamed.name, "AI1")
        self.assertEqual(renamed.home, (self.store.profiles_root / "AI1" / "home").resolve())
        self.assertEqual(renamed.settings.model, "gemini-2.5-flash")
        self.assertTrue(renamed.settings.dangerously_skip_permissions)
        self.assertEqual(renamed.subscription_date, "2027-03-14")

        # Verify old profile is gone
        with self.assertRaises(ProfileNotFound):
            self.store.get("personal")
        self.assertNotIn("personal", [prof.name for prof in self.store.list()])
        self.assertIn("AI1", [prof.name for prof in self.store.list()])

        # Verify files on disk
        self.assertFalse((self.store.profiles_root / "personal").exists())
        self.assertTrue((self.store.profiles_root / "AI1").exists())
        self.assertTrue((renamed.home / ".gemini" / "oauth_sample.txt").exists())
        self.assertEqual(
            (renamed.home / ".gemini" / "oauth_sample.txt").read_text(encoding="utf-8"),
            "sample-data",
        )

        # Verify new store instance reads config correctly
        reloaded_store = ProfileStore(self.store.config_root, self.store.data_root)
        reloaded_p = reloaded_store.get("AI1")
        self.assertEqual(reloaded_p.name, "AI1")
        self.assertEqual(reloaded_p.home, renamed.home)
        self.assertEqual(reloaded_p.settings.model, "gemini-2.5-flash")
        self.assertTrue(reloaded_p.settings.dangerously_skip_permissions)
        self.assertEqual(reloaded_p.subscription_date, "2027-03-14")

    def test_rename_nonexistent_profile(self) -> None:
        with self.assertRaises(ProfileNotFound):
            self.store.rename("does-not-exist", "new-name")

    def test_rename_to_existing_profile(self) -> None:
        self.store.create("prof1")
        self.store.create("prof2")
        with self.assertRaises(ProfileExists):
            self.store.rename("prof1", "prof2")

    def test_rename_to_same_name(self) -> None:
        self.store.create("prof1")
        with self.assertRaises(ProfileError):
            self.store.rename("prof1", "prof1")

    def test_rename_invalid_names(self) -> None:
        self.store.create("prof1")
        for bad in ["bad name", "setup", "list", "rename", "mv", "smartrename", "..", ""]:
            with self.subTest(bad=bad), self.assertRaises(InvalidProfileName):
                self.store.rename("prof1", bad)

    def test_rename_target_dir_already_exists_on_disk(self) -> None:
        self.store.create("prof1")
        target_dir = self.store.profiles_root / "prof2"
        target_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ProfileExists):
            self.store.rename("prof1", "prof2")

    def test_rename_when_source_dir_missing(self) -> None:
        p = self.store.create("prof1")
        shutil.rmtree(self.store.profile_dir("prof1"))
        renamed = self.store.rename("prof1", "prof2")
        self.assertEqual(renamed.name, "prof2")
        self.assertTrue(renamed.home.is_dir())

    def test_rename_rollback_on_save_failure(self) -> None:
        p = self.store.create("prof1")
        test_file = p.home / "test.txt"
        test_file.write_text("important", encoding="utf-8")

        with mock.patch.object(self.store, "_save", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.store.rename("prof1", "prof2")

        # Verify old dir is preserved and restored
        self.assertTrue(self.store.profile_dir("prof1").exists())
        self.assertTrue((p.home / "test.txt").exists())
        self.assertEqual((p.home / "test.txt").read_text(encoding="utf-8"), "important")
        self.assertFalse(self.store.profile_dir("prof2").exists())
        # Profile in store is still prof1
        self.assertEqual(self.store.get("prof1").name, "prof1")

    def test_rename_migrates_rotation_state(self) -> None:
        self.store.create("acc1")
        rot_file = self.store.data_root / "rotation_state.json"
        rot_file.write_text(
            json.dumps({"index": 1, "last_account": "acc1", "history": ["acc0", "acc1"]}),
            encoding="utf-8",
        )

        self.store.rename("acc1", "acc_renamed")

        data = json.loads(rot_file.read_text(encoding="utf-8"))
        self.assertEqual(data["last_account"], "acc_renamed")
        self.assertEqual(data["history"], ["acc0", "acc_renamed"])

