import json
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.steam import fetch_family_snapshot, parse_players, parse_snapshot, parse_store_item, store_cards

FIXTURES = Path(__file__).parent / "fixtures/steam"
ALICE, BOB, CHARLIE = (f"7656119800000000{i}" for i in (1, 2, 3))


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class SteamResponseParsingTest(unittest.TestCase):
    def test_family_library_and_players_normalize_ids_ownership_and_defaults(self):
        snapshot = parse_snapshot(fixture("family"), fixture("library"), fixture("players"))

        self.assertEqual(snapshot["family_id"], "42")
        self.assertEqual(snapshot["name"], "Fixture Family")
        self.assertEqual(snapshot["members"], [ALICE, BOB, CHARLIE])
        self.assertEqual(snapshot["joined"], {ALICE: 1700000000, BOB: 1700000001, CHARLIE: None})
        self.assertEqual(snapshot["roles"], {ALICE: 1, BOB: 2, CHARLIE: 1})
        self.assertEqual(snapshot["free_spots"], 2)
        self.assertEqual(
            snapshot["pending_invites"],
            [{"steamid": "76561198000000004", "role": 2}, {"steamid": "76561198000000005", "role": 1}],
        )
        self.assertEqual(
            snapshot["apps"],
            [
                {
                    "appid": 10,
                    "name": "Fixture Game",
                    "owners": [ALICE, BOB],
                    "acquired": 1700000020,
                    "exclude_reason": 0,
                },
                {"appid": 20, "name": "App 20", "owners": [CHARLIE], "acquired": 0, "exclude_reason": 0},
                {"appid": 30, "name": "App 30", "owners": [], "acquired": 0, "exclude_reason": 0},
            ],
        )
        self.assertEqual(snapshot["names"], {ALICE: "Alice & Алиса", BOB: "Bob"})
        self.assertEqual(snapshot["playtime"], [])

    def test_player_links_handle_private_profiles_and_non_steam_game_ids(self):
        names, players = parse_players(fixture("players"))
        self.assertEqual(names, {ALICE: "Alice & Алиса", BOB: "Bob"})
        self.assertEqual(set(players), {ALICE, BOB, CHARLIE})
        self.assertEqual(
            players[ALICE],
            {
                "vanity": "fixture-alice",
                "online": True,
                "created": 1600000000,
                "last_online": 1700000030,
                "game": 10,
                "game_name": "Fixture Game",
            },
        )
        self.assertFalse(players[BOB]["online"])
        self.assertIsNone(players[BOB]["game"])
        self.assertEqual(players[BOB]["game_name"], "Fixture Shortcut")
        self.assertEqual(
            players[CHARLIE],
            {"vanity": None, "online": False, "created": None, "last_online": None, "game": None, "game_name": None},
        )

    def test_store_items_parse_price_weighted_tags_and_missing_purchase_details(self):
        paid, free, unavailable, *_ = fixture("store")["response"]["store_items"]
        with patch("bot.steam.time.time", return_value=2_000_000_000.0):
            self.assertEqual(
                parse_store_item(paid),
                {
                    "fetched": 2_000_000_000.0,
                    "price": 1234,
                    "price_text": "$12.34",
                    "tags": [2, 4, 6, 3, 5],
                    "multiplayer": True,
                },
            )
            self.assertEqual(parse_store_item(free)["price"], 0)
            self.assertEqual(parse_store_item(free)["price_text"], "Free")
            self.assertFalse(parse_store_item(free)["multiplayer"])
            self.assertIsNone(parse_store_item(unavailable)["price"])
            self.assertEqual(parse_store_item(unavailable)["tags"], [])


class SteamResponseHTTPTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = {
            "IFamilyGroupsService/GetFamilyGroupForUser/v1": fixture("family"),
            "IFamilyGroupsService/GetSharedLibraryApps/v1": fixture("library"),
            "IPlayerService/GetPlayerLinkDetails/v1": fixture("players"),
            "IFamilyGroupsService/GetPlaytimeSummary/v1": fixture("playtime"),
            "IStoreBrowseService/GetItems/v1": fixture("store"),
        }
        self.failures = {}
        self.requests = []

        async def respond(request):
            method = request.match_info["method"]
            params = dict(await request.post()) if request.method == "POST" else dict(request.query)
            self.requests.append((request.method, method, params))
            if method in self.failures:
                status, headers = self.failures[method]
                return web.Response(status=status, headers=headers)
            # Steam can return JSON under a non-JSON Content-Type.
            return web.Response(text=json.dumps(self.responses[method]), content_type="text/plain")

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

    async def test_snapshot_fetches_json_endpoints_and_post_only_playtime_summary(self):
        snapshot = await fetch_family_snapshot(self.http, "fixture-access")

        self.assertEqual(snapshot["members"], [ALICE, BOB, CHARLIE])
        self.assertEqual(snapshot["names"], {ALICE: "Alice & Алиса", BOB: "Bob"})
        self.assertEqual(
            snapshot["playtime"],
            [
                {"steamid": ALICE, "appid": 10, "seconds": 3600},
                {"steamid": BOB, "appid": 10, "seconds": 1800},
                {"steamid": CHARLIE, "appid": 20, "seconds": 0},
            ],
        )
        self.assertEqual(
            self.requests,
            [
                (
                    "GET",
                    "IFamilyGroupsService/GetFamilyGroupForUser/v1",
                    {"access_token": "fixture-access", "include_family_group_response": "true"},
                ),
                (
                    "GET",
                    "IFamilyGroupsService/GetSharedLibraryApps/v1",
                    {
                        "access_token": "fixture-access",
                        "family_groupid": "42",
                        "include_own": "true",
                        "include_excluded": "true",
                        "include_free": "false",
                        "include_non_games": "false",
                        "language": "english",
                    },
                ),
                (
                    "GET",
                    "IPlayerService/GetPlayerLinkDetails/v1",
                    {
                        "access_token": "fixture-access",
                        "steamids[0]": ALICE,
                        "steamids[1]": BOB,
                        "steamids[2]": CHARLIE,
                    },
                ),
                (
                    "POST",
                    "IFamilyGroupsService/GetPlaytimeSummary/v1",
                    {"access_token": "fixture-access", "family_groupid": "42"},
                ),
            ],
        )

    async def test_optional_profile_and_playtime_failures_preserve_family_and_library(self):
        for playtime_status, headers in ((503, {}), (200, {"x-eresult": "15", "x-error_message": "Access denied"})):
            with self.subTest(playtime_status=playtime_status):
                self.failures = {
                    "IPlayerService/GetPlayerLinkDetails/v1": (503, {}),
                    "IFamilyGroupsService/GetPlaytimeSummary/v1": (playtime_status, headers),
                }
                snapshot = await fetch_family_snapshot(self.http, "fixture-access")
                self.assertEqual(snapshot["members"], [ALICE, BOB, CHARLIE])
                self.assertEqual([a["appid"] for a in snapshot["apps"]], [10, 20, 30])
                self.assertEqual(snapshot["names"], {})
                self.assertEqual(snapshot["players"], {})
                self.assertEqual(snapshot["playtime"], [])

    async def test_required_library_http_failure_does_not_become_an_empty_snapshot(self):
        self.failures["IFamilyGroupsService/GetSharedLibraryApps/v1"] = (503, {})
        with self.assertRaisesRegex(RuntimeError, "GetSharedLibraryApps/v1 failed with HTTP 503"):
            await fetch_family_snapshot(self.http, "fixture-access")
        self.assertEqual(len(self.requests), 2)

    async def test_store_cards_parse_localized_request_assets_and_unavailable_items(self):
        cards = await store_cards(self.http, [10, 20, 30, 40], "ru", "KZ")

        self.assertEqual(
            cards,
            {
                10: {
                    "name": "Fixture Game",
                    "description": "A co-op fixture game.",
                    "price_text": "$12.34",
                    "thumbnail": "https://shared.akamai.steamstatic.com/store_item_assets/"
                    "apps/10/fixture/capsule_231x87.jpg",
                },
                20: {"name": "Fixture Free Game", "description": "", "price_text": "Free", "thumbnail": None},
                30: {"name": "Fixture Unavailable Game", "description": "", "price_text": None, "thumbnail": None},
            },
        )
        self.assertEqual(self.requests[0][:2], ("GET", "IStoreBrowseService/GetItems/v1"))
        self.assertEqual(
            json.loads(self.requests[0][2]["input_json"]),
            {
                "ids": [{"appid": 10}, {"appid": 20}, {"appid": 30}, {"appid": 40}],
                "context": {"language": "russian", "country_code": "KZ"},
                "data_request": {"include_basic_info": True, "include_assets": True},
            },
        )


if __name__ == "__main__":
    unittest.main()
