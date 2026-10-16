import asyncio
import unittest
from unittest.mock import patch

import asyncpg
from aiogram.fsm.storage.base import StorageKey
from cryptography.fernet import Fernet

from bot.db import SCHEMA, Db, PgStorage, SteamAlreadyLinked
from bot.states import Menu
from tests.pg import TEST_DATABASE_URL, fresh_db, needs_pg


@needs_pg
class DbTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        await self.db.ensure_family("9", "Fam")
        await self.db.update_family("9", state={"members": ["1"], "ownership": {}}, notify_chat=-100)

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def publication(self, telegram_id, steam_id, family_id, setting, on):
        account = await self.db.account(telegram_id)
        return await self.db.set_publication(
            telegram_id,
            steam_id,
            family_id,
            setting,
            on,
            expected_version=account["publication_version"] if account else "missing",
        )

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

    async def test_publication_upgrade_defaults_off_and_preserves_existing_accounts(self):
        await self.db.save_account(10, "1", "9", "legacy-token", "KZ")
        await self.db.set_status(10, "approved")
        await self.db.pool.execute(
            "ALTER TABLE accounts DROP COLUMN publish_presence, DROP COLUMN publish_telegram, "
            "DROP COLUMN publication_version"
        )
        await self.db.pool.execute(SCHEMA)
        account = await self.db.account(10)
        self.assertEqual((account["publish_presence"], account["publish_telegram"]), (False, False))
        self.assertTrue(account["publication_version"])
        legacy_version = account["publication_version"]
        self.assertEqual((account["status"], self.db.refresh_token(account)), ("approved", "legacy-token"))

        await self.db.save_account(20, "2", "9", "new-token")
        account = await self.db.account(20)
        self.assertEqual((account["publish_presence"], account["publish_telegram"]), (False, False))
        self.assertNotEqual(account["publication_version"], legacy_version)
        with self.assertRaises(asyncpg.NotNullViolationError):
            await self.db.pool.execute("UPDATE accounts SET publication_version = NULL WHERE telegram_id = 10")
        for setting in ("publish_presence", "publish_telegram"):
            with self.subTest(setting=setting):
                with self.assertRaises(asyncpg.NotNullViolationError):
                    await self.db.pool.execute(f"UPDATE accounts SET {setting} = NULL WHERE telegram_id = 10")
                self.assertTrue(await self.publication(10, "1", "9", setting, True))
        before = (await self.db.account(10))["publication_version"]
        await self.db.pool.execute(SCHEMA)
        account = await self.db.account(10)
        self.assertEqual((account["publish_presence"], account["publish_telegram"]), (True, True))
        self.assertEqual(account["publication_version"], before)

    async def test_publication_version_rejects_stale_clicks_and_changes_only_with_consent(self):
        await self.db.save_account(10, "1", "9", "token")
        await self.db.set_status(10, "approved")
        initial = (await self.db.account(10))["publication_version"]
        self.assertTrue(
            await self.db.set_publication(10, "1", "9", "publish_presence", False, expected_version=initial)
        )
        self.assertEqual((await self.db.account(10))["publication_version"], initial)
        self.assertTrue(await self.db.set_publication(10, "1", "9", "publish_presence", True, expected_version=initial))
        enabled = (await self.db.account(10))["publication_version"]
        self.assertNotEqual(enabled, initial)
        self.assertFalse(
            await self.db.set_publication(10, "1", "9", "publish_presence", False, expected_version=initial)
        )
        self.assertTrue(await self.publication(10, "1", "9", "publish_presence", False))
        self.assertTrue(await self.publication(10, "1", "9", "publish_presence", True))
        self.assertNotEqual((await self.db.account(10))["publication_version"], enabled)
        self.assertFalse(
            await self.db.set_publication(10, "1", "9", "publish_telegram", True, expected_version=enabled)
        )

    async def test_concurrent_publication_callbacks_accept_only_one_version(self):
        await self.db.save_account(10, "1", "9", "token")
        await self.db.set_status(10, "approved")
        version = (await self.db.account(10))["publication_version"]
        results = await asyncio.gather(
            self.db.set_publication(10, "1", "9", "publish_presence", True, expected_version=version),
            self.db.set_publication(10, "1", "9", "publish_telegram", True, expected_version=version),
        )
        self.assertCountEqual(results, [True, False])
        account = await self.db.account(10)
        self.assertEqual(sum((account["publish_presence"], account["publish_telegram"])), 1)

    async def test_publication_flags_and_accounts_are_independent_and_persistent(self):
        for telegram_id, steam_id in ((10, "1"), (20, "2")):
            await self.db.save_account(telegram_id, steam_id, "9", "token")
            await self.db.set_status(telegram_id, "approved")
        self.assertTrue(await self.publication(10, "1", "9", "publish_presence", True))
        self.assertTrue(await self.publication(20, "2", "9", "publish_telegram", True))
        self.assertTrue(await self.publication(10, "1", "9", "publish_presence", False))
        self.assertTrue(await self.publication(10, "1", "9", "publish_presence", True))
        restarted = await Db.connect(TEST_DATABASE_URL, Fernet.generate_key().decode())
        try:
            first, second = await restarted.account(10), await restarted.account(20)
            self.assertEqual((first["publish_presence"], first["publish_telegram"]), (True, False))
            self.assertEqual((second["publish_presence"], second["publish_telegram"]), (False, True))
        finally:
            await restarted.pool.close()

    async def test_publication_rejects_invalid_stale_unapproved_and_expired_accounts(self):
        await self.db.save_account(10, "1", "9", "token")
        for status in ("pending", "blocked"):
            await self.db.set_status(10, status)
            self.assertFalse(await self.publication(10, "1", "9", "publish_presence", True))
        await self.db.set_status(10, "approved")
        for telegram_id, steam_id, family_id, setting in (
            (20, "1", "9", "publish_presence"),
            (10, "2", "9", "publish_presence"),
            (10, "1", "other", "publish_presence"),
            (10, "1", "9", "status"),
        ):
            with self.subTest(telegram_id=telegram_id, steam_id=steam_id, family_id=family_id, setting=setting):
                self.assertFalse(await self.publication(telegram_id, steam_id, family_id, setting, True))
        await self.db.set_refresh_token(10, None)
        self.assertFalse(await self.publication(10, "1", "9", "publish_presence", True))
        account = await self.db.account(10)
        self.assertEqual((account["publish_presence"], account["publish_telegram"]), (False, False))
        self.assertEqual(account["status"], "approved")

    async def test_publication_survives_same_login_and_resets_for_changed_identity_or_family(self):
        await self.db.ensure_family("other", "Other")
        await self.db.save_account(10, "1", "9", "token")
        for steam_id, family_id in (("2", "9"), ("2", "other")):
            await self.db.set_status(10, "approved")
            account = await self.db.account(10)
            for setting in ("publish_presence", "publish_telegram"):
                await self.publication(10, account["steam_id"], account["family_id"], setting, True)
            version = (await self.db.account(10))["publication_version"]
            await self.db.save_account(10, account["steam_id"], account["family_id"], "same-login")
            account = await self.db.account(10)
            self.assertEqual((account["publish_presence"], account["publish_telegram"]), (True, True))
            self.assertEqual(account["publication_version"], version)
            await self.db.set_refresh_token(10, None)
            self.assertFalse(
                await self.publication(10, account["steam_id"], account["family_id"], "publish_presence", False)
            )
            await self.db.save_account(10, account["steam_id"], account["family_id"], "reauthenticated")
            restored = await self.db.account(10)
            self.assertEqual((restored["publish_presence"], restored["publish_telegram"]), (True, True))
            self.assertEqual(restored["publication_version"], version)
            await self.db.save_account(10, steam_id, family_id, "new-login")
            changed = await self.db.account(10)
            self.assertEqual((changed["publish_presence"], changed["publish_telegram"]), (False, False))
            self.assertNotEqual(changed["publication_version"], version)
            self.assertFalse(
                await self.publication(10, account["steam_id"], account["family_id"], "publish_presence", True)
            )

    async def test_publication_revocation_and_moderation_require_new_consent(self):
        await self.db.save_account(10, "1", "9", "token")
        for action in ("pending", "blocked", "revoke", "delete_account", "delete_user"):
            with self.subTest(action=action):
                await self.db.save_account(10, "1", "9", "token")
                await self.db.set_status(10, "approved")
                for setting in ("publish_presence", "publish_telegram"):
                    await self.publication(10, "1", "9", setting, True)
                version = (await self.db.account(10))["publication_version"]
                if action in ("pending", "blocked"):
                    await self.db.set_status(10, action)
                elif action == "revoke":
                    self.assertTrue(await self.db.revoke_account(10, "1", "9"))
                else:
                    await getattr(self.db, action)(10)
                await self.db.save_account(10, "1", "9", "relogin")
                await self.db.set_status(10, "approved")
                account = await self.db.account(10)
                self.assertEqual((account["publish_presence"], account["publish_telegram"]), (False, False))
                self.assertNotEqual(account["publication_version"], version)
                self.assertFalse(
                    await self.db.set_publication(10, "1", "9", "publish_presence", True, expected_version=version)
                )

    async def test_duplicate_steam_link_preserves_accounts_and_settings(self):
        await self.db.ensure_family("8", "Other family")
        for telegram_id, steam_id, family_id, status in ((10, "1", "9", "approved"), (20, "2", "8", "blocked")):
            await self.db.save_account(telegram_id, steam_id, family_id, "old-token", "KZ", 123.0)
            await self.db.set_status(telegram_id, status)
            await self.db.set_telegram_profile(telegram_id, f"user{telegram_id}", "Existing profile")
            await self.db.set_language(telegram_id, "ru")
            await self.db.set_notify_requests(telegram_id, False)
            await self.db.update_family(family_id, owner_telegram_id=telegram_id, alerts={"online": False})

        before = await self.snapshot_links()
        with self.assertRaises(SteamAlreadyLinked):
            await self.db.save_account(30, "1", "8", "new-token", "US")
        with self.assertRaises(SteamAlreadyLinked):
            await self.db.save_account(20, "1", "9", "replacement-token", "US", 999.0)
        self.assertEqual(await self.snapshot_links(), before)

    async def test_every_status_and_expired_token_reserve_the_steam_link(self):
        for status in ("pending", "approved", "blocked"):
            with self.subTest(status=status):
                await self.db.save_account(10, "1", "9", "token")
                await self.db.set_status(10, status)
                await self.db.set_refresh_token(10, None)
                with self.assertRaises(SteamAlreadyLinked):
                    await self.db.save_account(20, "1", "9", "another-token")
                self.assertIsNone(await self.db.account(20))
                self.assertEqual((await self.db.account(10))["status"], status)
                await self.db.delete_account(10)

    async def test_explicit_account_deletion_releases_the_steam_link(self):
        await self.db.save_account(10, "1", "9", "old-token")
        await self.db.delete_account(10)
        await self.db.save_account(20, "1", "9", "new-token")
        await self.db.delete_user(20)
        await self.db.save_account(30, "1", "9", "latest-token")
        self.assertEqual(self.db.refresh_token(await self.db.account(30)), "latest-token")

    async def test_status_changes_are_family_scoped_and_report_matches(self):
        await self.db.save_account(10, "1", "9", "token")
        self.assertFalse(await self.db.set_status(99, "approved", family_id="9"))
        self.assertFalse(await self.db.set_status(10, "approved", family_id="8"))
        self.assertEqual((await self.db.account(10))["status"], "pending")
        self.assertTrue(await self.db.set_status(10, "approved", family_id="9"))
        self.assertEqual((await self.db.account(10))["status"], "approved")
        self.assertTrue(await self.db.set_status(10, "blocked"))
        self.assertEqual((await self.db.account(10))["status"], "blocked")
        self.assertFalse(await self.db.set_status(99, "approved"))

    async def test_account_deletion_is_family_scoped_and_reports_matches(self):
        await self.db.save_account(10, "1", "9", "token")
        self.assertFalse(await self.db.delete_account(99, family_id="9"))
        self.assertFalse(await self.db.delete_account(10, family_id="8"))
        self.assertIsNotNone(await self.db.account(10))
        self.assertTrue(await self.db.delete_account(10, family_id="9"))
        self.assertIsNone(await self.db.account(10))
        await self.db.save_account(10, "1", "9", "token")
        self.assertTrue(await self.db.delete_account(10))
        self.assertIsNone(await self.db.account(10))
        self.assertFalse(await self.db.delete_account(10))

    async def test_scoped_mutations_reject_a_member_who_moved_after_the_caller_read(self):
        await self.db.ensure_family("8", "Other family")
        for status in ("approved", None):
            with self.subTest(status=status):
                await self.db.save_account(10, "1", "9", "token")
                stale_account = await self.db.account(10)
                await self.db.save_account(10, "1", "8", "new-token")
                moved_account = await self.db.account(10)
                if status is None:
                    changed = await self.db.delete_account(10, family_id=stale_account["family_id"])
                else:
                    changed = await self.db.set_status(10, status, family_id=stale_account["family_id"])
                self.assertFalse(changed)
                self.assertEqual(await self.db.account(10), moved_account)

    async def test_steam_uniqueness_also_blocks_direct_sql_writes(self):
        await self.db.save_account(10, "1", "9", "token")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.pool.execute("INSERT INTO accounts (telegram_id, steam_id, family_id) VALUES (20, '1', '9')")
        await self.db.save_account(20, "2", "9", "other-token")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.pool.execute("UPDATE accounts SET steam_id = '1' WHERE telegram_id = 20")
        self.assertEqual((await self.db.account(20))["steam_id"], "2")

    async def test_check_steam_link_allows_only_an_unlinked_or_same_telegram_account(self):
        await self.db.check_steam_link(10, "1")
        await self.db.save_account(10, "1", "9", "token")
        await self.db.check_steam_link(10, "1")
        with self.assertRaises(SteamAlreadyLinked):
            await self.db.check_steam_link(20, "1")

    async def test_unrelated_unique_constraint_errors_are_not_reported_as_link_conflicts(self):
        await self.db.save_account(10, "1", "9", "token", "KZ")
        await self.db.pool.execute("CREATE UNIQUE INDEX accounts_country_test ON accounts (country)")
        with self.assertRaises(asyncpg.UniqueViolationError) as caught:
            await self.db.save_account(20, "2", "9", "other-token", "KZ")
        self.assertEqual(caught.exception.constraint_name, "accounts_country_test")
        self.assertIsNone(await self.db.account(20))

    async def test_concurrent_saves_use_separate_connections_and_report_link_conflict(self):
        barrier = asyncio.Barrier(2)

        class RacingDb(Db):
            async def check_steam_link(self, telegram_id, steam_id):
                await super().check_steam_link(telegram_id, steam_id)
                await barrier.wait()

        async with self.db.pool.acquire() as first, self.db.pool.acquire() as second:
            key = Fernet.generate_key().decode()
            first_db, second_db = RacingDb(first, key), RacingDb(second, key)
            results = await asyncio.wait_for(
                asyncio.gather(
                    first_db.save_account(10, "1", "9", "first-token"),
                    second_db.save_account(20, "1", "9", "second-token"),
                    return_exceptions=True,
                ),
                timeout=5,
            )
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(sum(isinstance(result, SteamAlreadyLinked) for result in results), 1)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM accounts WHERE steam_id = '1'"), 1)

    async def snapshot_links(self):
        return {
            table: await self.db.pool.fetch(f"SELECT * FROM {table} ORDER BY 1")
            for table in ("accounts", "families", "telegram_users")
        }

    async def test_unique_steam_schema_upgrade_is_idempotent_and_keeps_existing_data(self):
        await self.db.save_account(10, "1", "9", "legacy-token", "KZ", 123.0)
        await self.db.set_status(10, "approved")
        await self.db.set_telegram_profile(10, "owner", "Original owner")
        await self.db.set_language(10, "ru")
        await self.db.update_family("9", owner_telegram_id=10)
        before = await self.snapshot_links()
        await self.db.pool.execute("DROP INDEX IF EXISTS accounts_steam_id_key")
        await self.db.pool.execute(SCHEMA)
        await self.db.pool.execute(SCHEMA)
        self.assertEqual(await self.snapshot_links(), before)
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.pool.execute("INSERT INTO accounts (telegram_id, steam_id, family_id) VALUES (20, '1', '9')")

    async def test_legacy_duplicates_abort_startup_without_changes_and_allow_reviewed_resolution(self):
        await self.db.save_account(10, "1", "9", "legacy-token", "KZ")
        await self.db.set_status(10, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.set_language(10, "ru")
        await self.db.pool.execute("DROP INDEX IF EXISTS accounts_steam_id_key")
        await self.db.pool.execute("INSERT INTO accounts (telegram_id, steam_id, family_id) VALUES (20, '1', '9')")
        before = await self.snapshot_links()
        created_pools = []
        create_pool = asyncpg.create_pool

        async def track_pool(*args, **kwargs):
            pool = await create_pool(*args, **kwargs)
            created_pools.append(pool)
            return pool

        try:
            with patch("bot.db.asyncpg.create_pool", side_effect=track_pool):
                with self.assertRaisesRegex(asyncpg.RaiseError, "docs/steam-account-links.md"):
                    await Db.connect(TEST_DATABASE_URL, Fernet.generate_key().decode())
            self.assertTrue(created_pools[0].is_closing())
            self.assertEqual(await self.snapshot_links(), before)
        finally:
            for pool in created_pools:
                await pool.close()

        await self.db.delete_account(20)  # An operator explicitly reviewed this losing link.
        await self.db.pool.execute(SCHEMA)
        await self.db.pool.execute(SCHEMA)
        self.assertEqual((await self.db.account(10))["owner_telegram_id"], 10)
        self.assertEqual(self.db.refresh_token(await self.db.account(10)), "legacy-token")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.pool.execute("INSERT INTO accounts (telegram_id, steam_id, family_id) VALUES (20, '1', '9')")

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

    async def test_initial_owner_claim_requires_an_account_in_the_family(self):
        await self.db.ensure_family("other", "Other")
        await self.db.save_account(10, "1", "other", "token")
        await self.db.set_status(10, "blocked")
        with self.assertRaises(RuntimeError):
            await self.db.claim_initial_owner("9", 10)
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        account = await self.db.account(10)
        self.assertEqual((account["family_id"], account["status"]), ("other", "blocked"))

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
