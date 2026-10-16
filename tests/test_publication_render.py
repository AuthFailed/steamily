import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.dialogs import admin
from bot.dialogs.family import info_getter, member_getter, members_getter
from bot.family import Family
from bot.i18n import t
from bot.states import Admin


class PublicationRenderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.family = Family("9")
        self.family.snapshot = {
            "name": "Family",
            "members": ["alice", "bob"],
            "names": {"alice": "Alice", "bob": "Bob"},
            "apps": [],
            "players": {
                "alice": {"online": True},
                "bob": {"online": True, "last_online": 1_790_000_000},
            },
        }
        self.accounts = [
            {
                "steam_id": steam_id,
                "family_id": "9",
                "telegram_id": telegram_id,
                "owner_telegram_id": 1,
                "status": "approved",
                "refresh_token": "token",
                "country": "US",
                "tg_username": f"{steam_id}_tg",
                "tg_name": steam_id,
                "publish_presence": False,
                "publish_telegram": False,
            }
            for steam_id, telegram_id in (("alice", 1), ("bob", 2))
        ]
        self.account = self.accounts[0]
        self.db = SimpleNamespace(
            owner_steam_id=AsyncMock(return_value="alice"),
            family_accounts=AsyncMock(side_effect=lambda _: [a.copy() for a in self.accounts]),
        )
        self.manager = SimpleNamespace(
            middleware_data={"family": self.family, "account": self.account},
            dialog_data={"member": "bob"},
        )
        self.families = SimpleNamespace(member_stats=AsyncMock(return_value={}))

    async def card(self):
        return await member_getter(self.manager, self.family, self.families, self.account, self.db, "en")

    async def test_hidden_presence_and_self_view(self):
        info = await info_getter(self.manager, self.family, self.account, self.db, "en")
        self.assertIn("🟢", info["members_list"].splitlines()[0])
        self.assertNotIn("🟢", info["members_list"].splitlines()[1])
        members = await members_getter(self.family, account=self.account, db=self.db)
        self.assertEqual(members["members"], [("alice", "🟢 Alice"), ("bob", "Bob")])
        for online in (True, False):
            self.family.snapshot["players"]["bob"]["online"] = online
            self.assertEqual((await self.card())["status"], t("en", "private"))
        self.manager.dialog_data["member"] = "alice"
        self.assertEqual((await self.card())["status"], t("en", "online"))

    async def test_opted_in_presence_is_visible(self):
        self.accounts[1]["publish_presence"] = True
        info = await info_getter(self.manager, self.family, self.account, self.db, "en")
        self.assertIn("🟢", info["members_list"].splitlines()[1])
        members = await members_getter(self.family, account=self.account, db=self.db)
        self.assertEqual(members["members"][1], ("bob", "🟢 Bob"))
        self.assertEqual((await self.card())["status"], t("en", "online"))
        self.family.snapshot["players"]["bob"]["online"] = False
        self.assertEqual((await self.card())["status"], t("en", "last_online", when="21.09.2026"))

    async def test_opt_out_during_stats_fetch_is_respected(self):
        self.accounts[1].update(publish_presence=True, publish_telegram=True)

        async def stats(*args):
            self.accounts[1].update(publish_presence=False, publish_telegram=False)
            return {}

        self.families.member_stats.side_effect = stats
        card = await self.card()
        self.assertEqual(card["status"], t("en", "private"))
        self.assertNotIn("bob_tg", card["links"])

    async def test_admin_card_masks_hidden_telegram_identifier(self):
        self.manager.dialog_data["telegram_id"] = 2
        self.db.account = AsyncMock(return_value=self.accounts[1])
        card = await admin.member_getter(self.manager, self.db, self.family, "en")
        self.assertEqual(card["telegram"], t("en", "private"))
        self.accounts[1]["publish_telegram"] = True
        card = await admin.member_getter(self.manager, self.db, self.family, "en")
        self.assertEqual(card["telegram"], 2)
        for status in ("pending", "blocked"):
            self.accounts[1]["status"] = status
            card = await admin.member_getter(self.manager, self.db, self.family, "en")
            self.assertEqual(card["telegram"], t("en", "private"))

    async def test_admin_member_callbacks_resolve_public_steam_identity(self):
        members = await admin.members_getter(self.db, self.family, self.account)
        self.assertEqual(members["accounts"], [("bob", "✅ Bob")])
        self.manager.middleware_data["db"] = self.db
        self.manager.switch_to = AsyncMock()
        await admin.on_member(None, None, self.manager, "bob")
        self.assertEqual(self.manager.dialog_data["telegram_id"], 2)
        self.manager.switch_to.assert_awaited_once_with(Admin.member)

    async def test_admin_vanished_member_clears_previous_target(self):
        self.manager.dialog_data["telegram_id"] = 1
        self.manager.middleware_data["db"] = self.db
        self.manager.switch_to = AsyncMock()
        self.accounts.pop()
        await admin.on_member(None, None, self.manager, "bob")
        self.assertNotIn("telegram_id", self.manager.dialog_data)
        self.manager.switch_to.assert_awaited_once_with(Admin.members)

    async def test_admin_old_telegram_callback_cannot_select_an_account(self):
        self.manager.dialog_data["telegram_id"] = 1
        self.manager.middleware_data["db"] = self.db
        self.manager.switch_to = AsyncMock()
        await admin.on_member(None, None, self.manager, "2")
        self.assertNotIn("telegram_id", self.manager.dialog_data)
        self.manager.switch_to.assert_awaited_once_with(Admin.members)


if __name__ == "__main__":
    unittest.main()
