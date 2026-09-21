from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.wincred import (
    DEFAULT_USER,
    TARGET_NAME,
    async_profile_credential_context,
    extract_email_from_blob,
    get_profile_email,
    get_profile_token_path,
    get_wincred_async_lock,
    has_profile_token,
    load_profile_token,
    profile_credential_context,
    save_profile_token,
    sync_credentials_after_launch,
    sync_credentials_before_launch,
)


class WincredUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_extract_email_direct_json(self) -> None:
        blob = json.dumps({"email": "alice@example.com"}).encode("utf-8")
        self.assertEqual(extract_email_from_blob(blob), "alice@example.com")

    def test_extract_email_jwt_id_token(self) -> None:
        claims = {"email": "bob@example.com", "name": "Bob"}
        claims_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("utf-8").rstrip("=")
        fake_jwt = f"eyJhbGciOiJSUzI1NiJ9.{claims_b64}.fakesig"
        blob = json.dumps({"id_token": fake_jwt, "token": {"access_token": "ya29.fake"}}).encode("utf-8")
        self.assertEqual(extract_email_from_blob(blob), "bob@example.com")

    def test_extract_email_invalid(self) -> None:
        self.assertIsNone(extract_email_from_blob(b"not json"))
        self.assertIsNone(extract_email_from_blob(b"{}"))

    def test_save_and_load_profile_token(self) -> None:
        blob = json.dumps({"email": "carol@example.com"}).encode("utf-8")
        self.assertFalse(has_profile_token(self.home))

        saved = save_profile_token(self.home, username="user1", blob=blob)
        self.assertTrue(saved)
        self.assertTrue(has_profile_token(self.home))
        self.assertEqual(get_profile_email(self.home), "carol@example.com")

        loaded = load_profile_token(self.home)
        self.assertIsNotNone(loaded)
        user, loaded_blob = loaded  # type: ignore
        self.assertEqual(user, "user1")
        self.assertEqual(loaded_blob, blob)

    @mock.patch("agym.wincred.is_windows_platform", return_value=True)
    @mock.patch("agym.wincred.wincred_delete")
    @mock.patch("agym.wincred.wincred_write")
    def test_sync_before_launch_setup_purges_credential(
        self,
        mock_write: mock.Mock,
        mock_delete: mock.Mock,
        _mock_win: mock.Mock,
    ) -> None:
        # Even if a token file exists, is_setup=True MUST purge to force fresh login
        save_profile_token(self.home, blob=b"dummy")
        sync_credentials_before_launch(self.home, is_setup=True)
        mock_delete.assert_called_once_with(TARGET_NAME)
        mock_write.assert_not_called()

    @mock.patch("agym.wincred.is_windows_platform", return_value=True)
    @mock.patch("agym.wincred.wincred_delete")
    @mock.patch("agym.wincred.wincred_write")
    def test_sync_before_launch_established_profile_injects_token(
        self,
        mock_write: mock.Mock,
        mock_delete: mock.Mock,
        _mock_win: mock.Mock,
    ) -> None:
        blob = json.dumps({"email": "dave@example.com"}).encode("utf-8")
        save_profile_token(self.home, username="myuser", blob=blob)

        sync_credentials_before_launch(self.home, is_setup=False)
        mock_write.assert_called_once_with(TARGET_NAME, "myuser", blob)
        mock_delete.assert_not_called()

    @mock.patch("agym.wincred.is_windows_platform", return_value=True)
    @mock.patch("agym.wincred.wincred_delete")
    @mock.patch("agym.wincred.wincred_write")
    def test_sync_before_launch_no_token_purges_credential(
        self,
        mock_write: mock.Mock,
        mock_delete: mock.Mock,
        _mock_win: mock.Mock,
    ) -> None:
        # Profile without token file
        sync_credentials_before_launch(self.home, is_setup=False)
        mock_delete.assert_called_once_with(TARGET_NAME)
        mock_write.assert_not_called()

    @mock.patch("agym.wincred.is_windows_platform", return_value=True)
    @mock.patch("agym.wincred.wincred_read")
    def test_sync_after_launch_saves_new_credential(
        self,
        mock_read: mock.Mock,
        _mock_win: mock.Mock,
    ) -> None:
        blob = json.dumps({"email": "eve@example.com"}).encode("utf-8")
        mock_read.return_value = ("antigravity", blob)

        sync_credentials_after_launch(self.home)
        self.assertTrue(has_profile_token(self.home))
        self.assertEqual(get_profile_email(self.home), "eve@example.com")

    @mock.patch("agym.wincred.is_windows_platform", return_value=True)
    @mock.patch("agym.wincred.wincred_read")
    def test_sync_after_launch_guards_against_cross_contamination(
        self,
        mock_read: mock.Mock,
        _mock_win: mock.Mock,
    ) -> None:
        alice_blob = json.dumps({"email": "alice@example.com"}).encode("utf-8")
        bob_blob = json.dumps({"email": "bob@example.com"}).encode("utf-8")
        save_profile_token(self.home, blob=alice_blob)
        self.assertEqual(get_profile_email(self.home), "alice@example.com")

        mock_read.return_value = ("antigravity", bob_blob)
        # In regular usage (is_setup=False), cross-account overwrite must be blocked
        sync_credentials_after_launch(self.home, is_setup=False)
        self.assertEqual(get_profile_email(self.home), "alice@example.com")

    @mock.patch("agym.wincred.is_windows_platform", return_value=True)
    @mock.patch("agym.wincred.wincred_read")
    def test_sync_after_launch_allows_different_email_during_setup(
        self,
        mock_read: mock.Mock,
        _mock_win: mock.Mock,
    ) -> None:
        alice_blob = json.dumps({"email": "alice@example.com"}).encode("utf-8")
        bob_blob = json.dumps({"email": "bob@example.com"}).encode("utf-8")
        save_profile_token(self.home, blob=alice_blob)

        mock_read.return_value = ("antigravity", bob_blob)
        # In setup mode (is_setup=True), new identity must be allowed
        sync_credentials_after_launch(self.home, is_setup=True)
        self.assertEqual(get_profile_email(self.home), "bob@example.com")

    @mock.patch("agym.wincred.is_windows_platform", return_value=True)
    @mock.patch("agym.wincred.wincred_read")
    @mock.patch("agym.wincred.wincred_write")
    def test_async_profile_credential_context(
        self,
        mock_write: mock.Mock,
        mock_read: mock.Mock,
        _mock_win: mock.Mock,
    ) -> None:
        blob = json.dumps({"email": "charlie@example.com"}).encode("utf-8")
        mock_read.return_value = ("antigravity", blob)

        async def _run() -> None:
            async with async_profile_credential_context(self.home, is_setup=True):
                pass

        asyncio.run(_run())
        self.assertTrue(has_profile_token(self.home))
        self.assertEqual(get_profile_email(self.home), "charlie@example.com")


if __name__ == "__main__":
    unittest.main()
