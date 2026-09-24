from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.maccred import (
    KEYCHAIN_NAME,
    MAC_TARGET_NAME,
    OAUTH_TOKEN_FILENAME,
    get_profile_keychain_path,
    is_darwin_platform,
    maccred_delete,
    maccred_read,
    maccred_write,
    setup_profile_keychain,
    sync_mac_credentials_after_launch,
    sync_mac_credentials_before_launch,
)
from agym.wincred import (
    DEFAULT_USER,
    get_profile_email,
    has_profile_token,
    profile_credential_context,
)


class MaccredUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name).resolve()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_is_darwin_platform(self) -> None:
        with mock.patch("platform.system", return_value="Darwin"):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGYM_DISABLE_MACCRED", None)
                self.assertTrue(is_darwin_platform())

            with mock.patch.dict(os.environ, {"AGYM_DISABLE_MACCRED": "1"}):
                self.assertFalse(is_darwin_platform())

        with mock.patch("platform.system", return_value="Linux"):
            self.assertFalse(is_darwin_platform())

    def test_get_profile_keychain_path(self) -> None:
        kc_path = get_profile_keychain_path(self.home)
        self.assertEqual(kc_path, self.home / "Library" / "Keychains" / KEYCHAIN_NAME)

    @mock.patch("agym.maccred.is_darwin_platform", return_value=True)
    @mock.patch("agym.maccred._run_security_cmd")
    def test_setup_profile_keychain_creates_new(self, mock_cmd: mock.Mock, _mock_plat: mock.Mock) -> None:
        mock_cmd.return_value = mock.Mock(returncode=0)
        kc_path = setup_profile_keychain(self.home, is_setup=False)
        self.assertIsNotNone(kc_path)
        self.assertTrue((self.home / "Library" / "Keychains").is_dir())
        self.assertGreaterEqual(mock_cmd.call_count, 4)

    @mock.patch("agym.maccred.is_darwin_platform", return_value=True)
    @mock.patch("agym.maccred._run_security_cmd")
    def test_maccred_read_and_write(self, mock_cmd: mock.Mock, _mock_plat: mock.Mock) -> None:
        # Test write
        mock_cmd.return_value = mock.Mock(returncode=0)
        written = maccred_write(self.home, MAC_TARGET_NAME, "antigravity", b"secret_blob")
        self.assertTrue(written)

        # Test read
        mock_cmd.return_value = mock.Mock(returncode=0, stdout="secret_blob\n")
        res = maccred_read(self.home, MAC_TARGET_NAME)
        self.assertIsNotNone(res)
        user, blob = res  # type: ignore
        self.assertEqual(user, DEFAULT_USER)
        self.assertEqual(blob, b"secret_blob")

    @mock.patch("agym.maccred.is_darwin_platform", return_value=True)
    @mock.patch("agym.maccred._run_security_cmd")
    def test_maccred_delete(self, mock_cmd: mock.Mock, _mock_plat: mock.Mock) -> None:
        mock_cmd.return_value = mock.Mock(returncode=0)
        self.assertTrue(maccred_delete(self.home, MAC_TARGET_NAME))

        mock_cmd.return_value = mock.Mock(returncode=44)  # item not found
        self.assertTrue(maccred_delete(self.home, MAC_TARGET_NAME))

        mock_cmd.return_value = mock.Mock(returncode=1)
        self.assertFalse(maccred_delete(self.home, MAC_TARGET_NAME))

    @mock.patch("agym.maccred.is_darwin_platform", return_value=True)
    @mock.patch("agym.maccred.setup_profile_keychain")
    @mock.patch("agym.maccred.maccred_delete")
    def test_sync_before_launch_setup_purges(
        self,
        mock_delete: mock.Mock,
        mock_setup: mock.Mock,
        _mock_plat: mock.Mock,
    ) -> None:
        oauth_file = self.home / ".gemini" / "antigravity-cli" / OAUTH_TOKEN_FILENAME
        oauth_file.parent.mkdir(parents=True)
        oauth_file.write_text("dummy", encoding="utf-8")

        sync_mac_credentials_before_launch(self.home, is_setup=True)
        mock_delete.assert_called_once_with(self.home, MAC_TARGET_NAME)
        self.assertFalse(oauth_file.exists())

    @mock.patch("agym.maccred.is_darwin_platform", return_value=True)
    @mock.patch("agym.maccred.setup_profile_keychain")
    @mock.patch("agym.maccred.maccred_read", return_value=None)
    @mock.patch("agym.maccred.maccred_write")
    def test_sync_before_launch_prepopulates_from_oauth_file(
        self,
        mock_write: mock.Mock,
        _mock_read: mock.Mock,
        _mock_setup: mock.Mock,
        _mock_plat: mock.Mock,
    ) -> None:
        oauth_file = self.home / ".gemini" / "antigravity-cli" / OAUTH_TOKEN_FILENAME
        oauth_file.parent.mkdir(parents=True)
        oauth_file.write_text('{"token": "xyz"}', encoding="utf-8")

        sync_mac_credentials_before_launch(self.home, is_setup=False)
        mock_write.assert_called_once_with(self.home, MAC_TARGET_NAME, DEFAULT_USER, '{"token": "xyz"}')

    @mock.patch("agym.maccred.is_darwin_platform", return_value=True)
    @mock.patch("agym.maccred.maccred_read")
    def test_sync_after_launch_persists_to_files(
        self,
        mock_read: mock.Mock,
        _mock_plat: mock.Mock,
    ) -> None:
        token_payload = json.dumps({
            "token": {"access_token": "ya29.test", "refresh_token": "ref.test"},
            "email": "macuser@example.com",
        })
        mock_read.return_value = (DEFAULT_USER, token_payload.encode("utf-8"))

        sync_mac_credentials_after_launch(self.home, is_setup=False)

        # Verify oauth token file written
        oauth_file = self.home / ".gemini" / "antigravity-cli" / OAUTH_TOKEN_FILENAME
        self.assertTrue(oauth_file.is_file())
        with open(oauth_file, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["email"], "macuser@example.com")

        # Verify token.json written and get_profile_email works
        self.assertTrue(has_profile_token(self.home))
        self.assertEqual(get_profile_email(self.home), "macuser@example.com")

    @mock.patch("agym.wincred.is_windows_platform", return_value=False)
    @mock.patch("agym.maccred.is_darwin_platform", return_value=True)
    @mock.patch("agym.maccred.sync_mac_credentials_before_launch")
    @mock.patch("agym.maccred.sync_mac_credentials_after_launch")
    def test_profile_credential_context_darwin(
        self,
        mock_after: mock.Mock,
        mock_before: mock.Mock,
        _mock_darwin: mock.Mock,
        _mock_win: mock.Mock,
    ) -> None:
        with profile_credential_context(self.home, is_setup=True):
            mock_before.assert_called_once_with(self.home, is_setup=True)
            mock_after.assert_not_called()
        mock_after.assert_called_once_with(self.home, is_setup=True)

    def test_get_profile_email_oauth_token_fallback(self) -> None:
        oauth_file = self.home / ".gemini" / "antigravity-cli" / OAUTH_TOKEN_FILENAME
        oauth_file.parent.mkdir(parents=True)
        oauth_file.write_text(json.dumps({"email": "fallback@example.com"}), encoding="utf-8")
        self.assertEqual(get_profile_email(self.home), "fallback@example.com")
