import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bot import library
from bot.family import role
from bot.library import (
    added_per_month,
    borrowed_hours_per_game,
    borrowed_hours_per_member,
    currency,
    exclusive_per_member,
    family_events,
    family_matches,
    format_event,
    games_per_member,
    presence_events,
    query,
    reconcile,
    summary,
    tag_counts,
    value_per_member,
)
from bot.steam import NoFamily, fetch_family_snapshot, get_json, parse_snapshot, parse_store_item
from bot.steam_auth import decode, encode, expires_within


def app(appid, name, owners, acquired=0):
    return {"appid": appid, "name": name, "owners": owners, "acquired": acquired}


class ReconcileTest(unittest.TestCase):
    def test_first_snapshot_is_a_silent_baseline(self):
        events, state = reconcile(None, {"members": ["alice"], "apps": [app(10, "CS", ["alice"])]})
        self.assertEqual(events, [])
        self.assertEqual(state, {"members": ["alice"], "ownership": {"10": ["alice"]}})

    def test_new_game_and_new_copy_are_announced(self):
        previous = {"members": ["alice", "bob"], "ownership": {"620": ["alice"]}}
        events, _ = reconcile(
            previous,
            {"members": ["alice", "bob"], "apps": [app(620, "Portal 2", ["alice", "bob"]), app(10, "CS", ["alice"])]},
        )
        self.assertEqual(
            events, [{"appid": 620, "name": "Portal 2", "owner": "bob"}, {"appid": 10, "name": "CS", "owner": "alice"}]
        )

    def test_new_member_library_is_not_announced(self):
        previous = {"members": ["alice"], "ownership": {"10": ["alice"]}}
        events, state = reconcile(previous, {"members": ["alice", "bob"], "apps": [app(620, "Portal 2", ["bob"])]})
        self.assertEqual(events, [])
        self.assertEqual(state["ownership"]["620"], ["bob"])

    def test_history_survives_transient_omissions(self):
        previous = {"members": ["alice"], "ownership": {"620": ["alice"]}}
        _, missing = reconcile(previous, {"members": ["alice"], "apps": []})
        events, restored = reconcile(missing, {"members": ["alice"], "apps": [app(620, "Portal 2", ["alice"])]})
        self.assertEqual(events, [])
        self.assertEqual(restored["ownership"]["620"], ["alice"])


STORE = {
    "tag_names": {"19": "Action", "1685": "Co-op"},
    "apps": {
        "1": {"price": 0, "tags": [19], "play_mode": "multiplayer"},
        "2": {"price": 1999, "tags": [19, 1685], "play_mode": "multiplayer"},
        "3": {"price": 299, "tags": [], "play_mode": "singleplayer"},
    },
}
APPS = [
    app(1, "Alpha Strike", ["a"], 30),
    app(2, "Beta Co-op", ["a", "b"], 10),
    app(3, "Gamma", ["c"], 20),
    app(4, "Delisted", ["a", "b", "c"], 5),
]


def names(games):
    return [g["name"] for g in games]


