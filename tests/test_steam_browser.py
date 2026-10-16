import base64
import json
import time
import unittest
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, patch

import aiohttp
from multidict import CIMultiDict
from yarl import URL

from bot.steam_auth import SteamError, TokenRejected, decode, encode
from scripts import steam_browser_session as browser

ALICE = "76561198000000001"
BOB = "76561198000000002"
LOGIN = "https://login.steampowered.com"
POINTS = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"
REFRESH_URL = LOGIN + "/jwt/refresh?redir=https%3A%2F%2Fstore.steampowered.com%2Fpointssummary%2Fajaxgetasyncconfig"


def token(steam_id=ALICE, exp=None):
    payload = {"sub": steam_id, "exp": exp if exp is not None else int(time.time()) + 3600}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "x." + encoded + ".y"


def cookie(steam_id=ALICE):
    return steam_id + "%7C%7C" + token(steam_id)


def form_fields(form):
    assert form.is_multipart
    return {metadata["name"]: value for metadata, headers, value in form._fields}


class Response:
    def __init__(self, body=None, *, status=200, headers=None, cookies=None):
        self.body = body
        self.status = status
        self.headers = CIMultiDict(headers or {})
        self.cookies = SimpleCookie()
        for key, value in (cookies or {}).items():
            self.cookies[key] = value
            self.cookies[key]["path"] = "/"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return self.body

    async def text(self):
        return self.body if isinstance(self.body, str) else json.dumps(self.body)


class HTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.cookie_jar = aiohttp.CookieJar()

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        self.cookie_jar.update_cookies(response.cookies, response_url=URL(url))
        return response

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class BrowserLoginTest(unittest.IsolatedAsyncioTestCase):
    async def test_browser_qr_request_and_rotation_use_browser_platform(self):
        rpc = AsyncMock(
            side_effect=[
                {1: 7, 2: b"https://s.team/q/abc", 3: b"request"},
                {1: 8, 2: b"https://s.team/q/def"},
                {3: token().encode()},
            ]
        )
        with patch.object(browser, "call", rpc):
            login = await browser.begin_browser_login(None)
            self.assertEqual(
                login, {"client_id": 7, "request_id": b"request", "url": "https://s.team/q/abc", "interval": 5.0}
            )
            self.assertIsNone(await browser.poll_browser_login(None, login))
            self.assertEqual(login["client_id"], 8)
            self.assertEqual(login["url"], "https://s.team/q/def")
            self.assertEqual(await browser.poll_browser_login(None, login), token())
        self.assertEqual(rpc.await_args_list[0].args[1], "BeginAuthSessionViaQR")
        fields = decode(encode(rpc.await_args_list[0].args[2]))
        self.assertEqual(set(fields), {3, 4})
        self.assertEqual(fields[4], b"Community")
        self.assertEqual(decode(fields[3]), {1: browser.BROWSER_HEADERS["user-agent"].encode(), 2: 2})
        for request in rpc.await_args_list:
            self.assertEqual(request.kwargs, {"headers": browser.BROWSER_HEADERS})
        self.assertNotIn("cookie", browser.BROWSER_HEADERS)
        self.assertNotIn("android", browser.BROWSER_HEADERS["user-agent"].lower())

    async def test_poll_updates_optional_qr_rotation_fields_independently(self):
        old_url, new_url = "https://s.team/q/abc", "https://s.team/q/def"
        for response, expected_id, expected_url in [
            ({1: 8}, 8, old_url),
            ({2: new_url.encode()}, 7, new_url),
            ({1: 0, 2: b""}, 7, old_url),
            ({1: 0, 2: new_url.encode()}, 7, new_url),
            ({1: 8, 2: b""}, 8, old_url),
        ]:
            login = {"client_id": 7, "request_id": b"request", "url": old_url, "interval": 5.0}
            rpc = AsyncMock(return_value=response)
            with self.subTest(response=response), patch.object(browser, "call", rpc):
                self.assertIsNone(await browser.poll_browser_login(None, login))
                self.assertEqual(login["client_id"], expected_id)
                self.assertEqual(login["url"], expected_url)
                self.assertEqual(login["request_id"], b"request")
                rpc.assert_awaited_once_with(
                    None, "PollAuthSessionStatus", [(1, 7), (2, b"request")], headers=browser.BROWSER_HEADERS
                )

    async def test_qr_errors_do_not_expose_server_text_or_urls(self):
        with patch.object(browser, "call", AsyncMock(side_effect=SteamError(401, "secret-token"))):
            with self.assertRaises(RuntimeError) as error:
                await browser.begin_browser_login(None)
        self.assertNotIn("secret-token", str(error.exception))
        self.assertIsNone(error.exception.__cause__)

    async def test_finalization_and_supplied_transfers_return_durable_cookie(self):
        refresh, durable = token(), cookie()
        http = HTTP(
            Response(
                {"steamID": ALICE, "transfer_info": [{"url": POINTS, "params": {"nonce": "transfer-secret"}}]},
                cookies={"steamRefresh_steam": durable},
            ),
            Response({"result": 1}, cookies={"steamLoginSecure": cookie()}),
        )
        with patch.object(browser.secrets, "token_hex", return_value="session-value"):
            self.assertEqual(await browser.finalize_login(http, refresh, ALICE), durable)
        self.assertEqual(len(http.requests), 2)
        method, url, kwargs = http.requests[0]
        self.assertEqual((method, url), ("POST", LOGIN + "/jwt/finalizelogin"))
        self.assertEqual(
            form_fields(kwargs["data"]),
            {"nonce": refresh, "sessionid": "session-value", "redir": "https://steamcommunity.com/login/home/?goto="},
        )
        self.assertEqual(kwargs["headers"]["Origin"], "https://steamcommunity.com")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(form_fields(http.requests[1][2]["data"]), {"steamID": ALICE, "nonce": "transfer-secret"})
        self.assertFalse(http.requests[1][2]["allow_redirects"])

    async def test_finalization_persists_durable_cookie_before_transfer_failure(self):
        durable = cookie()
        http = HTTP(
            Response({"transfer_info": [{"url": POINTS, "params": {}}]}, cookies={"steamRefresh_steam": durable}),
            Response("unavailable", status=500),
        )

        async def persist(value):
            self.assertEqual(value, durable)
            self.assertEqual(len(http.requests), 1)

        save = AsyncMock(side_effect=persist)
        with self.assertRaises(RuntimeError):
            await browser.finalize_login(http, token(), ALICE, on_refresh=save)
        save.assert_awaited_once_with(durable)
        self.assertEqual(len(http.requests), 2)

    async def test_finalization_persists_valid_cookie_before_body_failure(self):
        durable = cookie()
        save = AsyncMock()
        http = HTTP(Response(None, cookies={"steamRefresh_steam": durable}))
        with self.assertRaises(TokenRejected):
            await browser.finalize_login(http, token(), ALICE, on_refresh=save)
        save.assert_awaited_once_with(durable)

    async def test_finalization_accepts_official_help_domain_transfer(self):
        destination = "https://help.steampowered.com/login/settoken"
        durable = cookie()
        http = HTTP(
            Response(
                {"transfer_info": [{"url": destination, "params": {"auth": "transfer-secret"}}]},
                cookies={"steamRefresh_steam": durable},
            ),
            Response({"result": 1}, cookies={"steamLoginSecure": cookie()}),
        )
        self.assertEqual(await browser.finalize_login(http, token(), ALICE), durable)
        self.assertEqual(http.requests[1][1], destination)

    async def test_finalization_requires_a_durable_cookie_and_valid_transfers(self):
        for body, cookies in [
            ({"transfer_info": []}, {}),
            ({"error": 5}, {}),
            ({}, {}),
            ({"transfer_info": []}, {"steamRefresh_steam": cookie(BOB)}),
            ({"steamID": BOB, "transfer_info": []}, {"steamRefresh_steam": cookie()}),
        ]:
            with self.subTest(body=body):
                with self.assertRaises((RuntimeError, TokenRejected)):
                    await browser.finalize_login(HTTP(Response(body, cookies=cookies)), token(), ALICE)

    async def test_hostile_transfer_is_rejected_before_any_transfer_request(self):
        for url in [
            "https://evil.example/x",
            "http://store.steampowered.com/x",
            "https://user:password@store.steampowered.com/x",
            "https://store.steampowered.com:444/x",
        ]:
            http = HTTP(
                Response(
                    {"transfer_info": [{"url": POINTS, "params": {}}, {"url": url, "params": {}}]},
                    cookies={"steamRefresh_steam": cookie()},
                )
            )
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                await browser.finalize_login(http, token(), ALICE)
            self.assertEqual(len(http.requests), 1)

    async def test_transfer_cannot_override_account_or_accept_another_accounts_cookie(self):
        for params, secure in [({"steamID": BOB}, cookie()), ({}, cookie(BOB)), ({}, None)]:
            http = HTTP(
                Response(
                    {"transfer_info": [{"url": POINTS, "params": params}]}, cookies={"steamRefresh_steam": cookie()}
                ),
                Response({"result": 1}, cookies={"steamLoginSecure": secure} if secure else {}),
            )
            with self.subTest(params=params), self.assertRaises((RuntimeError, TokenRejected)):
                await browser.finalize_login(http, token(), ALICE)


