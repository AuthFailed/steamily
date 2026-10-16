import unittest
from unittest.mock import AsyncMock, patch

from bot.family import Families
from bot.steam_auth import TokenRejected
from tests.pg import fresh_db, needs_pg

ALICE, BOB = "76561198000000001", "76561198000000002"


def snapshot(apps, family_id="9"):
    return {
        "family_id": family_id,
        "name": "Fam",
        "members": [ALICE, BOB],
        "names": {ALICE: "Alice", BOB: "Bob"},
        "apps": apps,
    }


@needs_pg
class FamiliesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        self.bot = AsyncMock()
        self.families = Families(self.db, http=None, bot=self.bot)
        self.patches = [
            patch("bot.family.update_store_cache", AsyncMock(return_value=False)),
            patch("bot.family.fetch_user_country", AsyncMock(return_value="KZ")),
            patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(return_value="access")),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.db.pool.close()

    async def test_first_registrant_owns_and_new_games_are_announced(self):
        portal = {"appid": 620, "name": "Portal 2", "owners": [ALICE], "acquired": 1}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([portal]))):
            await self.families.register(1, "refresh", ALICE)
            await self.families.register(2, "refresh", BOB)
        account = await self.db.account(1)
        self.assertEqual((account["status"], account["country"], account["owner_telegram_id"]), ("approved", "KZ", 1))
        self.assertEqual((await self.db.account(2))["status"], "pending")
        self.assertEqual(set(self.families.stores), {"KZ", "US"})  # prices for every member's currency
        self.bot.send_message.assert_not_called()  # the first check is a silent baseline

        await self.db.update_family("9", notify_chat=-100)
        cs = {"appid": 730, "name": "CS2", "owners": [BOB], "acquired": 2}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([portal, cs]))):
            await self.families.refresh("9")
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.args[0], -100)
        self.assertIn("CS2", self.bot.send_message.await_args.args[1])

    async def test_rejected_token_is_dropped_but_outage_keeps_it(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        with patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(side_effect=OSError("network down"))):
            with self.assertRaises(OSError):
                await self.families.refresh("9")
        self.assertIsNotNone((await self.db.account(1))["refresh_token"])
        with patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(side_effect=TokenRejected("revoked"))):
            await self.families.refresh("9")
        self.assertIsNone((await self.db.account(1))["refresh_token"])

    async def test_missing_country_is_filled_on_refresh(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
            await self.db.pool.execute("UPDATE accounts SET country = NULL")
            with patch("bot.family.fetch_user_country", AsyncMock(return_value="TR")):
                await self.families.refresh("9")
        self.assertEqual((await self.db.account(1))["country"], "TR")
        self.assertIn("TR", self.families.stores)

    async def test_owner_logout_hands_the_family_over(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
            await self.families.register(2, "refresh", BOB)
            await self.families.register(3, "refresh", "76561198000000003")
        await self.db.set_language(1, "ru")
        await self.db.set_status(3, "blocked")  # blocked members never inherit

        self.assertEqual(await self.families.leave(1), 2)
        self.assertIsNone(await self.db.account(1))
        self.assertIsNone(await self.db.language(1))  # settings are gone too
        new_owner = await self.db.account(2)
        self.assertEqual((new_owner["owner_telegram_id"], new_owner["status"]), (2, "approved"))

        self.assertIsNone(await self.families.leave(2))  # nobody left to inherit
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        await self.db.set_status(3, "pending")
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(4, "refresh", BOB)  # the next to register becomes owner
        self.assertEqual((await self.db.account(4))["owner_telegram_id"], 4)

    async def test_member_logout_keeps_the_owner(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
            await self.families.register(2, "refresh", BOB)
        self.assertIsNone(await self.families.leave(2))
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 1)

    async def test_presence_and_membership_alerts_go_to_the_group(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        await self.db.update_family("9", notify_chat=-100, alerts={"playing": True})
        sent = self.bot.send_message
        sent.reset_mock()
        self.families.presence["9"] = {ALICE: {"online": True, "game": None}}
        with patch(
            "bot.family.fetch_presence",
            AsyncMock(return_value={ALICE: {"online": True, "game": 620, "game_name": "Portal 2"}}),
        ):
            await self.families.check_presence("9")
        self.assertEqual(sent.await_args.args[:2], (-100, "🎮 Alice started playing Portal 2"))
        self.assertEqual(self.families.items["9"].snapshot["players"][ALICE]["game"], 620)

        sent.reset_mock()
        moved = {**snapshot([]), "members": [ALICE]}  # BOB left; "members" alerts are on by default
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=moved)):
            await self.families.refresh("9")
        self.assertIn("left the Steam family", sent.await_args.args[1])

    async def test_member_who_moved_family_is_not_used(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([], family_id="other"))):
            await self.families.refresh("9")
        self.assertEqual((await self.db.family("9"))["state"]["ownership"], {})


if __name__ == "__main__":
    unittest.main()
