import unittest

import asyncpg
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

    async def test_reader_schema_upgrade_preserves_login_and_reader_selection(self):
        await self.db.save_account(10, "1", "9", "existing-login", "KZ")
        await self.db.set_status(10, "approved")
        await self.db.pool.execute("ALTER TABLE families DROP COLUMN IF EXISTS reader_telegram_id")
        await self.db.pool.execute(SCHEMA)
        self.assertIsNone((await self.db.family("9"))["reader_telegram_id"])
        await self.db.update_family("9", reader_telegram_id=10)
        await self.db.pool.execute(SCHEMA)
        self.assertEqual((await self.db.family("9"))["reader_telegram_id"], 10)
        account = await self.db.account(10)
        self.assertEqual((self.db.refresh_token(account), account["status"]), ("existing-login", "approved"))

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

    async def test_stale_session_cannot_overwrite_or_clear_a_new_login(self):
        await self.db.save_account(10, "1", "9", "old")
        old_generation = (await self.db.account(10))["token_generation"]
        await self.db.save_account(10, "1", "9", "new")
        current = await self.db.account(10)
        self.assertGreater(current["token_generation"], old_generation)
        for token in ("late-rotation", None):
            self.assertFalse(await self.db.set_refresh_token(10, token, expected_generation=old_generation))
            self.assertEqual(self.db.refresh_token(await self.db.account(10)), "new")
        self.assertTrue(
            await self.db.set_refresh_token(10, "current-rotation", expected_generation=current["token_generation"])
        )
        self.assertEqual((await self.db.account(10))["token_generation"], current["token_generation"])

    async def test_first_login_and_owner_claim_commit_together(self):
        await self.db.save_account(10, "1", "9", "first", claim_owner=True)
        self.assertEqual((await self.db.account(10))["status"], "approved")
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 10)
        await self.db.save_account(20, "2", "9", "second", claim_owner=True)
        self.assertEqual((await self.db.account(20))["status"], "pending")
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 10)

    async def test_rejected_generation_cannot_be_republished_by_a_late_rotation(self):
        await self.db.save_account(10, "1", "9", "old")
        generation = (await self.db.account(10))["token_generation"]
        self.assertTrue(await self.db.set_refresh_token(10, None, expected_generation=generation))
        self.assertFalse(await self.db.set_refresh_token(10, "late", expected_generation=generation))
        self.assertIsNone(self.db.refresh_token(await self.db.account(10)))

    async def test_logout_and_recreation_do_not_reuse_the_credential_generation(self):
        await self.db.save_account(10, "1", "9", "old")
        old_generation = (await self.db.account(10))["token_generation"]
        await self.db.delete_user(10)
        await self.db.save_account(10, "1", "9", "replacement")
        self.assertNotEqual((await self.db.account(10))["token_generation"], old_generation)
        self.assertFalse(await self.db.set_refresh_token(10, None, expected_generation=old_generation))
        self.assertEqual(self.db.refresh_token(await self.db.account(10)), "replacement")

    async def test_failed_owner_claim_rolls_back_the_new_account(self):
        await self.db.pool.execute("""
            CREATE FUNCTION reject_owner() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'owner update unavailable'; END $$;
            CREATE TRIGGER owner_failure BEFORE UPDATE OF owner_telegram_id ON families
            FOR EACH ROW EXECUTE FUNCTION reject_owner();
        """)
        with self.assertRaises(asyncpg.RaiseError):
            await self.db.save_account(10, "1", "9", "must-not-commit", claim_owner=True)
        self.assertIsNone(await self.db.account(10))

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