class BrowserRenewalTest(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_renewal_uses_only_refresh_cookie_and_accepts_rotation(self):
        original, rotated, access = cookie(), ALICE + "%7C%7C" + token(exp=int(time.time()) + 7200), token()
        http = HTTP(
            Response(status=302, headers={"Location": POINTS}, cookies={"steamRefresh_steam": rotated}),
            Response({"success": 1, "data": {"webapi_token": access}}),
        )
        self.assertEqual(await browser.renew_access_token(http, original, ALICE), (access, rotated))
        self.assertEqual(
            [(method, url) for method, url, kwargs in http.requests], [("GET", REFRESH_URL), ("GET", POINTS)]
        )
        self.assertEqual(http.requests[0][2]["headers"], browser.BROWSER_HEADERS)
        self.assertFalse(http.requests[0][2]["allow_redirects"])
        self.assertEqual(http.cookie_jar.filter_cookies(URL(LOGIN))["steamRefresh_steam"].value, rotated)
        self.assertNotIn("steamLoginSecure", http.cookie_jar.filter_cookies(URL(POINTS)))

    async def test_rotated_cookie_is_persisted_before_later_redirect_failure(self):
        original, rotated = cookie(), ALICE + "%7C%7C" + token(exp=int(time.time()) + 7200)
        for failure in [Response("unavailable", status=500), TimeoutError("secret-url")]:
            http = HTTP(
                Response(status=302, headers={"Location": POINTS}, cookies={"steamRefresh_steam": rotated}), failure
            )

            async def persist(value, request_http=http):
                self.assertEqual(value, rotated)
                self.assertEqual(len(request_http.requests), 1)

            save = AsyncMock(side_effect=persist)
            with self.subTest(failure=type(failure).__name__), self.assertRaises(RuntimeError):
                await browser.renew_access_token(http, original, ALICE, on_refresh=save)
            save.assert_awaited_once_with(rotated)
            self.assertEqual(len(http.requests), 2)

    async def test_unchanged_cookie_does_not_invoke_persistence(self):
        durable, access = cookie(), token()
        save = AsyncMock()
        http = HTTP(Response({"webapi_token": access}, cookies={"steamRefresh_steam": durable}))
        self.assertEqual(await browser.renew_access_token(http, durable, ALICE, on_refresh=save), (access, durable))
        save.assert_not_awaited()

    async def test_persistence_failure_stops_before_next_redirect(self):
        rotated = ALICE + "%7C%7C" + token(exp=int(time.time()) + 7200)
        http = HTTP(Response(status=302, headers={"Location": POINTS}, cookies={"steamRefresh_steam": rotated}))
        save = AsyncMock(side_effect=OSError("disk unavailable"))
        with self.assertRaises(OSError):
            await browser.renew_access_token(http, cookie(), ALICE, on_refresh=save)
        self.assertEqual(len(http.requests), 1)

    async def test_absent_rotation_preserves_full_encoded_cookie(self):
        durable, access = cookie(), token()
        http = HTTP(Response({"success": 1, "data": {"webapi_token": access}}))
        self.assertEqual(await browser.renew_access_token(http, durable, ALICE), (access, durable))
        self.assertEqual(http.cookie_jar.filter_cookies(URL(LOGIN))["steamRefresh_steam"].value, durable)

    async def test_refresh_can_be_a_raw_jwt(self):
        durable, access = token(), token()
        self.assertEqual(
            await browser.renew_access_token(HTTP(Response({"webapi_token": access})), durable, ALICE),
            (access, durable),
        )

    async def test_hostile_redirect_is_not_followed_and_error_is_redacted(self):
        for url in [
            "https://evil.example/?token=secret",
            "http://store.steampowered.com/",
            "https://u@store.steampowered.com/",
            "https://store.steampowered.com:444/",
        ]:
            http = HTTP(Response(status=302, headers={"Location": url}))
            with self.subTest(url=url), self.assertRaises(RuntimeError) as error:
                await browser.renew_access_token(http, cookie(), ALICE)
            self.assertEqual(len(http.requests), 1)
            self.assertNotIn("secret", str(error.exception))
            self.assertNotIn(url, str(error.exception))

    async def test_redirect_limit_and_login_page_reject_expired_session(self):
        http = HTTP(*(Response(status=302, headers={"Location": POINTS}) for _ in range(7)))
        with self.assertRaises(RuntimeError):
            await browser.renew_access_token(http, cookie(), ALICE)
        self.assertLessEqual(len(http.requests), 6)
        for body in [
            "<html>Log in</html>",
            {"success": 1, "data": []},
            {"webapi_token": ""},
            {"success": 0, "webapi_token": token()},
            {"webapi_token": "malformed"},
            {"webapi_token": token(BOB)},
            {"webapi_token": token(exp=1)},
        ]:
            with self.subTest(body=body), self.assertRaises(TokenRejected):
                await browser.renew_access_token(HTTP(Response(body)), cookie(), ALICE)

    async def test_bad_input_or_rotated_cookie_is_rejected(self):
        for durable in ["garbage", cookie(BOB), BOB + "%7C%7C" + token(), "", token(exp=1)]:
            http = HTTP()
            with self.subTest(durable=durable), self.assertRaises(TokenRejected):
                await browser.renew_access_token(http, durable, ALICE)
            self.assertEqual(http.requests, [])
        for durable in [cookie(BOB), "", "garbage"]:
            http = HTTP(Response({"webapi_token": token()}, cookies={"steamRefresh_steam": durable}))
            with self.subTest(durable=durable), self.assertRaises(TokenRejected):
                await browser.renew_access_token(http, cookie(), ALICE)

    async def test_refresh_cookie_is_only_accepted_from_login_domain(self):
        for domain in ["evil.example", ".steampowered.com"]:
            response = Response({"webapi_token": token()}, cookies={"steamRefresh_steam": cookie()})
            response.cookies["steamRefresh_steam"]["domain"] = domain
            with self.subTest(domain=domain), self.assertRaises(TokenRejected):
                await browser.renew_access_token(HTTP(response), cookie(), ALICE)
        http = HTTP(
            Response(status=302, headers={"Location": POINTS}),
            Response({"webapi_token": token()}, cookies={"steamRefresh_steam": cookie()}),
        )
        with self.assertRaises(TokenRejected):
            await browser.renew_access_token(http, cookie(), ALICE)

    async def test_unexpected_jwt_payload_shapes_are_safe(self):
        for payload in [[], "secret", None, {"sub": ALICE, "exp": True}, {"sub": ALICE, "exp": float("inf")}]:
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            with self.subTest(payload=type(payload).__name__), self.assertRaises(TokenRejected):
                await browser.renew_access_token(HTTP(), "x." + encoded + ".y", ALICE)

    async def test_http_and_network_errors_are_safe(self):
        for response in [
            Response("secret", status=401),
            Response("secret", status=500),
            aiohttp.ClientError("secret-token https://example.invalid/?token=secret"),
        ]:
            with (
                self.subTest(response=type(response).__name__),
                self.assertRaises((RuntimeError, TokenRejected)) as error,
            ):
                await browser.renew_access_token(HTTP(response), cookie(), ALICE)
            self.assertNotIn("secret", str(error.exception))
            self.assertIsNone(error.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
