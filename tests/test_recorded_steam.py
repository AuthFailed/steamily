import json
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.steam import fetch_family_snapshot, parse_store_item, store_cards

FIXTURES = Path(__file__).parent / "fixtures/steam"
MEMBERS = [f"7656119800000000{i}" for i in range(1, 6)]


class RecordedSteamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = {
            method: json.loads((FIXTURES / f"{name}_recorded.json").read_text())
            for method, name in (
                ("IFamilyGroupsService/GetFamilyGroupForUser/v1", "family"),
                ("IFamilyGroupsService/GetSharedLibraryApps/v1", "library_snapshot"),
                ("IPlayerService/GetPlayerLinkDetails/v1", "players"),
                ("IFamilyGroupsService/GetPlaytimeSummary/v1", "playtime"),
                ("IStoreBrowseService/GetItems/v1", "store"),
            )
        }
        self.requests = []

        async def respond(request):
            method = request.match_info["method"]
            params = dict(await request.post()) if request.method == "POST" else dict(request.query)
            self.requests.append((request.method, method, params))
            return web.json_response(self.responses[method])

        app = web.Application()
        app.router.add_route("*", "/{method:.+}/", respond)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.http.close)
        api_patch = patch("bot.steam.API", str(server.make_url("")).rstrip("/"))
        api_patch.start()
        self.addCleanup(api_patch.stop)

    async def test_recorded_family_players_library_and_playtime_form_one_snapshot(self):
        with patch("bot.steam.time.time", return_value=2_000_000_000.0):
            snapshot = await fetch_family_snapshot(self.http, "recorded-access")

        self.assertEqual(
            snapshot,
            {
                "fetched_at": 2_000_000_000.0,
                "family_id": "42",
                "name": "Recorded family",
                "members": MEMBERS,
                "joined": {member: 1700000000 + index for index, member in enumerate(MEMBERS)},
                "roles": dict.fromkeys(MEMBERS, 1),
                "free_spots": 1,
                "calendar": {"checked_at": 2_000_000_000.0, "member_cooldowns": {}, "free_spots": 1},
                "pending_invites": [],
                "names": {member: f"Recorded member {index:02}" for index, member in enumerate(MEMBERS, 1)},
                "players": {
                    member: {
                        "vanity": f"recorded-member-{index:02}" if index in (1, 4, 5) else None,
                        "online": index == 1,
                        "created": 1600000000,
                        "last_online": 1700000030,
                        "game": None,
                        "game_name": None,
                    }
                    for index, member in enumerate(MEMBERS, 1)
                },
                "playtime": [{"steamid": MEMBERS[2], "appid": 201, "seconds": 4688}],
                "apps": [
                    {
                        "appid": 201,
                        "name": "Recorded game 201",
                        "owners": [MEMBERS[0]],
                        "acquired": 1700000020,
                        "exclude_reason": 0,
                    },
                    {
                        "appid": 202,
                        "name": "Recorded game 202",
                        "owners": [MEMBERS[0]],
                        "acquired": 1700000020,
                        "exclude_reason": 3,
                    },
                    {
                        "appid": 203,
                        "name": "Recorded game 203",
                        "owners": [MEMBERS[0], MEMBERS[1], MEMBERS[4]],
                        "acquired": 1700000020,
                        "exclude_reason": 0,
                    },
                ],
            },
        )
        self.assertEqual(
            [(verb, method) for verb, method, _ in self.requests],
            [
                ("GET", "IFamilyGroupsService/GetFamilyGroupForUser/v1"),
                ("GET", "IFamilyGroupsService/GetSharedLibraryApps/v1"),
                ("GET", "IPlayerService/GetPlayerLinkDetails/v1"),
                ("POST", "IFamilyGroupsService/GetPlaytimeSummary/v1"),
            ],
        )
        self.assertEqual(self.requests[1][2]["family_groupid"], "42")
        self.assertEqual(
            self.requests[2][2],
            {"access_token": "recorded-access", **{f"steamids[{i}]": member for i, member in enumerate(MEMBERS)}},
        )
        self.assertEqual(self.requests[3][2], {"access_token": "recorded-access", "family_groupid": "42"})

    async def test_recorded_store_prices_tags_play_modes_and_cards(self):
        items = self.responses["IStoreBrowseService/GetItems/v1"]["response"]["store_items"]
        with patch("bot.steam.time.time", return_value=2_000_000_000.0):
            self.assertEqual(
                [parse_store_item(item) for item in items],
                [
                    {
                        "fetched": 2_000_000_000.0,
                        "price": 999,
                        "price_text": "$9.99",
                        "tags": [4182, 1625, 1664, 3839, 5923],
                        "play_mode": "multiplayer",
                    },
                    {
                        "fetched": 2_000_000_000.0,
                        "price": 0,
                        "price_text": "Free",
                        "tags": [1663, 1774, 3859, 3878, 19],
                        "play_mode": "multiplayer",
                    },
                ],
            )

        cards = await store_cards(self.http, [620, 730], "en", "US")

        self.assertEqual(
            cards,
            {
                620: {
                    "name": "Portal 2",
                    "description": 'The "Perpetual Testing Initiative" has been expanded to allow you to design '
                    "co-op puzzles for you and your friends!",
                    "price_text": "$9.99",
                    "thumbnail": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/620/"
                    "bd23943dcd0280aa2cdc066e2f86d2185cfd4cce/capsule_231x87.jpg?t=1790187113",
                },
                730: {
                    "name": "Counter-Strike 2",
                    "description": "For over two decades, Counter-Strike has offered an elite competitive experience, "
                    "one shaped by millions of players from across the globe. And now the next chapter in the CS story "
                    "is about to begin. This is Counter-Strike 2.",
                    "price_text": "Free",
                    "thumbnail": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/730/"
                    "942c04efaa5bc87afb6f2a97dbf17ac614c8a84d/capsule_231x87.jpg?t=1791824795",
                },
            },
        )
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][:2], ("GET", "IStoreBrowseService/GetItems/v1"))
        self.assertEqual(
            json.loads(self.requests[0][2]["input_json"]),
            {
                "ids": [{"appid": 620}, {"appid": 730}],
                "context": {"language": "english", "country_code": "US"},
                "data_request": {"include_basic_info": True, "include_assets": True},
            },
        )
