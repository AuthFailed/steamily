import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.dialogs.games import game_getter
from bot.family import Family


class SharingCardTest(unittest.IsolatedAsyncioTestCase):
    async def card(self, reason, viewer="alice", lang="en"):
        family = Family("9")
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
