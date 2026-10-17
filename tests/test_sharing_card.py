import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.dialogs.games import game_getter
from bot.family import Family
from bot.inline import card_text


class InlineSharingCardTest(unittest.TestCase):
    def test_family_cards_distinguish_owned_shareable_and_available_copies(self):
        for reason, shareable in ((0, 2), (1, 0), (999, 0)):
            with self.subTest(reason=reason):
                text = card_text("en", "Game", "", None, ["Alice", "Bob"], "https://store/", reason)
                self.assertIn("Owned copies: 2", text)
                self.assertIn(f"Shareable copies: {shareable}", text)
                self.assertIn("Available now: unknown", text)
                self.assertNotIn("Your access:", text)

    def test_store_only_card_does_not_imply_family_copy_availability(self):
        text = card_text("en", "Game", "", None, [], "https://store/", 0)
        self.assertIn("Nobody in the family has it", text)
        self.assertNotIn("Owned copies:", text)
        self.assertNotIn("Shareable copies:", text)
        self.assertNotIn("Available now:", text)


class SharingCardTest(unittest.IsolatedAsyncioTestCase):
    async def card(self, reason, viewer="alice", lang="en", info=None):
        family = Family("9")
        if info is not None:
            family.stores["US"] = {"apps": {"620": info}}
        family.snapshot = {
            "apps": [{"appid": 620, "name": "Portal 2", "owners": ["alice", "bob"], "exclude_reason": reason}],
            "names": {"alice": "Alice", "bob": "Bob"},
        }
        manager = SimpleNamespace(
            dialog_data={"appid": 620},
            middleware_data={"family": family, "account": {"country": "US", "steam_id": viewer}},
        )
        db = SimpleNamespace(family_accounts=AsyncMock(return_value=[]))
        return (await game_getter(manager, family, db, lang))["details"]

    async def test_card_distinguishes_known_modes_from_missing_and_legacy_details(self):
        for info, mode in (
            (None, "❔ Mode unavailable"),
            ({}, "❔ Mode unavailable"),
            ({"multiplayer": False}, "❔ Mode unavailable"),
            ({"play_mode": None}, "❔ Mode unavailable"),
            ({"play_mode": "singleplayer"}, "👤 Single-player"),
            ({"play_mode": "multiplayer"}, "👥 Multiplayer"),
        ):
            with self.subTest(info=info):
                details = await self.card(0, info=info)
                self.assertIn(mode, details.splitlines())
                for other in {"❔ Mode unavailable", "👤 Single-player", "👥 Multiplayer"} - {mode}:
                    self.assertNotIn(other, details)

    async def test_russian_card_shows_unknown_mode(self):
        details = await self.card(0, lang="ru")
        self.assertIn("❔ Режим игры неизвестен", details)
        self.assertNotIn("👤 Одиночная", details)

    async def test_excluded_card_keeps_ownership_separate_from_sharing_and_availability(self):
        details = await self.card(1)
        self.assertIn("Owned copies: 2", details)
        self.assertIn("Shareable copies: 0", details)
        self.assertIn("Your access: own copy", details)
        self.assertIn("Not shareable: excluded by the publisher", details)
        self.assertIn("Available now: unknown", details)
        self.assertIn("Alice", details)
        self.assertIn("Bob", details)

    async def test_nonowner_access_depends_on_sharing_eligibility(self):
        included = await self.card(0, viewer="carol")
        self.assertIn("Shareable copies: 2", included)
        self.assertIn("Your access: family sharing", included)
        excluded = await self.card(1, viewer="carol")
        self.assertIn("Your access: unavailable through family sharing", excluded)

    async def test_future_exclusion_is_safe_and_shows_its_code(self):
        details = await self.card(999, viewer="carol")
        self.assertIn("Shareable copies: 0", details)
        self.assertIn("Steam restriction (code 999)", details)
        self.assertIn("unavailable through family sharing", details)

    async def test_russian_card_explains_exclusion_and_owned_access(self):
        details = await self.card(4, lang="ru")
        self.assertIn("Собственных копий: 2", details)
        self.assertIn("Копий для семейного доступа: 0", details)
        self.assertIn("Ваша копия", details)
        self.assertIn("приватная лицензия", details)
        self.assertIn("Свободно сейчас: неизвестно", details)
