import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.dialogs.games import game_getter
from bot.family import Family
from bot.library import query
from bot.steam import parse_snapshot


class RecordedSharingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.library = json.loads((Path(__file__).parent / "fixtures/steam/shared_library_recorded.json").read_text())
        family = {
            "response": {
                "family_groupid": "9",
                "family_group": {"members": [{"steamid": member} for member in ("a", "b", "c")]},
            }
        }
        self.family = Family("9")
        self.family.snapshot = parse_snapshot(family, self.library, {})

    def test_recorded_exclusions_and_derived_optional_field_defaults(self):
        self.assertNotIn("exclude_reason", self.library["response"]["apps"][2])
        self.assertEqual(
            self.family.apps,
            [
                {
                    "appid": 201,
                    "name": "Recorded included title",
                    "owners": ["a", "b"],
                    "exclude_reason": 0,
                    "acquired": 1700000000,
                },
                {
                    "appid": 202,
                    "name": "Recorded excluded title",
                    "owners": ["a"],
                    "exclude_reason": 1,
                    "acquired": 1700000000,
                },
                {"appid": 203, "name": "App 203", "owners": ["a", "b"], "exclude_reason": 0, "acquired": 0},
            ],
        )

    def test_player_filter_keeps_excluded_game_only_for_its_owner(self):
        cases = [
            (["a"], [201, 202, 203]),
            (["b"], [201, 203]),
            (["c"], [201, 203]),
            (["a", "b"], [201, 203]),
            (["b", "c"], [201, 203]),
            (["a", "b", "c"], []),
        ]
        for players, expected in cases:
            with self.subTest(players=players):
                self.assertEqual(sorted(app["appid"] for app in query(self.family.apps, {}, players=players)), expected)

    async def test_cards_separate_ownership_sharing_and_unknown_live_availability(self):
        db = SimpleNamespace(family_accounts=AsyncMock(return_value=[]), game_responses=AsyncMock(return_value=[]))
        cases = [
            (202, "a", 1, 0, "own copy"),
            (202, "b", 1, 0, "unavailable through family sharing"),
            (201, "c", 2, 2, "family sharing"),
            (203, "c", 2, 2, "family sharing"),
        ]
        for appid, viewer, owned, shareable, access in cases:
            with self.subTest(appid=appid, viewer=viewer):
                manager = SimpleNamespace(
                    dialog_data={"appid": appid},
                    middleware_data={
                        "family": self.family,
                        "account": {
                            "country": "US",
                            "steam_id": viewer,
                            "telegram_id": 1,
                            "family_id": "9",
                            "game_response_version": "version",
                            "status": "approved",
                            "refresh_token": b"token",
                        },
                    },
                )
                db.family_accounts.return_value = [manager.middleware_data["account"]]
                details = (await game_getter(manager, self.family, db, "en"))["details"]
                self.assertIn(f"Owned copies: {owned}", details)
                self.assertIn(f"Shareable copies: {shareable}", details)
                self.assertIn(f"Your access: {access}", details)
                self.assertIn("Available now: unknown", details)
                if appid == 202:
                    self.assertIn("Not shareable: excluded by the publisher", details)
