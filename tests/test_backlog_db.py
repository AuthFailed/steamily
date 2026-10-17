import asyncio
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from bot.db import Db
from bot.migrations import MIGRATIONS, migrate
from tests.pg import TEST_DATABASE_URL, fresh_db, needs_pg


@needs_pg
class BacklogDbTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        await self.db.ensure_family("9", "Family")
        await self.db.save_account(10, "1", "9", "token")
        await self.db.set_status(10, "approved")

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def test_status_transitions_replace_one_mark_and_clear_is_idempotent(self):
        self.assertEqual(await self.db.game_marks(10), {})
        for status in ("backlog", "playing", "completed", "backlog"):
            self.assertTrue(await self.db.set_game_status(10, "1", "9", 440, "Team Fortress 2", status))
            self.assertEqual(await self.db.game_marks(10), {440: {"name": "Team Fortress 2", "status": status}})
        self.assertTrue(await self.db.set_game_status(10, "1", "9", 440, "Updated name", "completed"))
        self.assertEqual(await self.db.game_marks(10), {440: {"name": "Updated name", "status": "completed"}})
        for _ in range(2):
            self.assertTrue(await self.db.set_game_status(10, "1", "9", 440, "Updated name", None))
            self.assertEqual(await self.db.game_marks(10), {})

    async def test_marks_are_personal_and_survive_database_reconnect(self):
        await self.db.save_account(20, "2", "9", "token")
        await self.db.set_status(20, "approved")
        await self.db.set_game_status(10, "1", "9", 440, "Team Fortress 2", "playing")
        await self.db.set_game_status(20, "2", "9", 440, "Team Fortress 2", "backlog")
        await self.db.set_game_status(10, "1", "9", 570, "Dota 2", "completed")
        await self.db.pool.close()
        self.db = await Db.connect(TEST_DATABASE_URL, Fernet.generate_key().decode())
        self.assertEqual(
            await self.db.game_marks(10),
            {440: {"name": "Team Fortress 2", "status": "playing"}, 570: {"name": "Dota 2", "status": "completed"}},
        )
        self.assertEqual(await self.db.game_marks(20), {440: {"name": "Team Fortress 2", "status": "backlog"}})
        self.assertEqual(await self.db.game_marks(99), {})

    async def test_invalid_values_cannot_change_saved_marks(self):
        await self.db.set_game_status(10, "1", "9", 440, "Team Fortress 2", "backlog")
        for appid in (0, -1, True, "440", 440.0, 2**32):
            with self.subTest(appid=appid), self.assertRaises(ValueError):
                await self.db.set_game_status(10, "1", "9", appid, "Game", "playing")
        for name in (None, "", " \n", "bad\0name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                await self.db.set_game_status(10, "1", "9", 440, name, "playing")
        for status in ("done", "", True, []):
            with self.subTest(status=status), self.assertRaises(ValueError):
                await self.db.set_game_status(10, "1", "9", 440, "Game", status)
        self.assertEqual(await self.db.game_marks(10), {440: {"name": "Team Fortress 2", "status": "backlog"}})

    async def test_current_approval_and_identity_are_required_for_reads_and_writes(self):
        await self.db.set_game_status(10, "1", "9", 440, "Team Fortress 2", "backlog")
        for steam_id, family_id in (("old-steam", "9"), ("1", "old-family")):
            for status in ("playing", None):
                self.assertFalse(await self.db.set_game_status(10, steam_id, family_id, 440, "Game", status))
        for account_status in ("pending", "blocked"):
            await self.db.set_status(10, account_status)
            self.assertEqual(await self.db.game_marks(10), {})
            for status in ("playing", None):
                self.assertFalse(await self.db.set_game_status(10, "1", "9", 440, "Game", status))
        await self.db.set_status(10, "approved")
        self.assertEqual(await self.db.game_marks(10), {440: {"name": "Team Fortress 2", "status": "backlog"}})
        await self.db.ensure_family("8", "Other")
        await self.db.save_account(10, "2", "8", "replacement")
        await self.db.set_status(10, "approved")
        self.assertFalse(await self.db.set_game_status(10, "1", "9", 440, "Game", "completed"))

    async def test_logout_and_account_deletion_remove_marks_and_reject_late_writes(self):
        for delete in (self.db.delete_user, self.db.delete_account):
            await self.db.ensure_family("9", "Family")
            await self.db.save_account(10, "1", "9", "token")
            await self.db.set_status(10, "approved")
            await self.db.set_game_status(10, "1", "9", 440, "Team Fortress 2", "playing")
            await delete(10)
            self.assertEqual(await self.db.game_marks(10), {})
            self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_statuses"), 0)
            self.assertFalse(await self.db.set_game_status(10, "1", "9", 440, "Game", "backlog"))

    async def test_waiting_write_rechecks_identity_after_deletion_or_relink(self):
        for mutation in (
            "DELETE FROM accounts WHERE telegram_id = 10",
            "UPDATE accounts SET steam_id = '2' WHERE telegram_id = 10",
            "UPDATE accounts SET token_generation = nextval('steamily_token_generation') WHERE telegram_id = 10",
        ):
            with self.subTest(mutation=mutation):
                await self.db.save_account(10, "1", "9", "token")
                await self.db.set_status(10, "approved")
                generation = (await self.db.account(10))["token_generation"]
                async with self.db.pool.acquire() as connection, connection.transaction():
                    await connection.fetchval("SELECT telegram_id FROM accounts WHERE telegram_id = 10 FOR UPDATE")
                    writer = asyncio.create_task(
                        self.db.set_game_status(10, "1", "9", 440, "Game", "playing", expected_generation=generation)
                    )
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(writer), 0.05)
                    await connection.execute(mutation)
                self.assertFalse(await asyncio.wait_for(writer, 5))
                self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_statuses"), 0)

    async def test_logout_and_same_identity_relogin_reject_old_generation_writes_and_clears(self):
        old_generation = (await self.db.account(10))["token_generation"]
        await self.db.delete_user(10)
        await self.db.ensure_family("9", "Family")
        await self.db.save_account(10, "1", "9", "replacement")
        await self.db.set_status(10, "approved")
        generation = (await self.db.account(10))["token_generation"]
        self.assertNotEqual(generation, old_generation)
        self.assertTrue(
            await self.db.set_game_status(10, "1", "9", 440, "Game", "completed", expected_generation=generation)
        )
        for status in ("playing", None):
            with self.subTest(status=status):
                self.assertFalse(
                    await self.db.set_game_status(10, "1", "9", 440, "Game", status, expected_generation=old_generation)
                )
                self.assertEqual(await self.db.game_marks(10), {440: {"name": "Game", "status": "completed"}})

    async def test_upgrade_preserves_accounts_and_creates_empty_personal_marks(self):
        async with self.db.pool.acquire() as connection:
            await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            with patch("bot.migrations.MIGRATIONS", MIGRATIONS[:7]):
                await migrate(connection)
            await connection.execute("""
                INSERT INTO families (family_id, name) VALUES ('9', 'Existing');
                INSERT INTO accounts (telegram_id, steam_id, family_id, status)
                VALUES (10, '1', '9', 'approved');
            """)
            before = await connection.fetchrow("SELECT * FROM accounts")
            await migrate(connection)
            self.assertEqual(await connection.fetchrow("SELECT * FROM accounts"), before)
            self.assertIsNotNone(await connection.fetchval("SELECT to_regclass('game_statuses')"))
        self.assertEqual(await self.db.game_marks(10), {})
