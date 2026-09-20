from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym import cli
from agym.launcher import build_profile_env, run_auto_prompt
from agym.profiles import ProfileSettings, ProfileStore


class HostSafetyTests(unittest.TestCase):
    def test_setup_remove_store_never_touch_host_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host_gemini = root / "host-home" / ".gemini"
            host_gemini.mkdir(parents=True)
            sentinel = host_gemini / "DO_NOT_TOUCH"
            sentinel.write_text("host-state", encoding="utf-8")

            store = ProfileStore(root / "config", root / "data")
            profile = store.create("personal")
            isolated = profile.home / ".gemini" / "antigravity-cli"
            isolated.mkdir(parents=True)
            (isolated / "state").write_text("profile-state", encoding="utf-8")
            store.remove("personal")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "host-state")
            self.assertTrue(host_gemini.is_dir())

    def test_config_and_autoprompt_never_touch_host_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host_gemini = root / "host-home" / ".gemini"
            host_gemini.mkdir(parents=True)
            sentinel = host_gemini / "DO_NOT_TOUCH"
            sentinel.write_text("host-state", encoding="utf-8")

            store = ProfileStore(root / "config", root / "data")
            profile = store.create("personal")

            # Mutate config
            store.update_settings("personal", ProfileSettings(model="new-model", dangerously_skip_permissions=True))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "host-state")

            # Run auto-prompt mock
            with mock.patch("agym.launcher.run_agy_capture") as mock_cap, mock.patch(
                "agym.launcher.exec_agy_interactive"
            ) as mock_exec:
                mock_cap.return_value = mock.Mock(returncode=0, stdout="plan result", stderr="")
                mock_exec.return_value = 0
                run_auto_prompt(Path("/usr/bin/agy"), profile, "make plan", replace_process=False)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "host-state")
            self.assertEqual(len(list(host_gemini.iterdir())), 1)

    def test_credentials_never_leaked_in_list_doctor_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            profile = store.create("personal")

            # Plant secret in profile credential file
            secret = "SUPER_SECRET_TOKEN_XYZ_12345"
            token_file = profile.home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(f'{{"access_token": "{secret}"}}', encoding="utf-8")

            from agym.launcher import AgyNotFound

            with mock.patch("agym.cli.ProfileStore", return_value=store), mock.patch(
                "agym.diagnostics.resolve_agy", side_effect=AgyNotFound("missing")
            ):

                # Check list output
                out_list = io.StringIO()
                with mock.patch("sys.stdout", out_list):
                    cli.main(["list"])
                self.assertNotIn(secret, out_list.getvalue())

                # Check doctor output
                out_doc = io.StringIO()
                with mock.patch("sys.stdout", out_doc):
                    cli.main(["doctor", "personal"])
                self.assertNotIn(secret, out_doc.getvalue())

                # Check config output
                out_cfg = io.StringIO()
                with mock.patch("sys.stdout", out_cfg):
                    cli.main(["config", "personal"])
                self.assertNotIn(secret, out_cfg.getvalue())

    def test_separate_profiles_produce_separate_isolated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root / "config", root / "data")
            p_a = store.create("personal")
            p_b = store.create("work")

            env_a = build_profile_env(p_a.home, {"HOME": "/host"})
            env_b = build_profile_env(p_b.home, {"HOME": "/host"})

            self.assertNotEqual(env_a["HOME"], env_b["HOME"])
            path_a = Path(env_a["HOME"]) / ".gemini" / "antigravity-cli"
            path_b = Path(env_b["HOME"]) / ".gemini" / "antigravity-cli"
            self.assertNotEqual(path_a, path_b)