class QueryTest(unittest.TestCase):
    def test_search_matches_all_words_case_insensitively(self):
        self.assertEqual(names(query(APPS, STORE, search="co BETA")), ["Beta Co-op"])

    def test_genre_price_and_multiplayer_filters(self):
        self.assertEqual(names(query(APPS, STORE, tag="1685")), ["Beta Co-op"])
        self.assertEqual(names(query(APPS, STORE, price="free")), ["Alpha Strike"])
        self.assertEqual(names(query(APPS, STORE, multiplayer=True)), ["Alpha Strike", "Beta Co-op"])

    def test_unknown_play_modes_are_included_unless_multiplayer_is_required(self):
        apps = APPS + [app(5, "Unknown", ["a"])]
        store = {"apps": {**STORE["apps"], "5": {"play_mode": None}}}

        self.assertEqual(names(query(apps, store)), ["Alpha Strike", "Beta Co-op", "Delisted", "Gamma", "Unknown"])
        self.assertEqual(names(query(apps, store, multiplayer=True)), ["Alpha Strike", "Beta Co-op"])

    def test_budget_is_inclusive_and_excludes_unknown_prices(self):
        for budget, expected in (
            (0, ["Alpha Strike"]),
            (298, ["Alpha Strike"]),
            (299, ["Alpha Strike", "Gamma"]),
            (1999, ["Alpha Strike", "Beta Co-op", "Gamma"]),
        ):
            with self.subTest(budget=budget):
                self.assertEqual(names(query(APPS, STORE, price=budget)), expected)

    def test_budget_uses_local_prices_without_currency_conversion(self):
        store = {"apps": {"1": {"price": 0}, "2": {"price": 100000}, "3": {"price": 100001}}}
        self.assertEqual(names(query(APPS, store, price=100000)), ["Alpha Strike", "Beta Co-op"])
        self.assertEqual(names(query(APPS, store, price=99999)), ["Alpha Strike"])

    def test_missing_prices_are_only_shown_without_a_price_filter(self):
        self.assertEqual(names(query(APPS, {}, price="any")), ["Alpha Strike", "Beta Co-op", "Delisted", "Gamma"])
        self.assertEqual(query(APPS, {}, price="free"), [])
        self.assertEqual(query(APPS, {}, price=100000), [])

    def test_players_need_enough_copies(self):
        self.assertEqual(names(query(APPS, STORE, players=["a", "b"])), ["Beta Co-op", "Delisted"])
        self.assertEqual(names(query(APPS, STORE, players=["a", "b", "c"])), ["Delisted"])

    def test_sorting_both_directions_with_unknown_prices_last(self):
        self.assertEqual(names(query(APPS, STORE, sort="price")), ["Alpha Strike", "Gamma", "Beta Co-op", "Delisted"])
        self.assertEqual(
            names(query(APPS, STORE, sort="price", desc=True)), ["Beta Co-op", "Gamma", "Alpha Strike", "Delisted"]
        )
        self.assertEqual(
            names(query(APPS, STORE, sort="copies", desc=True)), ["Delisted", "Beta Co-op", "Alpha Strike", "Gamma"]
        )  # ties by name
        self.assertEqual(names(query(APPS, STORE, sort="copies"))[:2], ["Alpha Strike", "Gamma"])
        self.assertEqual(
            names(query(APPS, STORE, sort="added", desc=True)), ["Alpha Strike", "Gamma", "Beta Co-op", "Delisted"]
        )
        self.assertEqual(names(query(APPS, STORE, sort="name", desc=True))[0], "Gamma")

    def test_tag_counts(self):
        self.assertEqual(tag_counts(APPS, STORE), [("19", 2), ("1685", 1)])


