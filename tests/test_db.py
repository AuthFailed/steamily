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

    async def test_moving_family_requires_approval_and_clears_only_the_departing_owner(self):
        await self.db.ensure_family("8", "Other family")
        await self.db.save_account(10, "1", "9", "old-token", "KZ")
        await self.db.set_status(10, "approved")
        await self.db.set_language(10, "ru")
        await self.db.set_notify_requests(10, False)
        await self.db.set_telegram_profile(10, "owner", "Original owner")
        await self.db.update_family("9", owner_telegram_id=10, alerts={"online": False})
        before_family = dict(await self.db.family("9"))

        await self.db.save_account(10, "1", "8", "new-token", "US", 123.0)

        account = await self.db.account(10)
        self.assertEqual((account["family_id"], account["status"]), ("8", "pending"))
        self.assertEqual(
            (self.db.refresh_token(account), account["country"], account["token_renewed_at"]),
            ("new-token", "US", 123.0),
        )
        self.assertEqual((account["tg_username"], account["tg_name"]), ("owner", "Original owner"))
        self.assertEqual(await self.db.language(10), "ru")
        self.assertFalse(await self.db.notify_requests(10))
        self.assertEqual(dict(await self.db.family("9")), {**before_family, "owner_telegram_id": None})
        self.assertIsNone((await self.db.family("8"))["owner_telegram_id"])

    async def test_failed_family_move_keeps_the_old_owner_and_account(self):
        await self.db.save_account(10, "1", "9", "old-token")
        await self.db.set_status(10, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        before = await self.snapshot_links()
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.db.save_account(10, "1", "missing-family", "new-token")
        self.assertEqual(await self.snapshot_links(), before)

    async def test_changing_steam_account_clears_owner_and_requires_approval(self):
        await self.db.save_account(10, "1", "9", "old-token")
        await self.db.set_status(10, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.save_account(10, "2", "9", "new-token")
        self.assertEqual((await self.db.account(10))["status"], "pending")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_revoke_membership_is_scoped_to_account_family_and_preserves_blocked(self):
        await self.db.ensure_family("8", "Other family")
        await self.db.save_account(10, "1", "9", "token")
        await self.db.set_status(10, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.update_family("8", owner_telegram_id=10)
        await self.db.revoke_membership(10, "8")
        self.assertEqual((await self.db.account(10))["status"], "approved")
        self.assertEqual((await self.db.family("8"))["owner_telegram_id"], 10)
        await self.db.revoke_membership(10, "9")
        self.assertEqual((await self.db.account(10))["status"], "pending")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        await self.db.set_status(10, "blocked")
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.revoke_membership(10, "9")
        self.assertEqual((await self.db.account(10))["status"], "blocked")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_reconcile_members_revokes_departed_owner_and_promotes_a_current_member(self):
        for telegram_id, steam_id in ((10, "1"), (20, "2")):
            await self.db.save_account(telegram_id, steam_id, "9", "token", "KZ", 123.0)
            await self.db.set_status(telegram_id, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        before = await self.snapshot_links()

        await self.db.reconcile_members("9", ["2"])

        self.assertEqual((await self.db.account(10))["status"], "pending")
        self.assertEqual((await self.db.account(20))["status"], "approved")
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 20)
        for account in before["accounts"]:
            current = dict(await self.db.account(account["telegram_id"]))
            current.pop("owner_telegram_id")
            expected_status = "pending" if account["telegram_id"] == 10 else "approved"
            self.assertEqual(current, {**dict(account), "status": expected_status})
        self.assertEqual((await self.db.family("9"))["state"], before["families"][0]["state"])

    async def test_reconcile_members_prefers_approved_then_oldest_pending_and_excludes_blocked(self):
        for telegram_id, steam_id, status in ((10, "1", "blocked"), (20, "2", "pending"), (30, "3", "approved")):
            await self.db.save_account(telegram_id, steam_id, "9", "token")
            await self.db.set_status(telegram_id, status)
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.reconcile_members("9", ["1", "2", "3"])
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 30)
        self.assertEqual((await self.db.account(10))["status"], "blocked")

        await self.db.set_status(30, "blocked")
        await self.db.reconcile_members("9", ["1", "2", "3"])
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 20)
        self.assertEqual((await self.db.account(20))["status"], "approved")
        self.assertEqual((await self.db.account(30))["status"], "blocked")

    async def test_reconcile_members_bootstraps_legacy_owner_and_keeps_valid_tokenless_owner(self):
        for telegram_id, steam_id in ((20, "2"), (10, "1")):
            await self.db.save_account(telegram_id, steam_id, "9", "token")
        await self.db.pool.execute(
            "UPDATE accounts SET created_at = created_at - interval '1 day' WHERE telegram_id = 20"
        )
        await self.db.reconcile_members("9", ["1", "2"])
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 20)
        await self.db.set_refresh_token(20, None)
        await self.db.set_status(10, "approved")
        before_family = await self.db.pool.fetchrow(
            "SELECT *, xmin::text AS version FROM families WHERE family_id = '9'"
        )
        before_accounts = await self.db.pool.fetch("SELECT *, xmin::text AS version FROM accounts ORDER BY telegram_id")
        await self.db.reconcile_members("9", ["1", "2"])
        self.assertEqual(
            await self.db.pool.fetchrow("SELECT *, xmin::text AS version FROM families WHERE family_id = '9'"),
            before_family,
        )
        self.assertEqual(
            await self.db.pool.fetch("SELECT *, xmin::text AS version FROM accounts ORDER BY telegram_id"),
            before_accounts,
        )

    async def test_reconcile_members_clears_owner_without_eligible_registered_members(self):
        await self.db.save_account(10, "1", "9", "token")
        await self.db.set_status(10, "approved")
        await self.db.save_account(20, "2", "9", "token")
        await self.db.set_status(20, "blocked")
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.reconcile_members("9", ["2", "unregistered"])
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        self.assertEqual((await self.db.account(10))["status"], "pending")
        self.assertEqual((await self.db.account(20))["status"], "blocked")
        await self.db.reconcile_members("9", [])
        self.assertEqual((await self.db.account(20))["status"], "blocked")

    async def test_reconcile_members_replaces_missing_or_wrong_family_owner(self):
        await self.db.ensure_family("8", "Other family")
        await self.db.save_account(10, "1", "8", "token")
        await self.db.save_account(20, "2", "9", "token")
        await self.db.set_status(10, "approved")
        await self.db.set_status(20, "approved")
        for owner in (None, 999, 10):
            with self.subTest(owner=owner):
                await self.db.update_family("9", owner_telegram_id=owner)
                await self.db.reconcile_members("9", ["1", "2"])
                self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 20)
                self.assertEqual((await self.db.account(10))["status"], "approved")

    async def test_reconcile_members_rolls_back_status_changes_if_owner_update_fails(self):
        await self.db.save_account(10, "1", "9", "token")
        await self.db.set_status(10, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.pool.execute("""
            CREATE FUNCTION reject_owner_update() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'owner update failed'; END $$;
            CREATE TRIGGER reject_owner_update BEFORE UPDATE ON families
            FOR EACH ROW EXECUTE FUNCTION reject_owner_update();
        """)
        before = await self.snapshot_links()
        with self.assertRaisesRegex(asyncpg.RaiseError, "owner update failed"):
            await self.db.reconcile_members("9", [])
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

    async def test_initial_owner_claim_requires_an_account_in_the_family(self):
        await self.db.ensure_family("other", "Other")
        await self.db.save_account(10, "1", "other", "token")
        await self.db.set_status(10, "blocked")
        with self.assertRaises(RuntimeError):
            await self.db.claim_initial_owner("9", 10)
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        account = await self.db.account(10)
        self.assertEqual((account["family_id"], account["status"]), ("other", "blocked"))

    async def test_blocked_account_cannot_claim_a_vacant_owner(self):
        await self.db.save_account(10, "1", "9", "token")
        await self.db.set_status(10, "blocked")
        before = await self.snapshot_links()
        await self.db.claim_initial_owner("9", 10)
        self.assertEqual(await self.snapshot_links(), before)

    async def test_concurrent_initial_owner_claims_approve_only_the_first_account(self):
        await self.db.save_account(10, "1", "9", "token")
        await self.db.save_account(20, "2", "9", "token")
        await asyncio.gather(self.db.claim_initial_owner("9", 10), self.db.claim_initial_owner("9", 20))
        owner = (await self.db.family("9"))["owner_telegram_id"]
        self.assertIn(owner, (10, 20))
        for account in await self.db.family_accounts("9"):
            self.assertEqual(account["status"], "approved" if account["telegram_id"] == owner else "pending")

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
