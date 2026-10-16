import asyncio
import base64
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.steam_auth import SteamAuth, SteamError, TokenRejected, begin_qr_login, poll_qr_login

ALICE = "76561198000000001"
NOW = 2_000_000_000.0
DAY = 86_400
ACCESS = "x.eyJleHAiOiAyMDAwMDAzNjAwfQ.y"  # {"exp": NOW + 3600}


class SteamAuthTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.persist = AsyncMock()
        self.auth = SteamAuth(None, "refresh", ALICE, self.persist)
        self.auth.last_renewal = NOW - DAY
        self.call = AsyncMock(return_value={1: b"access"})
        call_patch = patch("bot.steam_auth.call", self.call)
        time_patch = patch("bot.steam_auth.time.time", return_value=NOW)
        call_patch.start()
        time_patch.start()
        self.addCleanup(call_patch.stop)
        self.addCleanup(time_patch.stop)

    async def test_renewal_is_requested_at_the_daily_boundary(self):
        self.assertEqual(await self.auth.get_access_token(), "access")
        self.call.assert_awaited_once_with(
            None,
            "GenerateAccessTokenForApp",
            [(1, "refresh"), (2, ("fixed64", int(ALICE))), (3, 1)],
        )
        self.assertEqual(self.auth.last_renewal, NOW)
        self.persist.assert_awaited_once_with("refresh", NOW)

    async def test_renewal_is_suppressed_before_the_daily_boundary(self):
        previous = NOW - DAY + 1
        self.auth.last_renewal = previous
        self.assertEqual(await self.auth.get_access_token(), "access")
        self.call.assert_awaited_once_with(
            None,
            "GenerateAccessTokenForApp",
            [(1, "refresh"), (2, ("fixed64", int(ALICE))), (3, 0)],
        )
        self.assertEqual(self.auth.last_renewal, previous)
        self.persist.assert_not_awaited()

    async def test_persisted_renewal_suppresses_renewal_after_restart(self):
        self.call.return_value = {1: b"access", 2: b"rotated"}
        await self.auth.get_access_token()
        self.persist.assert_awaited_once_with("rotated", NOW)
        token, renewed_at = self.persist.await_args.args

        restored = SteamAuth(None, token, ALICE, self.persist, last_renewal=renewed_at)
        self.call.reset_mock()
        self.persist.reset_mock()
        self.call.return_value = {1: b"access"}
        with patch("bot.steam_auth.time.time", return_value=NOW + 1):
            self.assertEqual(await restored.get_access_token(), "access")

        self.call.assert_awaited_once_with(
            None,
            "GenerateAccessTokenForApp",
            [(1, "rotated"), (2, ("fixed64", int(ALICE))), (3, 0)],
        )
        self.assertEqual(restored.last_renewal, NOW)
        self.persist.assert_not_awaited()

    async def test_failed_request_does_not_advance_or_persist_renewal(self):
        for error, expected in (
            (SteamError(503, "unavailable"), SteamError),
            (SteamError(401, "revoked"), TokenRejected),
            (OSError("network down"), OSError),
        ):
            with self.subTest(error=type(error).__name__, status=getattr(error, "status", None)):
                self.auth.last_renewal = NOW - DAY
                self.call.side_effect = error
                with self.assertRaises(expected):
                    await self.auth.get_access_token()
                self.assertEqual(self.auth.last_renewal, NOW - DAY)
                self.assertEqual(self.auth.refresh_token, "refresh")
                self.persist.assert_not_awaited()

    async def test_missing_access_token_does_not_advance_or_persist_renewal(self):
        self.call.return_value = {2: b"rotated"}
        with self.assertRaises(TokenRejected):
            await self.auth.get_access_token()
        self.assertEqual(self.auth.last_renewal, NOW - DAY)
        self.assertEqual(self.auth.refresh_token, "refresh")
        self.persist.assert_not_awaited()

    async def test_successful_renewal_persists_time_when_token_is_unchanged(self):
        self.call.return_value = {1: b"access", 2: b"refresh"}
        await self.auth.get_access_token()
        self.assertEqual(self.auth.last_renewal, NOW)
        self.persist.assert_awaited_once_with("refresh", NOW)

    async def test_replacement_token_and_renewal_time_are_persisted_together(self):
        self.call.return_value = {1: b"access", 2: b"rotated"}
        await self.auth.get_access_token()
        self.assertEqual(self.auth.refresh_token, "rotated")
        self.assertEqual(self.auth.last_renewal, NOW)
        self.persist.assert_awaited_once_with("rotated", NOW)

    async def test_failed_persistence_retries_the_same_pair_before_calling_steam(self):
        self.call.return_value = {1: ACCESS.encode(), 2: b"rotated"}
        self.persist.side_effect = [OSError("database unavailable"), None]
        with self.assertRaises(OSError):
            await self.auth.get_access_token()
        self.assertEqual(self.auth.refresh_token, "refresh")
        self.assertEqual(self.auth.last_renewal, NOW - DAY)
        self.assertIsNone(self.auth.access_token)

        with patch("bot.steam_auth.time.time", return_value=NOW + 1):
            self.assertEqual(await self.auth.get_access_token(), ACCESS)

        self.assertEqual(self.auth.refresh_token, "rotated")
        self.assertEqual(self.auth.last_renewal, NOW)
        self.assertEqual(self.auth.access_token, ACCESS)
        self.call.assert_awaited_once()
        self.assertEqual(self.persist.await_args_list, [call("rotated", NOW), call("rotated", NOW)])

    async def test_expired_pending_access_is_refreshed_after_persistence_recovers(self):
        self.call.return_value = {1: ACCESS.encode(), 2: b"rotated"}
        self.persist.side_effect = [OSError("database unavailable"), None]
        with self.assertRaises(OSError):
            await self.auth.get_access_token()

        async def generate(*args):
            self.assertEqual(self.persist.await_args_list, [call("rotated", NOW), call("rotated", NOW)])
            return {1: b"fresh access"}

        self.call.side_effect = generate
        with patch("bot.steam_auth.time.time", return_value=NOW + 3601):
            self.assertEqual(await self.auth.get_access_token(), "fresh access")

        self.assertEqual(self.auth.refresh_token, "rotated")
        self.assertEqual(self.auth.last_renewal, NOW)
        self.assertEqual(self.call.await_count, 2)
        self.call.assert_awaited_with(
            None,
            "GenerateAccessTokenForApp",
            [(1, "rotated"), (2, ("fixed64", int(ALICE))), (3, 0)],
        )

    async def test_registration_can_renew_without_a_persistence_callback(self):
        auth = SteamAuth(None, "refresh", ALICE)
        self.call.return_value = {1: b"access", 2: b"rotated"}
        self.assertEqual(await auth.get_access_token(), "access")
        self.assertEqual(auth.refresh_token, "rotated")
        self.assertEqual(auth.last_renewal, NOW)

    async def test_concurrent_requests_share_one_access_token_renewal(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def generate(*args):
            started.set()
            await release.wait()
            return {1: ACCESS.encode(), 2: b"rotated"}

        self.call.side_effect = generate
        first = asyncio.create_task(self.auth.get_access_token())
        await started.wait()
        second = asyncio.create_task(self.auth.get_access_token())
        await asyncio.sleep(0)  # let the second request reach the in-flight renewal
        release.set()

        self.assertEqual(await asyncio.gather(first, second), [ACCESS, ACCESS])
        self.call.assert_awaited_once()
        self.persist.assert_awaited_once_with("rotated", NOW)

    async def test_valid_cached_access_token_skips_steam(self):
        self.auth.access_token = ACCESS
        self.assertEqual(await self.auth.get_access_token(), ACCESS)
        self.assertEqual(self.auth.last_renewal, NOW - DAY)
        self.call.assert_not_awaited()
        self.persist.assert_not_awaited()


class SteamAuthWireTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixtures = json.loads((Path(__file__).parent / "fixtures/steam/auth.json").read_text())
        self.response = self.fixtures["begin"]
        self.requests = []

        async def respond(request):
            form = await request.post()
            self.requests.append(
                {
                    "path": request.path,
                    "headers": request.headers,
                    "content_type": request.content_type,
                    "fields": set(form),
                    "protobuf": base64.b64decode(form["input_protobuf_encoded"], validate=True),
                }
            )
            return web.Response(
                body=bytes.fromhex(self.response["body_hex"]),
                status=self.response.get("status", 200),
                headers=self.response.get("headers", {}),
            )

        app = web.Application()
        app.router.add_post("/IAuthenticationService/{method}/v1/", respond)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.http.close)
        api_patch = patch("bot.steam_auth.API", str(server.make_url("/IAuthenticationService")))
        api_patch.start()
        self.addCleanup(api_patch.stop)

    async def test_qr_begin_posts_mobile_device_protobuf_and_decodes_interval(self):
        login = await begin_qr_login(self.http)

        self.assertEqual(
            login,
            {
                "client_id": 4242,
                "request_id": b"fixture-request-id",
                "url": "https://s.team/q/fixture-only",
                "interval": 2.5,
            },
        )
        request = self.requests[0]
        self.assertEqual(request["path"], "/IAuthenticationService/BeginAuthSessionViaQR/v1/")
        self.assertEqual(request["content_type"], "multipart/form-data")
        self.assertEqual(request["fields"], {"input_protobuf_encoded"})
        self.assertEqual(request["protobuf"], bytes.fromhex(self.fixtures["begin"]["request_hex"]))
        self.assertEqual(request["headers"]["user-agent"], "okhttp/4.9.2")
        self.assertIn("mobileClient=android", request["headers"]["cookie"])

    async def test_qr_begin_defaults_poll_interval_when_steam_omits_it(self):
        self.response = self.fixtures["begin_without_interval"]
        self.assertEqual((await begin_qr_login(self.http))["interval"], 5.0)

    async def test_qr_poll_waits_rotates_and_returns_approved_refresh_token(self):
        login = await begin_qr_login(self.http)
        self.response = self.fixtures["pending"]
        self.assertIsNone(await poll_qr_login(self.http, login))
        self.assertEqual(login["url"], "https://s.team/q/fixture-only")
        self.assertEqual(self.requests[-1]["protobuf"], bytes.fromhex(self.response["request_hex"]))

        self.response = self.fixtures["rotated"]
        self.assertIsNone(await poll_qr_login(self.http, login))
        self.assertEqual(login["client_id"], 4243)
        self.assertEqual(login["url"], "https://s.team/q/fixture-rotated")
        self.assertEqual(login["request_id"], b"fixture-request-id")

        self.response = self.fixtures["approved"]
        self.assertEqual(await poll_qr_login(self.http, login), "fixture-refresh-approved")
        self.assertEqual(self.requests[-1]["path"], "/IAuthenticationService/PollAuthSessionStatus/v1/")
        self.assertEqual(self.requests[-1]["protobuf"], bytes.fromhex(self.response["request_hex"]))

    async def test_access_token_renewal_posts_fixed64_steam_id_and_persists_decoded_tokens(self):
        self.response = self.fixtures["renewed"]
        persist = AsyncMock()
        auth = SteamAuth(self.http, "fixture-refresh", ALICE, persist, last_renewal=NOW - DAY)
        with patch("bot.steam_auth.time.time", return_value=NOW):
            self.assertEqual(await auth.get_access_token(), ACCESS)
            self.assertEqual(await auth.get_access_token(), ACCESS)

        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["path"], "/IAuthenticationService/GenerateAccessTokenForApp/v1/")
        self.assertEqual(self.requests[0]["protobuf"], bytes.fromhex(self.response["request_hex"]))
        self.assertEqual(auth.refresh_token, "fixture-refresh-renewed")
        persist.assert_awaited_once_with("fixture-refresh-renewed", NOW)

    async def test_steam_token_rejection_preserves_stored_credentials(self):
        for status in (200, 401, 403):
            with self.subTest(status=status):
                self.response = {**self.fixtures["rejected"], "status": status}
                persist = AsyncMock()
                auth = SteamAuth(self.http, "fixture-refresh", ALICE, persist, last_renewal=NOW - DAY)
                with self.assertRaisesRegex(TokenRejected, "EResult 15 Access Denied"):
                    await auth.get_access_token()
                self.assertEqual(auth.refresh_token, "fixture-refresh")
                self.assertEqual(auth.last_renewal, NOW - DAY)
                self.assertIsNone(auth.access_token)
                persist.assert_not_awaited()

    async def test_http_outage_is_retryable_and_keeps_steam_error_status(self):
        self.response = {"status": 503, "body_hex": ""}
        auth = SteamAuth(self.http, "fixture-refresh", ALICE)
        with self.assertRaises(SteamError) as caught:
            await auth.get_access_token()
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertEqual(auth.refresh_token, "fixture-refresh")

    async def test_success_without_access_token_is_rejected_before_persistence(self):
        self.response = self.fixtures["missing_access"]
        persist = AsyncMock()
        auth = SteamAuth(self.http, "fixture-refresh", ALICE, persist)
        with self.assertRaisesRegex(TokenRejected, "no access token"):
            await auth.get_access_token()
        self.assertEqual(auth.refresh_token, "fixture-refresh")
        persist.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
