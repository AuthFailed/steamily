import unittest
from unittest.mock import AsyncMock, patch

from bot.steam import NoFamily, fetch_family_snapshot, parse_snapshot

INCOMPLETE_FAMILIES = [
    {},
    {"response": {"is_not_member_of_any_group": False}},
    {"response": {"is_not_member_of_any_group": "true"}},
    {"response": {"is_not_member_of_any_group": 1}},
    {"response": {"family_group": {"members": [{"steamid": "1"}]}}},
    {"response": {"family_groupid": 0, "family_group": {"members": [{"steamid": "1"}]}}},
    {"response": {"family_groupid": "0", "family_group": {"members": [{"steamid": "1"}]}}},
    {"response": {"family_groupid": "9"}},
    {"response": {"family_groupid": "9", "family_group": {"members": {}}}},
    {"response": {"family_groupid": "9", "family_group": {"members": []}}},
]


class SteamMembershipParsingTest(unittest.TestCase):
    def test_only_confirmed_departure_means_no_family(self):
        with self.assertRaises(NoFamily):
            parse_snapshot({"response": {"is_not_member_of_any_group": True}}, {}, {})

    def test_incomplete_response_is_temporary(self):
        for family in INCOMPLETE_FAMILIES:
            with self.subTest(family=family):
                with self.assertRaises(RuntimeError) as caught:
                    parse_snapshot(family, {"response": {"apps": []}}, {})
                self.assertIs(type(caught.exception), RuntimeError)


class SteamMembershipFetchingTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_confirmed_departure_means_no_family(self):
        with patch("bot.steam.get_json", AsyncMock(return_value={"response": {"is_not_member_of_any_group": True}})):
            with self.assertRaises(NoFamily):
                await fetch_family_snapshot(None, "token")

    async def test_incomplete_response_is_temporary_without_fetching_library(self):
        for family in INCOMPLETE_FAMILIES:
            with self.subTest(family=family):
                with patch("bot.steam.get_json", AsyncMock(return_value=family)) as get_json:
                    with self.assertRaises(RuntimeError) as caught:
                        await fetch_family_snapshot(None, "token")
                    self.assertIs(type(caught.exception), RuntimeError)
                    get_json.assert_awaited_once()
