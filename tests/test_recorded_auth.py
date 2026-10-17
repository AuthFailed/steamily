import base64
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import steam_browser as browser
from bot.steam_auth import SteamAuth, TokenRejected, jwt_payload
from tests.test_steam_browser import HTTP, Response

NOW = 1_900_000_000


class RecordedSteamAuthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = json.loads((Path(__file__).parent / "fixtures/steam/auth_recorded.json").read_text())
        self.response = self.fixture["begin"]
        self.requests = []

        async def respond(request):
            form = await request.post()
            self.requests.append(
                (
                    request.path,
                    base64.b64decode(form["input_protobuf_encoded"], validate=True),
                    request.headers,
                )
            )
            return web.Response(
                status=self.response["status"],
                headers=self.response["headers"],
                body=bytes.fromhex(self.response["body_hex"]),
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

    async def test_recorded_anonymous_qr_begin_and_pending_poll(self):
        login = await browser.begin_browser_login(self.http)
        expected = {
            "client_id": 4242,
            "request_id": b"recorded-request-id",
            "url": "https://s.team/q/recorded-redacted",
            "interval": 5.0,
        }
        self.assertEqual(login, expected)
        self.response = self.fixture["pending"]
        self.assertIsNone(await browser.poll_browser_login(self.http, login))
        self.assertEqual(login, expected)
        self.assertEqual(
            [(path, protobuf) for path, protobuf, headers in self.requests],
            [
                (
                    "/IAuthenticationService/BeginAuthSessionViaQR/v1/",
                    bytes.fromhex("1a0c0a08537465616d696c7910022209436f6d6d756e697479"),
                ),
                (
                    "/IAuthenticationService/PollAuthSessionStatus/v1/",
                    bytes.fromhex("08922112137265636f726465642d726571756573742d6964"),
                ),
            ],
        )
        for _path, _protobuf, headers in self.requests:
            self.assertEqual(headers["user-agent"], "Steamily")
            self.assertNotIn("cookie", headers)

    async def test_recorded_invalid_refresh_rejection_preserves_credentials(self):
        self.response = self.fixture["rejected"]
        refresh = self.response["request_refresh_token"]
        persist = AsyncMock()
        auth = SteamAuth(self.http, refresh, self.fixture["steam_id"], persist, last_renewal=NOW)
        with patch("bot.steam_auth.time.time", return_value=NOW), self.assertRaises(TokenRejected):
            await auth.get_access_token()
        self.assertEqual(auth.refresh_token, refresh)
        self.assertEqual(auth.last_renewal, NOW)
        self.assertIsNone(auth.access_token)
        persist.assert_not_awaited()
        self.assertEqual(len(self.requests), 1)
        path, protobuf, headers = self.requests[0]
        self.assertEqual(path, "/IAuthenticationService/GenerateAccessTokenForApp/v1/")
        self.assertEqual(
            protobuf,
            bytes.fromhex("0a187265636f726465642d696e76616c69642d7265667265736811014c5e02010010011800"),
        )

    async def test_recorded_browser_redirect_chain_renews_without_cookie_rotation(self):
        responses = [
            Response(
                item.get("body"),
                status=item["status"],
                headers=item["headers"],
                cookies={"steamRefresh_steam": item["refresh_cookie"]} if "refresh_cookie" in item else {},
            )
            for item in self.fixture["renewal"]
        ]
        http = HTTP(*responses)
        save = AsyncMock()
        cookie = self.fixture["refresh_cookie"]
        access = self.fixture["renewal"][-1]["body"]["data"]["webapi_token"]
        with patch("bot.steam_browser.time.time", return_value=NOW):
            self.assertEqual(
                await browser.renew_access_token(http, cookie, self.fixture["steam_id"], on_refresh=save),
                (access, cookie),
            )
        self.assertEqual(
            jwt_payload(access),
            {"sub": "76561198000000001", "aud": ["web:store"], "exp": 2_000_000_000},
        )
        self.assertEqual(
            [(method, url) for method, url, kwargs in http.requests],
            [
                ("GET", browser.REFRESH_URL),
                ("GET", "https://store.steampowered.com/login/settoken"),
                ("GET", browser.POINTS),
                ("GET", browser.POINTS),
            ],
        )
        for _method, _url, kwargs in http.requests:
            self.assertEqual(kwargs["headers"], browser.BROWSER_HEADERS)
            self.assertFalse(kwargs["allow_redirects"])
        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