class BudgetParsingTest(unittest.TestCase):
    def test_budget_uses_exact_integer_hundredths(self):
        for text, expected in (
            ("0", 0),
            ("0.29", 29),
            ("5", 500),
            ("5.5", 550),
            (" 1000,50 ", 100050),
            ("999999999999.99", 99999999999999),
        ):
            with self.subTest(text=text):
                self.assertEqual(library.parse_budget(text), expected)

    def test_invalid_budget_is_rejected(self):
        for text in (
            "",
            " ",
            "-1",
            "+1",
            "1e2",
            "NaN",
            "Infinity",
            "1 000",
            "1_000",
            "$10",
            ".50",
            "1.",
            "1.234",
            "1,234",
            "1,000.00",
            "١٢",
            "1000000000000",
            "9" * 4096,
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                library.parse_budget(text)


class SteamParsingTest(unittest.TestCase):
    def test_parse_snapshot(self):
        family = {
            "response": {
                "family_groupid": "9",
                "family_group": {
                    "name": "Fam",
                    "members": [{"steamid": "1", "time_joined": 200}, {"steamid": "2", "time_joined": 100}],
                },
            }
        }
        library = {
            "response": {
                "apps": [{"appid": 620, "owner_steamids": ["2", "1", "2"], "rt_time_acquired": 5}, {"appid": 7}]
            }
        }
        players = {"response": {"accounts": [{"public_data": {"steamid": "1", "persona_name": "Alice"}}]}}
        snapshot = parse_snapshot(family, library, players)
        self.assertEqual(snapshot["members"], ["1", "2"])
        self.assertEqual((snapshot["family_id"], snapshot["name"]), ("9", "Fam"))
        self.assertEqual(snapshot["joined"], {"1": 200, "2": 100})
        self.assertEqual(snapshot["names"], {"1": "Alice"})
        self.assertEqual(
            snapshot["apps"][0],
            {"appid": 620, "name": "App 620", "owners": ["1", "2"], "acquired": 5, "exclude_reason": 0},
        )
        self.assertEqual(snapshot["apps"][1]["owners"], [])
        self.assertNotIn("fetched_at", snapshot)

    def test_missing_members_are_an_incomplete_response(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete family response"):
            parse_snapshot({"response": {"family_groupid": "9"}}, {"response": {"apps": []}}, {})

    def test_parse_store_item(self):
        item = {
            "appid": 620,
            "tags": [{"tagid": 1, "weight": 5}, {"tagid": 2, "weight": 50}],
            "categories": {"supported_player_categoryids": [2, 9]},
            "best_purchase_option": {"final_price_in_cents": "999", "formatted_final_price": "$9.99"},
        }
        info = parse_store_item(item)
        self.assertEqual(
            (info["price"], info["price_text"], info["tags"], info["play_mode"]),
            (999, "$9.99", [2, 1], "multiplayer"),
        )
        self.assertEqual(parse_store_item({"is_free": True})["price"], 0)
        self.assertIsNone(parse_store_item({})["price"])

    def test_store_categories_preserve_missing_fields_and_explicit_empty_lists(self):
        for categories in (
            None,
            {},
            {"supported_player_categoryids": []},
            {"feature_categoryids": []},
            {"supported_player_categoryids": [2, 38, 39], "feature_categoryids": [44], "controller_categoryids": [28]},
        ):
            with self.subTest(categories=categories):
                parsed = parse_store_item({} if categories is None else {"categories": categories})
                self.assertIn("categories", parsed)
                self.assertEqual(parsed["categories"], categories)

    def test_play_mode_requires_supported_player_categories(self):
        for categories, expected in (
            ([1], "multiplayer"),
            ([9], "multiplayer"),
            ([2, 9], "multiplayer"),
            ([2], "singleplayer"),
            ([], None),
            ([999], None),
        ):
            with self.subTest(categories=categories):
                item = {"categories": {"supported_player_categoryids": categories}}
                self.assertEqual(parse_store_item(item)["play_mode"], expected)

        for item in ({}, {"categories": {}}, {"categories": {"feature_categoryids": [2]}}):
            with self.subTest(item=item):
                self.assertIsNone(parse_store_item(item)["play_mode"])


class SteamFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_timestamp_is_recorded_after_optional_fetch_finishes(self):
        family = {"response": {"family_groupid": "9", "family_group": {"members": [{"steamid": "1"}]}}}
        for failed_playtime in (False, True):
            with self.subTest(failed_playtime=failed_playtime), patch("bot.steam.time.time", return_value=100) as now:

                async def playtime(*args, failed=failed_playtime):
                    now.return_value = 200
                    if failed:
                        raise RuntimeError("Steam HTTP 503")
                    return {"response": {"entries": []}}

                with (
                    patch("bot.steam.get_json", AsyncMock(side_effect=[family, {"response": {"apps": []}}, {}])),
                    patch("bot.steam.post_json", side_effect=playtime),
                ):
                    snapshot = await fetch_family_snapshot(None, "access")
                self.assertEqual(snapshot.get("fetched_at"), 200)

    async def test_explicit_no_family_is_confirmed_before_fetching_the_library(self):
        get = AsyncMock(return_value={"response": {"is_not_member_of_any_group": True}})
        with patch("bot.steam.get_json", get), self.assertRaises(NoFamily):
            await fetch_family_snapshot(None, "access")
        self.assertEqual(get.await_count, 1)

    async def test_missing_membership_data_is_not_confirmed_departure(self):
        for response in (
            {},
            {"response": {}},
            {"response": {"family_groupid": "9"}},
            {"response": {"family_group": {"members": []}}},
            {"response": {"is_not_member_of_any_group": False}},
            {"response": {"is_not_member_of_any_group": "true"}},
        ):
            with self.subTest(response=response), patch("bot.steam.get_json", AsyncMock(return_value=response)):
                with self.assertRaisesRegex(RuntimeError, "incomplete family response"):
                    await fetch_family_snapshot(None, "access")

    async def test_temporary_failure_is_not_confirmed_departure(self):
        with patch("bot.steam.get_json", AsyncMock(side_effect=RuntimeError("Steam HTTP 503"))):
            with self.assertRaisesRegex(RuntimeError, "Steam HTTP 503"):
                await fetch_family_snapshot(None, "access")

    async def test_http_and_steam_result_errors_are_rejected_before_reading_json(self):
        for status, eresult in ((503, "1"), (200, "2")):
            with self.subTest(status=status, eresult=eresult):
                response = AsyncMock()
                response.status, response.headers = status, {"x-eresult": eresult}
                http = Mock()
                http.get.return_value.__aenter__ = AsyncMock(return_value=response)
                http.get.return_value.__aexit__ = AsyncMock(return_value=False)
                with self.assertRaises(RuntimeError):
                    await get_json(http, "IFamilyGroupsService/GetFamilyGroupForUser/v1", {})
                response.json.assert_not_awaited()


class AuthTest(unittest.TestCase):
    def test_protobuf_roundtrip(self):
        data = encode([(1, "token"), (2, ("fixed64", 76561198000000000)), (3, 1), (4, -500)])
        decoded = decode(data)
        self.assertEqual(decoded[1], b"token")
        self.assertEqual(int.from_bytes(decoded[2], "little"), 76561198000000000)
        self.assertEqual(decoded[3], 1)
        self.assertEqual(decoded[4], (1 << 64) - 500)  # negative int32 is sent as 10-byte varint

    def test_token_expiry(self):
        def jwt(payload):
            return "x." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".y"

        self.assertFalse(expires_within(jwt({"exp": time.time() + 3600}), 300))
        self.assertTrue(expires_within(jwt({"exp": time.time() + 60}), 300))
        self.assertTrue(expires_within(None, 300))
        self.assertTrue(expires_within("garbage", 300))


class AccessTest(unittest.TestCase):
    def test_role(self):
        def account(telegram_id, status, owner=1):
            return {"telegram_id": telegram_id, "status": status, "owner_telegram_id": owner}

        self.assertEqual(role(None), "none")
        self.assertEqual(role(account(1, "approved")), "owner")
        self.assertEqual(role(account(1, "blocked")), "blocked")
        self.assertEqual(role(account(2, "approved")), "member")
        self.assertEqual(role(account(2, "pending")), "pending")
        self.assertEqual(role(account(2, "approved", owner=None)), "member")  # family without an owner


class AlertsTest(unittest.TestCase):
    def test_presence_events(self):
        old = {"a": {"online": False, "game": None}, "b": {"online": True, "game": 10, "game_name": "CS"}}
        new = {
            "a": {"online": True, "game": 620, "game_name": "Portal 2"},
            "b": {"online": True, "game": None},
            "c": {"online": True, "game": 5},
        }  # c is new: no alerts yet
        self.assertEqual(
            presence_events(old, new),
            [
                {"kind": "online", "type": "online", "steamid": "a"},
                {"kind": "playing", "type": "started", "steamid": "a", "appid": 620, "game": "Portal 2"},
                {"kind": "playing", "type": "stopped", "steamid": "b", "appid": 10, "game": "CS"},
            ],
        )

    def test_family_events_and_texts(self):
        old = {"members": ["a", "b"], "apps": APPS}
        new = {"members": ["a", "c"], "apps": APPS[:3]}
        events = family_events(old, new)
        self.assertEqual(
            [(e["type"], e.get("steamid") or e.get("game")) for e in events],
            [("joined", "c"), ("left", "b"), ("removed", "Delisted")],
        )
        self.assertEqual(format_event(events[1], {"b": "Bob"}, APPS, "ru"), "🚪 Bob покинул семью Steam")
        self.assertEqual(
            format_event({"type": "started", "steamid": "x", "appid": 3}, {}, APPS, "en"),
            "🎮 Steam user x started playing Gamma",
        )

    def test_family_matches(self):
        self.assertEqual(family_matches(APPS, "beta"), [2])
        self.assertEqual(family_matches(APPS, "gama"), [3])  # typo
        self.assertEqual(family_matches(APPS, ""), [1, 3, 2, 4])  # newest first
        self.assertEqual(family_matches(APPS, "zzz"), [])


class StatsTest(unittest.TestCase):
    def test_family_stats(self):
        self.assertEqual(summary(APPS, STORE), {"games": 4, "copies": 7, "shared": 2, "value": 22.98, "unpriced": 1})
        self.assertEqual(currency({"apps": {"1": {"price": 999, "price_text": "1 999₸"}}}), "₸")
        self.assertEqual(currency({"apps": {"1": {"price": 999, "price_text": "$9.99"}}}), "$")
        self.assertEqual(games_per_member(APPS, ["a", "c"]), {"a": 3, "c": 2})
        self.assertEqual(value_per_member(APPS, STORE, ["a", "b"]), {"a": 19.99, "b": 19.99})
        self.assertEqual(exclusive_per_member(APPS, ["a", "c"]), {"a": 1, "c": 1})
        added = added_per_month([app(1, "x", ["a"], 1_740_000_000)], months=3, now=1_745_000_000)
        self.assertEqual(added, [("2025-02", 1), ("2025-03", 0), ("2025-04", 0)])
        playtime = [
            {"steamid": "b", "appid": 1, "seconds": 7200},
            {"steamid": "c", "appid": 1, "seconds": 3600},
            {"steamid": "b", "appid": 3, "seconds": 1800},
        ]
        self.assertEqual(borrowed_hours_per_game(playtime, APPS), [("Alpha Strike", 3.0), ("Gamma", 0.5)])
        self.assertEqual(borrowed_hours_per_member(playtime, ["b", "c", "d"]), {"b": 2.5, "c": 1.0, "d": 0.0})

    def test_games_we_both_have(self):
        self.assertEqual(names(query(APPS, STORE, owners=["a", "b"])), ["Beta Co-op", "Delisted"])


if __name__ == "__main__":
    unittest.main()
