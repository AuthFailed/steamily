import unittest

from bot.steam import SteamActionError, fetch_user_country, get_json, post_json
from bot.steam_auth import TokenRejected


class Response:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return {"response": {"country": "US"}}


class HTTP:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers

    def get(self, *args, **kwargs):
        return Response(self.status, self.headers)

    post = get


class ReadAuthorizationTest(unittest.IsolatedAsyncioTestCase):
    async def test_auth_refusals_are_not_treated_as_transient_outages_or_missing_country(self):
        for status in (401, 403):
            for read in (
                lambda http: get_json(http, "IFamilyGroupsService/GetFamilyGroupForUser/v1", {}),
                lambda http: post_json(http, "IFamilyGroupsService/GetPlaytimeSummary/v1", {}),
                lambda http: fetch_user_country(http, "private-token", "1"),
            ):
                with self.subTest(status=status, read=read), self.assertRaises(TokenRejected):
                    await read(HTTP(status))

    async def test_server_failure_preserves_the_distinction_from_rejected_auth(self):
        with self.assertRaises(RuntimeError):
            await get_json(HTTP(503), "IFamilyGroupsService/GetFamilyGroupForUser/v1", {})
        self.assertIsNone(await fetch_user_country(HTTP(503), "private-token", "1"))

    async def test_explicit_auth_results_stop_reads_even_with_http_200(self):
        for result in (5, 26, 27, 43):
            for read in (
                lambda http: get_json(http, "IFamilyGroupsService/GetFamilyGroupForUser/v1", {}),
                lambda http: post_json(http, "IFamilyGroupsService/GetPlaytimeSummary/v1", {}),
                lambda http: fetch_user_country(http, "private-token", "1"),
            ):
                with self.subTest(result=result, read=read), self.assertRaises(TokenRejected):
                    await read(HTTP(200, {"x-eresult": str(result)}))

    async def test_endpoint_permission_refusal_does_not_reject_the_whole_session(self):
        with self.assertRaises(SteamActionError):
            await post_json(HTTP(200, {"x-eresult": "15"}), "IFamilyGroupsService/InviteToFamilyGroup/v1", {})
