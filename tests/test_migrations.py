import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import asyncpg
from cryptography.fernet import Fernet

from bot.db import Db
from bot.migrations import MIGRATIONS, migrate
from tests.pg import TEST_DATABASE_URL, needs_pg


@needs_pg
class MigrationsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = await asyncpg.connect(TEST_DATABASE_URL)
        await self.connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")

    async def asyncTearDown(self):
        await self.connection.close()

    async def versions(self):
        return [
            tuple(row)
            for row in await self.connection.fetch(
                "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
            )
        ]

    async def test_fresh_database_and_restart_apply_each_migration_once(self):
        key = Fernet.generate_key().decode()
        db = await Db.connect(TEST_DATABASE_URL, key)
        try:
            await db.ensure_family("9", "Family")
            await db.save_account(10, "1", "9", "secret-token")
            versions = await self.versions()
            self.assertEqual(
                [(row[0], row[1]) for row in versions],
                [(version, path.name) for version, path in enumerate(MIGRATIONS, 1)],
            )
        finally:
            await db.pool.close()

        with patch("bot.migrations.Path.read_text", side_effect=AssertionError("Applied SQL must not run again")):
            db = await Db.connect(TEST_DATABASE_URL, key)
        try:
            self.assertEqual(await self.versions(), versions)
            self.assertEqual(db.refresh_token(await db.account(10)), "secret-token")
            self.assertEqual((await db.account(10))["token_renewed_at"], 0.0)
        finally:
            await db.pool.close()

    async def test_earliest_unversioned_schema_keeps_all_data_and_gains_current_fields(self):
        await self.connection.execute(MIGRATIONS[0].read_text())
        token = Fernet(Fernet.generate_key()).encrypt(b"existing-token")
        await self.connection.execute("""
            INSERT INTO families (family_id, name, notify_chat, state)
            VALUES ('9', 'Family', -100, '{"members":["1"]}');
            INSERT INTO accounts (telegram_id, steam_id, family_id, status) VALUES (10, '1', '9', 'approved');
            INSERT INTO telegram_users VALUES (10, 'ru');
            INSERT INTO store_cache VALUES ('KZ', '{"apps":{}}');
            INSERT INTO fsm VALUES ('state-key', 'Menu:main', '{"page":2}');
        """)
        await self.connection.execute("UPDATE accounts SET refresh_token = $1", token)
        before = {
            table: await self.connection.fetch(f"SELECT * FROM {table}")
            for table in ("families", "accounts", "telegram_users", "store_cache", "fsm")
        }
        await migrate(self.connection)
        for table, rows in before.items():
            after = await self.connection.fetch(f"SELECT * FROM {table}")
            self.assertEqual(
                [{name: row[name] for name in rows[0].keys()} for row in after], [dict(row) for row in rows]
            )
        self.assertEqual(await self.connection.fetchval("SELECT language FROM families"), "en")
        self.assertTrue(await self.connection.fetchval("SELECT notify_requests FROM telegram_users"))
        self.assertEqual(await self.connection.fetchval("SELECT token_renewed_at FROM accounts"), 0.0)
        for column in ("country", "tg_username", "tg_name"):
            self.assertIsNone(await self.connection.fetchval(f"SELECT {column} FROM accounts"))
        for column in ("owner_telegram_id", "alerts"):
            self.assertIsNone(await self.connection.fetchval(f"SELECT {column} FROM families"))
        for column in ("alert_retry_at", "alert_link_version"):
            self.assertEqual(await self.connection.fetchval(f"SELECT {column} FROM families"), 0)
        self.assertEqual(await self.connection.fetchval("SELECT count(*) FROM pending_alerts"), 0)

    async def test_versioned_upgrade_adds_delivery_queue_without_replaying_earlier_migrations(self):
        with patch("bot.migrations.MIGRATIONS", MIGRATIONS[:3]):
            await migrate(self.connection)
        await self.connection.execute("INSERT INTO families (family_id, name) VALUES ('9', 'Existing')")
        before = await self.versions()
        await migrate(self.connection)
        self.assertEqual((await self.versions())[:3], before)
        family = await self.connection.fetchrow("SELECT * FROM families WHERE family_id = '9'")
        self.assertEqual((family["name"], family["alert_retry_at"], family["alert_link_version"]), ("Existing", 0, 0))
        self.assertEqual(await self.connection.fetchval("SELECT count(*) FROM pending_alerts"), 0)

    async def test_version_four_upgrade_defaults_publication_off_and_preserves_consent_on_restart(self):
        with patch("bot.migrations.MIGRATIONS", MIGRATIONS[:4]):
            await migrate(self.connection)
        key = Fernet.generate_key().decode()
        token = Fernet(key).encrypt(b"legacy-token")
        await self.connection.execute("""
            INSERT INTO families (family_id, name, notify_chat) VALUES ('9', 'Existing', -100);
            INSERT INTO accounts (telegram_id, steam_id, family_id, status) VALUES (10, '1', '9', 'approved');
            INSERT INTO pending_alerts (family_id, chat_id, text) VALUES ('9', -100, 'legacy alert');
        """)
        await self.connection.execute("UPDATE accounts SET refresh_token = $1 WHERE telegram_id = 10", token)
        before = await self.versions()

        db = await Db.connect(TEST_DATABASE_URL, key)
        try:
            account = await db.account(10)
            self.assertEqual((account["publish_presence"], account["publish_telegram"]), (False, False))
            self.assertEqual((account["status"], db.refresh_token(account)), ("approved", "legacy-token"))
            self.assertIsNone((await db.pending_alerts("9", 0))[0]["publication"])
            self.assertEqual((await self.versions())[:4], before)
            for setting in ("publish_presence", "publish_telegram"):
                account = await db.account(10)
                self.assertTrue(
                    await db.set_publication(
                        10, "1", "9", setting, True, expected_version=account["publication_version"]
                    )
                )
            version = (await db.account(10))["publication_version"]
        finally:
            await db.pool.close()

        db = await Db.connect(TEST_DATABASE_URL, key)
        try:
            account = await db.account(10)
            self.assertEqual((account["publish_presence"], account["publish_telegram"]), (True, True))
            self.assertEqual(account["publication_version"], version)
            self.assertEqual(len(await self.versions()), 5)
        finally:
            await db.pool.close()

    async def test_unversioned_delivery_queue_and_retry_state_survive_adoption_and_restart(self):
        for path in MIGRATIONS:
            await self.connection.execute(path.read_text())
        await self.connection.execute("""
            INSERT INTO families (family_id, name, notify_chat, alert_retry_at, alert_link_version)
            VALUES ('9', 'Existing', -100, 250, 6);
            INSERT INTO pending_alerts (family_id, chat_id, text, coalesce_key, expires_at)
            VALUES ('9', -100, 'durable event', NULL, NULL), ('9', -100, 'presence event', 'presence:1', 500);
        """)
        family = await self.connection.fetchrow("SELECT * FROM families WHERE family_id = '9'")
        pending = await self.connection.fetch("SELECT * FROM pending_alerts ORDER BY id")
        await migrate(self.connection)
        await migrate(self.connection)
        self.assertEqual(await self.connection.fetchrow("SELECT * FROM families WHERE family_id = '9'"), family)
        self.assertEqual(await self.connection.fetch("SELECT * FROM pending_alerts ORDER BY id"), pending)
        next_id = await self.connection.fetchval("""
            INSERT INTO pending_alerts (family_id, chat_id, text) VALUES ('9', -100, 'next event') RETURNING id
        """)
        self.assertGreater(next_id, pending[-1]["id"])
        self.assertEqual(len(await self.versions()), len(MIGRATIONS))

    async def test_failed_migration_rolls_back_schema_data_and_all_new_versions(self):
        await self.connection.execute(MIGRATIONS[0].read_text())
        await self.connection.execute("INSERT INTO families (family_id, name) VALUES ('9', 'Original')")
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / f"{len(MIGRATIONS) + 1:03d}_broken.sql"
            broken.write_text("""
                ALTER TABLE families ADD COLUMN failed_upgrade TEXT;
                UPDATE families SET name = 'Changed';
                SELECT * FROM missing_migration_table;
            """)
            with patch("bot.migrations.MIGRATIONS", (*MIGRATIONS, broken)):
                with self.assertRaises(asyncpg.UndefinedTableError):
                    await migrate(self.connection)
        self.assertIsNone(await self.connection.fetchval("SELECT to_regclass('schema_migrations')"))
        self.assertEqual(await self.connection.fetchval("SELECT name FROM families"), "Original")
        self.assertEqual(
            await self.connection.fetchval("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_name = 'families' AND column_name IN ('failed_upgrade', 'language')
        """),
            0,
        )
        await migrate(self.connection)
        self.assertEqual(len(await self.versions()), len(MIGRATIONS))

    async def test_newer_or_inconsistent_history_refuses_startup_without_changes(self):
        await migrate(self.connection)
        newer = len(MIGRATIONS) + 1
        await self.connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)", newer, f"{newer:03d}_newer.sql"
        )
        before = await self.versions()
        with self.assertRaisesRegex(RuntimeError, "matching release"):
            await migrate(self.connection)
        self.assertEqual(await self.versions(), before)
        await self.connection.execute("DELETE FROM schema_migrations WHERE version = 2 OR version = $1", newer)
        with self.assertRaisesRegex(RuntimeError, "matching release"):
            await migrate(self.connection)

    async def test_concurrent_startups_serialize_migrations_on_separate_connections(self):
        second = await asyncpg.connect(TEST_DATABASE_URL)
        try:
            await asyncio.wait_for(asyncio.gather(migrate(self.connection), migrate(second)), timeout=5)
            self.assertEqual(len(await self.versions()), len(MIGRATIONS))
            self.assertIsNotNone(await self.connection.fetchval("SELECT to_regclass('accounts_steam_id_key')"))
        finally:
            await second.close()
