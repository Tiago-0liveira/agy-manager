from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agym.profiles import Profile
from agym.quota_api import (
    CLOUDCODE_HOSTS,
    OAUTH_TOKEN_URL,
    OAuthRefreshError,
    QuotaUnauthorizedError,
    fetch_quota_direct_async,
    get_candidate_oauth_credentials,
    get_oauth_client_credentials,
    get_valid_access_token_async,
    is_token_expired,
    load_profile_token_data,
    normalize_api_quota_response,
    query_quota_api_async,
    refresh_oauth_token_async,
    update_profile_tokens,
)
from agym.usage import DIRECT_QUOTA_WAIT_SECONDS, AccountUsage, fetch_account_usage_async, parse_iso_datetime
from agym.wincred import save_profile_token

SAMPLE_GOOGLE_API_RESPONSE = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "description": "Models within this group: Gemini Flash, Gemini Pro",
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "displayName": "Weekly Limit Remaining",
                    "window": "weekly",
                    "resetTime": "2026-09-27T22:17:44Z",
                    "description": "You have used some of your weekly limit, it will fully refresh in 5 days.",
                    "remainingFraction": 0.625,
                },
                {
                    "bucketId": "gemini-5h",
                    "displayName": "Five Hour Limit Remaining",
                    "window": "5h",
                    "resetTime": "2026-09-22T16:23:37Z",
                    "remainingFraction": 0.95,
                },
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "description": "Models within this group: Claude Opus, Claude Sonnet",
            "buckets": [
                {
                    "bucketId": "3p-weekly",
                    "displayName": "Weekly Limit Remaining",
                    "window": "weekly",
                    "resetTime": "2026-09-29T11:23:37Z",
                    "remainingFraction": 1.0,
                }
            ],
        },
    ]
}


class QuotaApiUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name)
        self.profile = Profile(name="test_user", home=self.home, created_at="2026-01-01T00:00:00Z")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _setup_token_file(
        self,
        access_token: str = "test_access_token",
        refresh_token: str = "test_refresh_token",
        expiry: str | None = "2026-09-22T15:00:00Z",
    ) -> None:
        blob_dict = {
            "token": {
                "access_token": access_token,
                "token_type": "Bearer",
                "refresh_token": refresh_token,
                "expiry": expiry,
            },
            "auth_method": "consumer",
            "id_token": "mock_id_token",
        }
        save_profile_token(
            profile_home=self.home,
            username="antigravity",
            blob=json.dumps(blob_dict),
        )

    def test_normalize_api_quota_response(self) -> None:
        usage, synthetic_json = normalize_api_quota_response(
            SAMPLE_GOOGLE_API_RESPONSE,
            account="test_user",
            subscription_date="2027-01-01",
        )

        self.assertEqual(usage.account, "test_user")
        self.assertEqual(usage.status, "success")
        self.assertEqual(usage.subscription_date, "2027-01-01")
        self.assertEqual(len(usage.groups), 2)

        gemini_group = usage.groups[0]
        self.assertEqual(gemini_group.name, "Gemini Models")
        self.assertEqual(len(gemini_group.buckets), 2)

        weekly_bucket = gemini_group.buckets[0]
        self.assertEqual(weekly_bucket.id, "gemini-weekly")
        self.assertEqual(weekly_bucket.name, "Weekly Limit Remaining")
        self.assertEqual(weekly_bucket.window, "weekly")
        self.assertEqual(weekly_bucket.percentage, 62)
        self.assertAlmostEqual(weekly_bucket.remaining_fraction, 0.625)
        self.assertEqual(weekly_bucket.reset_time_raw, "2026-09-27T22:17:44Z")

        synth = json.loads(synthetic_json)
        self.assertEqual(synth.get("status"), "SUCCESS")
        self.assertEqual(synth.get("command", {}).get("name"), "usage")

    def test_is_token_expired(self) -> None:
        self.assertTrue(is_token_expired(None))
        self.assertTrue(is_token_expired(""))

        # Past time is expired
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.assertTrue(is_token_expired(past))

        # Time within 30s is treated as expired (buffer_seconds=60)
        near_future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        self.assertTrue(is_token_expired(near_future, buffer_seconds=60.0))

        # Time in far future is not expired
        far_future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.assertFalse(is_token_expired(far_future))

    def test_load_and_update_profile_token_data(self) -> None:
        self._setup_token_file(access_token="tok_1", refresh_token="ref_1")
        data = load_profile_token_data(self.home)
        self.assertIsNotNone(data)
        self.assertEqual(data["access_token"], "tok_1")
        self.assertEqual(data["refresh_token"], "ref_1")

        # Update tokens
        new_exp = datetime.now(timezone.utc) + timedelta(hours=1)
        ok = update_profile_tokens(self.home, "tok_2", new_exp, "ref_2")
        self.assertTrue(ok)

        data2 = load_profile_token_data(self.home)
        self.assertEqual(data2["access_token"], "tok_2")
        self.assertEqual(data2["refresh_token"], "ref_2")
        self.assertEqual(data2["expiry"], new_exp.isoformat())

    @mock.patch("agym.quota_api._http_post_sync")
    def test_refresh_oauth_token_async(self, mock_post: mock.Mock) -> None:
        mock_post.return_value = (
            200,
            json.dumps({"access_token": "refreshed_tok", "expires_in": 3600}),
        )

        async def _run() -> None:
            token, expiry = await refresh_oauth_token_async("test_refresh")
            self.assertEqual(token, "refreshed_tok")
            self.assertGreater(expiry, datetime.now(timezone.utc))

        asyncio.run(_run())

    @mock.patch("agym.quota_api.refresh_oauth_token_async")
    def test_get_valid_access_token_refreshes_when_expired(
        self, mock_refresh: mock.Mock
    ) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self._setup_token_file(access_token="old_token", expiry=past)
        future_exp = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_refresh.return_value = ("new_token", future_exp)

        async def _run() -> None:
            tok = await get_valid_access_token_async(self.home)
            self.assertEqual(tok, "new_token")

        asyncio.run(_run())
        mock_refresh.assert_called_once_with("test_refresh_token", timeout=10.0)

        # Check that updated token was persisted
        updated = load_profile_token_data(self.home)
        self.assertEqual(updated["access_token"], "new_token")

    @mock.patch("agym.quota_api._http_post_sync")
    def test_query_quota_api_async_success(self, mock_post: mock.Mock) -> None:
        mock_post.return_value = (200, json.dumps(SAMPLE_GOOGLE_API_RESPONSE))

        async def _run() -> None:
            data = await query_quota_api_async("test_token")
            self.assertIn("groups", data)
            self.assertEqual(len(data["groups"]), 2)

        asyncio.run(_run())

    @mock.patch("agym.quota_api._http_post_sync")
    def test_query_quota_api_async_retries_on_429(self, mock_post: mock.Mock) -> None:
        # First call returns 429, second call returns 200
        mock_post.side_effect = [
            (429, "Too Many Requests"),
            (200, json.dumps(SAMPLE_GOOGLE_API_RESPONSE)),
        ]

        async def _run() -> None:
            with mock.patch("asyncio.sleep", new_callable=mock.AsyncMock):
                data = await query_quota_api_async("test_token")
                self.assertIn("groups", data)

        asyncio.run(_run())
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("agym.quota_api._http_post_sync")
    def test_quota_401_has_safe_typed_error(self, mock_post: mock.Mock) -> None:
        mock_post.return_value = (401, '{"error":"expired","secret":"do-not-log"}')

        async def _run() -> None:
            with self.assertRaises(QuotaUnauthorizedError) as caught:
                await query_quota_api_async("test_token")
            self.assertNotIn("do-not-log", str(caught.exception))

        asyncio.run(_run())

    @mock.patch("agym.quota_api._http_post_sync")
    def test_query_quota_api_uses_matching_host(self, mock_post: mock.Mock) -> None:
        mock_post.return_value = (200, json.dumps(SAMPLE_GOOGLE_API_RESPONSE))

        async def _run() -> None:
            data = await query_quota_api_async("test_token")
            self.assertIn("groups", data)

        asyncio.run(_run())
        self.assertEqual(CLOUDCODE_HOSTS, ("daily-cloudcode-pa.googleapis.com",))
        self.assertIn(CLOUDCODE_HOSTS[0], mock_post.call_args.args[0])
        self.assertEqual(mock_post.call_count, 1)

    def test_fetch_quota_direct_async_returns_none_without_token(self) -> None:
        async def _run() -> None:
            res = await fetch_quota_direct_async(self.profile)
            self.assertIsNone(res)

        asyncio.run(_run())

    @mock.patch("agym.usage._default_subprocess_runner", new_callable=mock.AsyncMock)
    @mock.patch("agym.usage.fetch_quota_direct_async", new_callable=mock.AsyncMock)
    def test_fetch_account_usage_prefers_parallel_api(
        self, mock_direct: mock.AsyncMock, mock_runner: mock.AsyncMock
    ) -> None:
        self._setup_token_file()
        usage, synthetic = normalize_api_quota_response(SAMPLE_GOOGLE_API_RESPONSE, "test_user")
        mock_direct.return_value = (usage, synthetic)

        async def _run() -> None:
            result = await fetch_account_usage_async(Path("/bin/agy"), self.profile, asyncio.Semaphore(1))
            self.assertEqual(result.groups[0].buckets[0].percentage, 62)

        asyncio.run(_run())
        mock_direct.assert_awaited_once()
        mock_runner.assert_not_awaited()

    @mock.patch("agym.usage._default_subprocess_runner", new_callable=mock.AsyncMock)
    @mock.patch("agym.usage.fetch_quota_direct_async", new_callable=mock.AsyncMock)
    def test_fetch_account_usage_uses_cli_when_api_fails(
        self, mock_direct: mock.AsyncMock, mock_runner: mock.AsyncMock
    ) -> None:
        self._setup_token_file()
        mock_direct.return_value = None
        cli_response = {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {"groups": [{
                    "name": "Gemini Models",
                    "buckets": [{
                        "id": "gemini-weekly", "name": "Weekly Limit Remaining",
                        "window": "weekly", "remaining_fraction": 0.4224,
                    }],
                }]},
            },
        }
        mock_runner.return_value = (0, json.dumps(cli_response), "")

        async def _run() -> None:
            sem = asyncio.Semaphore(1)
            result = await fetch_account_usage_async(
                agy_path=Path("/bin/agy"),
                profile=self.profile,
                semaphore=sem,
            )
            self.assertEqual(result.status, "success")
            self.assertEqual(result.account, "test_user")
            self.assertEqual(result.groups[0].buckets[0].percentage, 42)

        asyncio.run(_run())
        mock_direct.assert_awaited_once()
        mock_runner.assert_awaited_once()

    @mock.patch("agym.usage._default_subprocess_runner", new_callable=mock.AsyncMock)
    @mock.patch("agym.usage.fetch_quota_direct_async", new_callable=mock.AsyncMock)
    def test_api_deadline_falls_back_to_cli(
        self, mock_direct: mock.AsyncMock, mock_runner: mock.AsyncMock
    ) -> None:
        self._setup_token_file()
        self.assertEqual(DIRECT_QUOTA_WAIT_SECONDS, 2.5)

        async def stalled_api(*args: object, **kwargs: object) -> None:
            await asyncio.Event().wait()

        mock_direct.side_effect = stalled_api
        mock_runner.return_value = (
            0, json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": []}}}), "",
        )

        async def _run() -> None:
            with mock.patch("agym.usage.DIRECT_QUOTA_WAIT_SECONDS", 0.01):
                result = await fetch_account_usage_async(Path("/bin/agy"), self.profile, asyncio.Semaphore(1))
            self.assertEqual(result.status, "success")

        asyncio.run(_run())
        mock_direct.assert_awaited_once()
        mock_runner.assert_awaited_once()

    def test_fetch_account_usage_async_uses_injected_runner(self) -> None:
        self._setup_token_file()

        async def mock_runner(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
            return 0, json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": []}}}), ""

        async def _run() -> None:
            sem = asyncio.Semaphore(1)
            # When runner is provided, it goes through runner
            result = await fetch_account_usage_async(
                agy_path=Path("/bin/agy"),
                profile=self.profile,
                semaphore=sem,
                runner=mock_runner,
            )
            self.assertEqual(result.status, "success")

        asyncio.run(_run())

    def test_get_oauth_client_credentials(self) -> None:
        cid, sec = get_oauth_client_credentials()
        self.assertTrue(len(cid) > 10)
        self.assertTrue(len(sec) > 10)

    def test_load_and_update_native_oauth_token_data(self) -> None:
        native_dir = self.home / ".gemini" / "antigravity-cli"
        native_dir.mkdir(parents=True, exist_ok=True)
        native_file = native_dir / "antigravity-oauth-token"
        token_payload = {
            "token": {
                "access_token": "ya29.native_tok",
                "refresh_token": "native_ref",
                "expiry": "2026-09-22T20:00:00Z",
            },
            "auth_method": "consumer",
        }
        native_file.write_text(json.dumps(token_payload), encoding="utf-8")

        data = load_profile_token_data(self.home)
        self.assertIsNotNone(data)
        self.assertEqual(data["format"], "native")
        self.assertEqual(data["access_token"], "ya29.native_tok")
        self.assertEqual(data["refresh_token"], "native_ref")

        new_exp = datetime.now(timezone.utc) + timedelta(hours=1)
        ok = update_profile_tokens(self.home, "ya29.refreshed", new_exp, "ref_updated")
        self.assertTrue(ok)

        data2 = load_profile_token_data(self.home)
        self.assertEqual(data2["access_token"], "ya29.refreshed")
        self.assertEqual(data2["refresh_token"], "ref_updated")

    def test_candidate_credentials_contains_fallback(self) -> None:
        candidates = get_candidate_oauth_credentials()
        self.assertGreaterEqual(len(candidates), 1)
        cid, sec = candidates[0]
        self.assertTrue(cid.startswith("1071006060591-") or "apps.googleusercontent.com" in cid)
        self.assertTrue(sec.startswith("GOCSPX-"))

    @mock.patch("agym.quota_api._http_post_sync")
    def test_refresh_oauth_token_retries_next_candidate_on_invalid_client(
        self, mock_post: mock.Mock
    ) -> None:
        # First candidate fails with 401 invalid_client, second candidate succeeds with 200
        mock_post.side_effect = [
            (401, json.dumps({"error": "invalid_client", "error_description": "invalid client secret"})),
            (200, json.dumps({"access_token": "fallback_refreshed_tok", "expires_in": 3600})),
        ]

        async def _run() -> None:
            with mock.patch(
                "agym.quota_api.get_candidate_oauth_credentials",
                return_value=[("bad_cid", "bad_sec"), ("good_cid", "good_sec")],
            ):
                tok, exp = await refresh_oauth_token_async("test_ref")
                self.assertEqual(tok, "fallback_refreshed_tok")

        asyncio.run(_run())
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("agym.quota_api._http_post_sync")
    def test_refresh_invalid_grant_does_not_expose_response(self, mock_post: mock.Mock) -> None:
        mock_post.return_value = (400, '{"error":"invalid_grant","detail":"do-not-log"}')

        async def _run() -> None:
            with mock.patch("agym.quota_api.get_candidate_oauth_credentials", return_value=[("cid", "secret")]):
                with self.assertRaises(OAuthRefreshError) as caught:
                    await refresh_oauth_token_async("test_ref")
            self.assertEqual(caught.exception.code, "invalid_grant")
            self.assertNotIn("do-not-log", str(caught.exception))

        asyncio.run(_run())

    @mock.patch("agym.quota_api.refresh_oauth_token_async", new_callable=mock.AsyncMock)
    @mock.patch("agym.quota_api.query_quota_api_async", new_callable=mock.AsyncMock)
    def test_expired_token_refresh_failure_is_not_retried_twice(
        self, mock_query: mock.AsyncMock, mock_refresh: mock.AsyncMock
    ) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self._setup_token_file(expiry=past)
        mock_refresh.side_effect = OAuthRefreshError("invalid_grant")
        mock_query.side_effect = QuotaUnauthorizedError("Quota API rejected the access token (HTTP 401)")

        result = asyncio.run(fetch_quota_direct_async(self.profile))
        self.assertIsNone(result)
        mock_refresh.assert_awaited_once()
        mock_query.assert_awaited_once()

    @mock.patch("agym.quota_api.refresh_oauth_token_async")
    @mock.patch("agym.quota_api.query_quota_api_async")
    def test_fetch_quota_direct_retries_with_refresh_on_401(
        self, mock_query: mock.Mock, mock_refresh: mock.Mock
    ) -> None:
        self._setup_token_file(access_token="initial_tok", refresh_token="valid_ref", expiry="2099-01-01T00:00:00Z")
        future_exp = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_refresh.return_value = ("retried_tok", future_exp)

        # First query raises 401 Unauthorized, second query succeeds
        mock_query.side_effect = [
            QuotaUnauthorizedError("Quota API rejected the access token (HTTP 401)"),
            SAMPLE_GOOGLE_API_RESPONSE,
        ]

        async def _run() -> None:
            res = await fetch_quota_direct_async(self.profile)
            self.assertIsNotNone(res)
            usage, _ = res  # type: ignore
            self.assertEqual(usage.status, "success")

        asyncio.run(_run())
        self.assertEqual(mock_query.call_count, 2)
        mock_refresh.assert_called_once()

    @mock.patch("agym.quota_api.refresh_oauth_token_async", side_effect=RuntimeError("refresh failed"))
    def test_get_valid_access_token_does_not_log_warning_on_refresh_failure(
        self, mock_refresh: mock.Mock
    ) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self._setup_token_file(access_token="old_token", expiry=past)

        async def _run() -> None:
            with mock.patch("agym.quota_api.logger.warning") as mock_warn:
                tok = await get_valid_access_token_async(self.home)
                self.assertEqual(tok, "old_token")
                mock_warn.assert_not_called()

        asyncio.run(_run())

    def test_parse_iso_datetime_nanoseconds(self) -> None:
        raw_nano = "2026-09-22T20:37:41.572328817+01:00"
        dt = parse_iso_datetime(raw_nano)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 9)
        self.assertEqual(dt.day, 22)


if __name__ == "__main__":
    unittest.main()
