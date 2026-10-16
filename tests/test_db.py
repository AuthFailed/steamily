import unittest

from aiogram.fsm.storage.base import StorageKey

from bot.db import SCHEMA, PgStorage
from bot.states import Menu
from tests.pg import fresh_db, needs_pg


@needs_pg
class DbTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        await self.db.ensure_family("9", "Fam")
        await self.db.update_family("9", state={"members": ["1"], "ownership": {}}, notify_chat=-100)

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def test_refresh_token_is_encrypted_and_status_survives_relogin(self):
        await self.db.save_account(10, "1", "9", "secret-token", "KZ")
        raw = await self.db.pool.fetchval("SELECT refresh_token FROM accounts WHERE telegram_id = 10")
        self.assertNotIn(b"secret-token", raw)
        account = await self.db.account(10)
        self.assertEqual(self.db.refresh_token(account), "secret-token")
        self.assertEqual((account["status"], account["country"]), ("pending", "KZ"))

        await self.db.set_status(10, "approved")
        await self.db.save_account(10, "1", "9", "new-token")  # same Steam account: keeps approval
        self.assertEqual((await self.db.account(10))["status"], "approved")
        await self.db.save_account(10, "2", "9", "other")  # different Steam account: back to pending
        self.assertEqual((await self.db.account(10))["status"], "pending")

        await self.db.set_refresh_token(10, None)
        self.assertIsNone(self.db.refresh_token(await self.db.account(10)))

    async def test_family_settings_and_store_cache(self):
        family = await self.db.family("9")
        self.assertEqual((family["name"], family["notify_chat"], family["language"]), ("Fam", -100, "en"))
        self.assertEqual(family["state"], {"members": ["1"], "ownership": {}})
        await self.db.ensure_family("9", "Renamed")  # existing settings stay
        await self.db.update_family("9", language="ru", alerts={"online": True})
        family = await self.db.family("9")
        self.assertEqual(
            (family["name"], family["notify_chat"], family["language"], family["alerts"]),
            ("Renamed", -100, "ru", {"online": True}),
        )
        with self.assertRaises(AssertionError):
            await self.db.update_family("9", owner="evil")

        self.assertEqual(await self.db.store_cache("DE"), {})
        await self.db.save_store_cache("DE", {"apps": {"620": {"price": 999}}})
        self.assertEqual(await self.db.store_cache("DE"), {"apps": {"620": {"price": 999}}})

    async def test_changing_family_requires_new_approval(self):
        await self.db.ensure_family("other", "Other")
        for status in ("approved", "blocked"):
            with self.subTest(status=status):
                await self.db.save_account(10, "1", "9", "old")
                await self.db.set_status(10, status)
                await self.db.save_account(10, "1", "9", "same-family")
                self.assertEqual((await self.db.account(10))["status"], status)
                await self.db.save_account(10, "1", "other", "new-family")
                self.assertEqual((await self.db.account(10))["status"], "pending")

    async def test_token_and_renewal_time_are_saved_together(self):
        await self.db.save_account(10, "1", "9", "first", "KZ", token_renewed_at=100_000.0)
        await self.db.set_refresh_token(10, "rotated", 186_400.0)
        account = await self.db.account(10)
        self.assertEqual((self.db.refresh_token(account), account["token_renewed_at"]), ("rotated", 186_400.0))
        self.assertNotIn(b"rotated", account["refresh_token"])

        await self.db.save_account(10, "1", "9", "relogin", token_renewed_at=186_400.0)
        account = await self.db.account(10)
        self.assertEqual((self.db.refresh_token(account), account["token_renewed_at"]), ("relogin", 186_400.0))
        await self.db.set_refresh_token(10, None)
        account = await self.db.account(10)
        self.assertEqual((self.db.refresh_token(account), account["token_renewed_at"]), (None, 0.0))

    async def test_renewal_schema_upgrade_keeps_existing_accounts(self):
        await self.db.save_account(10, "1", "9", "legacy", "KZ")
        await self.db.set_status(10, "approved")
        await self.db.pool.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS token_renewed_at")
        await self.db.pool.execute(SCHEMA)
        await self.db.pool.execute(SCHEMA)
        account = await self.db.account(10)
        self.assertEqual(account["token_renewed_at"], 0.0)
        self.assertEqual(
            (self.db.refresh_token(account), account["country"], account["status"]), ("legacy", "KZ", "approved")
        )

    async def test_fsm_storage(self):
        storage = PgStorage(self.db)
        key = StorageKey(bot_id=1, chat_id=2, user_id=3)
        self.assertIsNone(await storage.get_state(key))
        self.assertEqual(await storage.get_data(key), {})
        await storage.set_state(key, Menu.main)
        await storage.set_data(key, {"stack": ["a"]})
        self.assertEqual(await storage.get_state(key), Menu.main.state)
        self.assertEqual(await storage.get_data(key), {"stack": ["a"]})
        await storage.set_state(key, None)
        self.assertIsNone(await storage.get_state(key))
        self.assertEqual(await storage.get_data(key), {"stack": ["a"]})


if __name__ == "__main__":
    unittest.main()
