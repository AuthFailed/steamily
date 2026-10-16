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

    async def test_observation_and_alerts_commit_together_and_survive_reconnect(self):
        state = {"members": ["1", "2"], "ownership": {"620": ["1"]}}
        await self.db.observe_family("9", state, "Observed", [{"text": "Game added"}, {"text": "Member joined"}])
        await self.db.pool.close()
        self.db = await Db.connect(TEST_DATABASE_URL, Fernet.generate_key().decode())
        family = await self.db.family("9")
        self.assertEqual((family["state"], family["name"]), (state, "Observed"))
        pending = await self.db.pending_alerts("9", 100)
        self.assertEqual([row["text"] for row in pending], ["Game added", "Member joined"])
        self.assertEqual([row["chat_id"] for row in pending], [-100, -100])
        self.assertEqual([row["family_id"] for row in await self.db.alert_families()], ["9"])
        await self.db.delete_alert(pending[0]["id"])
        self.assertEqual([row["text"] for row in await self.db.pending_alerts("9", 100)], ["Member joined"])

    async def test_observation_rolls_back_if_alert_enqueue_fails(self):
        before = await self.db.family("9")
        with self.assertRaises(asyncpg.NotNullViolationError):
            await self.db.observe_family("9", {"members": []}, "Changed", [{"text": "first"}, {"text": None}])
        self.assertEqual(await self.db.family("9"), before)
        self.assertEqual(await self.db.pending_alerts("9", 100), [])

    async def test_unlinked_family_observation_does_not_create_alerts(self):
        await self.db.update_family("9", notify_chat=None)
        await self.db.observe_family("9", {"members": []}, "Changed", [{"text": "not sent"}])
        await self.db.enqueue_alerts("9", [{"text": "also not sent"}])
        self.assertEqual((await self.db.family("9"))["name"], "Changed")
        self.assertEqual(await self.db.pending_alerts("9", 100), [])
        self.assertEqual(await self.db.alert_families(), [])

    async def test_presence_coalesces_with_new_identity_and_expires(self):
        await self.db.enqueue_alerts("9", [{"text": "durable"}, {"text": "Online", "coalesce_key": "presence:1"}])
        original = await self.db.pending_alerts("9", 100)
        await self.db.enqueue_alerts("9", [{"text": "Playing", "coalesce_key": "presence:1", "expires_at": 200}])
        pending = await self.db.pending_alerts("9", 100)
        self.assertEqual([row["text"] for row in pending], ["durable", "Playing"])
        self.assertNotEqual(pending[-1]["id"], original[-1]["id"])
        await self.db.delete_alert(original[-1]["id"])
        self.assertEqual([row["text"] for row in await self.db.pending_alerts("9", 100)], ["durable", "Playing"])
        self.assertEqual([row["text"] for row in await self.db.pending_alerts("9", 200)], ["durable"])
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts"), 1)

    async def test_retry_delay_and_migration_preserve_pending_identity(self):
        await self.db.enqueue_alerts("9", [{"text": "durable"}])
        alert_id = (await self.db.pending_alerts("9", 100))[0]["id"]
        await self.db.defer_alerts("9", -100, 200)
        self.assertEqual(await self.db.pending_alerts("9", 199), [])
        self.assertTrue(await self.db.migrate_chat(-100, -100123))
        self.assertEqual(await self.db.pending_alerts("9", 199), [])
        pending = await self.db.pending_alerts("9", 200)
        self.assertEqual((pending[0]["id"], pending[0]["chat_id"]), (alert_id, -100123))
        self.assertEqual((await self.db.family("9"))["alert_retry_at"], 200)
        self.assertFalse(await self.db.migrate_chat(-100, -100123))
        await self.db.defer_alerts("9", -100, 999)
        self.assertEqual((await self.db.family("9"))["alert_retry_at"], 200)

    async def test_chat_migration_rejects_stale_and_occupied_destinations(self):
        await self.db.ensure_family("other", "Other")
        await self.db.update_family("other", notify_chat=-200)
        await self.db.enqueue_alerts("9", [{"text": "durable"}])
        self.assertFalse(await self.db.migrate_chat(-100, -200))
        self.assertEqual((await self.db.family("9"))["notify_chat"], -100)
        self.assertEqual((await self.db.pending_alerts("9", 100))[0]["chat_id"], -100)
        await self.db.update_family("9", notify_chat=-300)
        self.assertFalse(await self.db.migrate_chat(-100, -400))
        self.assertEqual((await self.db.family("9"))["notify_chat"], -300)

    async def test_stale_send_response_cannot_migrate_or_unlink_another_family(self):
        await self.db.update_family("9", notify_chat=-200)
        await self.db.ensure_family("other", "Other")
        await self.db.update_family("other", notify_chat=-100)
        await self.db.enqueue_alerts("other", [{"text": "other family's alert"}])
        self.assertFalse(await self.db.migrate_chat(-100, -300, family_id="9"))
        await self.db.unlink_chat(-100, family_id="9")
        self.assertEqual((await self.db.family("9"))["notify_chat"], -200)
        self.assertEqual((await self.db.family("other"))["notify_chat"], -100)
        self.assertEqual((await self.db.pending_alerts("other", 100))[0]["chat_id"], -100)

    async def test_stale_send_response_cannot_change_a_relinked_same_chat(self):
        link_version = (await self.db.family("9"))["alert_link_version"]
        await self.db.unlink_chat(-100)
        await self.db.update_family("9", notify_chat=-100)
        await self.db.enqueue_alerts("9", [{"text": "new binding"}])
        self.assertFalse(await self.db.migrate_chat(-100, -300, family_id="9", link_version=link_version))
        await self.db.unlink_chat(-100, family_id="9", link_version=link_version)
        self.assertEqual((await self.db.family("9"))["notify_chat"], -100)
        self.assertEqual((await self.db.pending_alerts("9", 100))[0]["text"], "new binding")

    async def test_inflight_failure_defers_migrated_alert_but_not_a_manual_relink(self):
        await self.db.enqueue_alerts("9", [{"text": "in flight"}])
        link_version = (await self.db.pending_alerts("9", 100))[0]["link_version"]
        await self.db.migrate_chat(-100, -200)
        await self.db.defer_alerts("9", -100, 300, link_version=link_version)
        await self.db.defer_alerts("9", -100, 200, link_version=link_version)
        self.assertEqual((await self.db.family("9"))["alert_retry_at"], 300)
        await self.db.update_family("9", notify_chat=-300)
        await self.db.enqueue_alerts("9", [{"text": "new link"}])
        await self.db.defer_alerts("9", -100, 999, link_version=link_version)
        self.assertEqual((await self.db.family("9"))["alert_retry_at"], 0)

    async def test_inflight_presence_failure_defers_its_coalesced_replacement(self):
        await self.db.enqueue_alerts("9", [{"text": "Online", "coalesce_key": "presence:1"}])
        link_version = (await self.db.pending_alerts("9", 100))[0]["link_version"]
        await self.db.enqueue_alerts("9", [{"text": "Playing", "coalesce_key": "presence:1"}])
        await self.db.defer_alerts("9", -100, 300, link_version=link_version)
        self.assertEqual((await self.db.family("9"))["alert_retry_at"], 300)
        self.assertEqual(await self.db.pending_alerts("9", 299), [])
        self.assertEqual((await self.db.pending_alerts("9", 300))[0]["text"], "Playing")

    async def test_inflight_presence_failure_survives_both_migration_and_coalescing(self):
        await self.db.enqueue_alerts("9", [{"text": "Online", "coalesce_key": "presence:1"}])
        event = (await self.db.pending_alerts("9", 100))[0]
        await self.db.migrate_chat(-100, -200)
        await self.db.enqueue_alerts("9", [{"text": "Playing", "coalesce_key": "presence:1"}])
        await self.db.defer_alerts("9", -100, 300, link_version=event["link_version"])
        self.assertEqual((await self.db.family("9"))["alert_retry_at"], 300)
        self.assertEqual(await self.db.pending_alerts("9", 299), [])

    async def test_manual_relink_and_unlink_discard_pending_and_retry(self):
        await self.db.enqueue_alerts("9", [{"text": "old destination"}])
        link_version = (await self.db.family("9"))["alert_link_version"]
        await self.db.defer_alerts("9", -100, 200)
        await self.db.update_family("9", notify_chat=-100, language="ru")
        self.assertEqual(len(await self.db.pending_alerts("9", 200)), 1)
        self.assertEqual((await self.db.family("9"))["alert_retry_at"], 200)
        self.assertEqual((await self.db.family("9"))["alert_link_version"], link_version)
        await self.db.update_family("9", notify_chat=-200)
        self.assertEqual(await self.db.pending_alerts("9", 200), [])
        self.assertEqual((await self.db.family("9"))["alert_retry_at"], 0)
        self.assertEqual((await self.db.family("9"))["alert_link_version"], link_version + 1)
        await self.db.enqueue_alerts("9", [{"text": "new destination"}])
        await self.db.defer_alerts("9", -200, 300)
        await self.db.unlink_chat(-100)
        self.assertEqual((await self.db.family("9"))["notify_chat"], -200)
        await self.db.unlink_chat(-200)
        family = await self.db.family("9")
        self.assertEqual((family["notify_chat"], family["alert_retry_at"]), (None, 0))
        self.assertEqual(family["alert_link_version"], link_version + 2)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts"), 0)

    async def test_unlink_waits_for_observation_and_clears_its_committed_alert(self):
        inserted, release = asyncio.Event(), asyncio.Event()
        enqueue = self.db._enqueue_alerts

        async def pause_after_enqueue(*args):
            await enqueue(*args)
            inserted.set()
            await release.wait()

        with patch.object(self.db, "_enqueue_alerts", side_effect=pause_after_enqueue):
            observation = asyncio.create_task(self.db.observe_family("9", {}, "New", [{"text": "concurrent"}]))
            await asyncio.wait_for(inserted.wait(), 5)
            unlink = asyncio.create_task(self.db.unlink_chat(-100))
            try:
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(unlink), 0.05)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(observation, unlink), 5)
        self.assertIsNone((await self.db.family("9"))["notify_chat"])
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts"), 0)

    async def test_alert_schema_upgrade_is_idempotent_and_keeps_existing_family(self):
        await self.db.pool.execute("DROP TABLE IF EXISTS pending_alerts")
        await self.db.pool.execute("ALTER TABLE families DROP COLUMN IF EXISTS alert_retry_at")
        await self.db.pool.execute("ALTER TABLE families DROP COLUMN IF EXISTS alert_link_version")
        await self.db.pool.execute(SCHEMA)
        await self.db.pool.execute(SCHEMA)
        family = await self.db.family("9")
        self.assertEqual((family["name"], family["notify_chat"], family["alert_retry_at"]), ("Fam", -100, 0))
        self.assertEqual(family["alert_link_version"], 0)
        await self.db.enqueue_alerts("9", [{"text": "after upgrade"}])
        self.assertEqual((await self.db.pending_alerts("9", 100))[0]["text"], "after upgrade")

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
