import asyncio
import unittest
from unittest.mock import patch

import asyncpg
from aiogram.fsm.storage.base import StorageKey
from cryptography.fernet import Fernet

from bot.db import Db, PgStorage, SteamAlreadyLinked
from bot.migrations import migrate
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
        self.assertEqual(self.db.refresh_token(await self.db.account(10)), "token")
        self.assertEqual((await self.db.family("8"))["owner_telegram_id"], 10)
        await self.db.revoke_membership(10, "9")
        self.assertEqual((await self.db.account(10))["status"], "pending")
        self.assertIsNone(self.db.refresh_token(await self.db.account(10)))
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        await self.db.set_status(10, "blocked")
        await self.db.set_refresh_token(10, "blocked-token", 123.0)
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.revoke_membership(10, "9")
        self.assertEqual((await self.db.account(10))["status"], "blocked")
        self.assertIsNone(self.db.refresh_token(await self.db.account(10)))
        self.assertEqual((await self.db.account(10))["token_renewed_at"], 0.0)
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_stale_steam_membership_cannot_revoke_a_same_family_replacement(self):
        await self.db.save_account(10, "1", "9", "old-token")
        await self.db.save_account(10, "2", "9", "new-token", "KZ", 123.0)
        await self.db.set_status(10, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        before = await self.snapshot_links()
        self.assertFalse(await self.db.revoke_membership(10, "9", steam_id="1"))
        self.assertEqual(await self.snapshot_links(), before)
        self.assertTrue(await self.db.revoke_membership(10, "9", steam_id="2"))
        account = await self.db.account(10)
        self.assertEqual(account["status"], "pending")
        self.assertIsNone(self.db.refresh_token(account))
        self.assertEqual(account["token_renewed_at"], 0.0)
        self.assertIsNone(account["owner_telegram_id"])

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
            expected = {**dict(account), "status": expected_status}
            if account["telegram_id"] == 10:
                expected.update(refresh_token=None, token_renewed_at=0.0)
            self.assertEqual(current, expected)
        self.assertEqual((await self.db.family("9"))["state"], before["families"][0]["state"])

    async def test_reconcile_members_clears_departed_credentials_for_every_status(self):
        for telegram_id, status in ((10, "approved"), (20, "pending"), (30, "blocked"), (40, "approved")):
            await self.db.save_account(telegram_id, str(telegram_id), "9", "token", "KZ", 123.0)
            await self.db.set_status(telegram_id, status)
        await self.db.update_family("9", owner_telegram_id=40)
        current_before = dict(await self.db.account(40))
        await self.db.reconcile_members("9", ["40"])
        self.assertEqual(dict(await self.db.account(40)), current_before)
        for telegram_id, expected_status in ((10, "pending"), (20, "pending"), (30, "blocked")):
            account = await self.db.account(telegram_id)
            self.assertEqual(account["status"], expected_status)
            self.assertIsNone(self.db.refresh_token(account))
            self.assertEqual(account["token_renewed_at"], 0.0)

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
                await self.db.ensure_family("9", "Fam")
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
        await self.db.ensure_family("9", "Fam")
        await self.db.save_account(20, "1", "9", "new-token")
        await self.db.delete_user(20)
        await self.db.ensure_family("9", "Fam")
        await self.db.save_account(30, "1", "9", "latest-token")
        self.assertEqual(self.db.refresh_token(await self.db.account(30)), "latest-token")

    async def test_logout_erases_personal_data_and_all_dialogs_after_restart(self):
        for telegram_id, steam_id in ((10, "1"), (20, "2")):
            await self.db.save_account(telegram_id, steam_id, "9", "secret-token", "KZ", 123.0)
            await self.db.set_telegram_profile(telegram_id, f"user{telegram_id}", "Saved profile")
            await self.db.set_language(telegram_id, "ru")
            await self.db.set_notify_requests(telegram_id, False)
        await self.db.update_family("9", owner_telegram_id=20)
        await self.db.enqueue_alerts("9", [{"text": "Shared family event"}])
        pending = await self.db.pending_alerts("9", 100)
        family, survivor = await self.db.family("9"), await self.db.account(20)
        storage = PgStorage(self.db)
        erased = [
            StorageKey(bot_id=1, chat_id=10, user_id=10),
            StorageKey(bot_id=2, chat_id=-100, user_id=10, thread_id=5, destiny="other"),
            StorageKey(bot_id=1, chat_id=-100, user_id=10, business_connection_id="business"),
        ]
        retained = [StorageKey(bot_id=1, chat_id=10, user_id=20), StorageKey(bot_id=1, chat_id=10, user_id=100)]
        for key in erased + retained:
            await storage.set_state(key, Menu.main)
            await storage.set_data(key, {"stack": ["saved dialog"]})

        await self.db.delete_user(10)
        await self.db.pool.close()
        self.db = await Db.connect(TEST_DATABASE_URL, Fernet.generate_key().decode())
        storage = PgStorage(self.db)
        self.assertIsNone(await self.db.account(10))
        self.assertIsNone(await self.db.language(10))
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM telegram_users WHERE telegram_id = 10"), 0)
        for key in erased:
            self.assertIsNone(await storage.get_state(key))
            self.assertEqual(await storage.get_data(key), {})
        for key in retained:
            self.assertEqual(await storage.get_state(key), Menu.main.state)
            self.assertEqual(await storage.get_data(key), {"stack": ["saved dialog"]})
        self.assertEqual(await self.db.account(20), survivor)
        self.assertEqual(await self.db.family("9"), family)
        self.assertEqual(await self.db.pending_alerts("9", 100), pending)

    async def test_logout_of_unregistered_user_erases_settings_and_dialogs(self):
        await self.db.set_language(10, "ru")
        storage = PgStorage(self.db)
        key = StorageKey(bot_id=1, chat_id=-100, user_id=10)
        await storage.set_data(key, {"stack": ["unregistered dialog"]})
        await self.db.delete_user(10)
        self.assertIsNone(await self.db.language(10))
        self.assertEqual(await storage.get_data(key), {})
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM fsm"), 0)

    async def test_admin_deletion_is_guarded_and_removes_personal_data_and_abandoned_family(self):
        await self.db.save_account(10, "1", "9", "secret-token", "KZ", 123.0)
        await self.db.set_telegram_profile(10, "user10", "Saved profile")
        await self.db.set_language(10, "ru")
        await self.db.set_notify_requests(10, False)
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.enqueue_alerts("9", [{"text": "Queued family event"}])
        storage = PgStorage(self.db)
        key = StorageKey(bot_id=1, chat_id=-100, user_id=10)
        await storage.set_data(key, {"stack": ["saved dialog"]})
        before = await self.snapshot_links()
        self.assertFalse(await self.db.delete_account(10, family_id="other"))
        self.assertEqual(await self.snapshot_links(), before)

        self.assertTrue(await self.db.delete_account(10, family_id="9"))
        self.assertIsNone(await self.db.account(10))
        self.assertIsNone(await self.db.language(10))
        self.assertIsNone(await self.db.family("9"))
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts"), 0)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM fsm"), 0)

    async def test_deleting_last_account_prunes_its_family_but_preserves_unrelated_data(self):
        await self.db.save_account(10, "1", "9", "secret-token")
        await self.db.ensure_family("other", "Other")
        await self.db.save_account(20, "2", "other", "surviving-token")
        await self.db.update_family("other", notify_chat=-200)
        await self.db.enqueue_alerts("9", [{"text": "Removed family event"}])
        await self.db.enqueue_alerts("other", [{"text": "Surviving family event"}])
        pending = await self.db.pending_alerts("other", 100)
        await self.db.save_store_cache("DE", {"apps": {"620": {"price": 999}}})
        family, survivor = await self.db.family("other"), await self.db.account(20)
        await self.db.delete_user(10)
        self.assertIsNone(await self.db.family("9"))
        self.assertEqual(await self.db.family("other"), family)
        self.assertEqual(await self.db.account(20), survivor)
        self.assertEqual(await self.db.pending_alerts("9", 100), [])
        self.assertEqual(await self.db.pending_alerts("other", 100), pending)
        self.assertEqual(await self.db.store_cache("DE"), {"apps": {"620": {"price": 999}}})

    async def test_startup_prunes_legacy_abandoned_families_and_preserves_registered_families(self):
        await self.db.save_account(10, "1", "9", "secret-token")
        family, account = await self.db.family("9"), await self.db.account(10)
        await self.db.ensure_family("orphan", "Abandoned")
        await self.db.update_family("orphan", state={"members": ["old"]}, owner_telegram_id=99, notify_chat=-999)
        await self.db.enqueue_alerts("orphan", [{"text": "Abandoned family event"}])
        await self.db.pool.execute("DELETE FROM schema_migrations WHERE version = 5")
        await self.migrate_db()
        await self.migrate_db()
        self.assertIsNone(await self.db.family("orphan"))
        self.assertEqual(await self.db.family("9"), family)
        self.assertEqual(await self.db.account(10), account)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts"), 0)

    async def test_concurrent_final_logouts_do_not_leave_family_data(self):
        await self.db.save_account(10, "1", "9", "first-token")
        await self.db.save_account(20, "2", "9", "second-token")
        await asyncio.wait_for(asyncio.gather(self.db.delete_user(10), self.db.delete_user(20)), timeout=5)
        self.assertIsNone(await self.db.family("9"))
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM accounts"), 0)

    async def test_logout_uses_same_lock_order_as_membership_reconciliation(self):
        await self.db.save_account(10, "1", "9", "token")
        logout = None
        try:
            async with self.db.pool.acquire() as connection, connection.transaction():
                await connection.fetchval("SELECT family_id FROM families WHERE family_id = '9' FOR UPDATE")
                logout = asyncio.create_task(self.db.delete_user(10))
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(logout), 0.05)
                await asyncio.wait_for(
                    connection.execute("UPDATE accounts SET status = 'pending' WHERE telegram_id = 10"), 3
                )
        finally:
            if logout is not None:
                await asyncio.wait_for(logout, 5)
        self.assertIsNone(await self.db.account(10))
        self.assertIsNone(await self.db.family("9"))

    async def test_deletion_rechecks_registration_if_login_moves_while_family_lock_is_pending(self):
        for scope in ("9", None):
            with self.subTest(scope=scope):
                await self.db.ensure_family("9", "Fam")
                await self.db.ensure_family("8", "Other")
                await self.db.save_account(10, "1", "9", "old-token")
                await self.db.set_language(10, "ru")
                async with self.db.pool.acquire() as connection, connection.transaction():
                    await connection.fetchval("SELECT family_id FROM families WHERE family_id = '9' FOR UPDATE")
                    deletion = asyncio.create_task(self.db.delete_account(10, family_id=scope))
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(deletion), 0.05)
                    await self.db.save_account(10, "1", "8", "replacement-token")
                self.assertEqual(await asyncio.wait_for(deletion, 5), scope is None)
                if scope is None:
                    self.assertIsNone(await self.db.account(10))
                    self.assertIsNone(await self.db.language(10))
                    self.assertIsNone(await self.db.family("8"))
                else:
                    self.assertEqual((await self.db.account(10))["family_id"], "8")
                    self.assertEqual(await self.db.language(10), "ru")
                    await self.db.delete_user(10)

    async def test_revoked_blocked_registration_is_retained_until_explicit_deletion(self):
        for delete in (self.db.delete_user, self.db.delete_account):
            with self.subTest(delete=delete.__name__):
                await self.db.ensure_family("9", "Fam")
                await self.db.save_account(10, "1", "9", "token")
                await self.db.set_status(10, "blocked")
                await self.db.set_language(10, "ru")
                storage = PgStorage(self.db)
                key = StorageKey(bot_id=1, chat_id=10, user_id=10)
                await storage.set_data(key, {"saved": "dialog"})
                await self.db.reconcile_members("9", [])
                await self.migrate_db()
                account = await self.db.account(10)
                self.assertEqual(account["status"], "blocked")
                self.assertIsNone(self.db.refresh_token(account))
                self.assertEqual(await self.db.language(10), "ru")
                self.assertEqual(await storage.get_data(key), {"saved": "dialog"})
                await delete(10)
                await self.db.reconcile_members("9", ["1"])
                self.assertIsNone(await self.db.account(10))
                self.assertIsNone(await self.db.language(10))
                self.assertIsNone(await self.db.family("9"))
                self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM fsm"), 0)

    async def test_family_pruning_only_removes_the_requested_abandoned_family(self):
        await self.db.save_account(10, "1", "9", "surviving-token")
        await self.db.ensure_family("abandoned", "Abandoned")
        await self.db.ensure_family("unrelated", "Unrelated")
        family, account = await self.db.family("9"), await self.db.account(10)
        self.assertFalse(await self.db.prune_family("9"))
        self.assertEqual(await self.db.family("9"), family)
        self.assertEqual(await self.db.account(10), account)
        self.assertTrue(await self.db.prune_family("abandoned"))
        self.assertIsNone(await self.db.family("abandoned"))
        self.assertFalse(await self.db.prune_family("abandoned"))
        self.assertIsNotNone(await self.db.family("unrelated"))

    async def test_dialog_cleanup_erases_rows_recreated_after_logout(self):
        storage = PgStorage(self.db)
        key = StorageKey(bot_id=1, chat_id=10, user_id=10)
        await self.db.delete_user(10)
        await storage.set_data(key, {"empty_stack": True})
        await self.db.delete_dialogs(10)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM fsm"), 0)

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
        await self.db.ensure_family("9", "Fam")
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
            for table in ("accounts", "families", "telegram_users", "fsm", "pending_alerts")
        }

    async def migrate_db(self):
        async with self.db.pool.acquire() as connection:
            await migrate(connection)

    async def test_unique_steam_schema_upgrade_is_idempotent_and_keeps_existing_data(self):
        await self.db.save_account(10, "1", "9", "legacy-token", "KZ", 123.0)
        await self.db.set_status(10, "approved")
        await self.db.set_telegram_profile(10, "owner", "Original owner")
        await self.db.set_language(10, "ru")
        await self.db.update_family("9", owner_telegram_id=10)
        before = await self.snapshot_links()
        await self.db.pool.execute("DROP TABLE schema_migrations; DROP INDEX accounts_steam_id_key")
        await self.migrate_db()
        await self.migrate_db()
        self.assertEqual(await self.snapshot_links(), before)
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.db.pool.execute("INSERT INTO accounts (telegram_id, steam_id, family_id) VALUES (20, '1', '9')")

    async def test_legacy_duplicates_abort_startup_without_changes_and_allow_reviewed_resolution(self):
        await self.db.save_account(10, "1", "9", "legacy-token", "KZ")
        await self.db.set_status(10, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        await self.db.set_language(10, "ru")
        await self.db.pool.execute("DROP TABLE schema_migrations; DROP INDEX accounts_steam_id_key")
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
        await self.migrate_db()
        await self.migrate_db()
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
        await self.db.save_account(10, "1", "9", "token")
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
        await self.db.save_account(10, "1", "9", "token")
        await self.db.pool.execute("DROP TABLE schema_migrations")
        await self.db.pool.execute("DROP TABLE IF EXISTS pending_alerts")
        await self.db.pool.execute("ALTER TABLE families DROP COLUMN IF EXISTS alert_retry_at")
        await self.db.pool.execute("ALTER TABLE families DROP COLUMN IF EXISTS alert_link_version")
        await self.migrate_db()
        await self.migrate_db()
        family = await self.db.family("9")
        self.assertEqual((family["name"], family["notify_chat"], family["alert_retry_at"]), ("Fam", -100, 0))
        self.assertEqual(family["alert_link_version"], 0)
        await self.db.enqueue_alerts("9", [{"text": "after upgrade"}])
        self.assertEqual((await self.db.pending_alerts("9", 100))[0]["text"], "after upgrade")

    async def test_alert_health_counts_only_live_events_for_current_chat_links(self):
        self.assertEqual(await self.db.alert_health(200), {"pending_retry": 0, "deferred": 0})
        await self.db.enqueue_alerts(
            "9",
            [
                {"text": "private durable message"},
                {"text": "fresh presence", "expires_at": 201},
                {"text": "expired", "expires_at": 199},
                {"text": "expires now", "expires_at": 200},
            ],
        )
        await self.db.ensure_family("other", "Other")
        await self.db.update_family("other", notify_chat=-200)
        await self.db.enqueue_alerts(
            "other",
            [
                {"text": "deferred durable"},
                {"text": "deferred presence", "expires_at": 400},
                {"text": "deferred expired", "expires_at": 150},
            ],
        )
        await self.db.defer_alerts("other", -200, 300)
        await self.db.ensure_family("unlinked", "Unlinked")
        await self.db.pool.execute("""
            INSERT INTO pending_alerts (family_id, chat_id, text)
            VALUES ('9', -999, 'obsolete link'), ('unlinked', -300, 'unlinked destination')
        """)
        self.assertEqual(await self.db.alert_health(200), {"pending_retry": 4, "deferred": 2})
        self.assertEqual(await self.db.alert_health(300), {"pending_retry": 3, "deferred": 0})
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts"), 9)

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

    async def test_stale_token_callbacks_cannot_overwrite_a_new_login(self):
        await self.db.save_account(10, "1", "9", "original-token")
        original = (await self.db.account(10))["refresh_token"]
        await self.db.delete_user(10)
        await self.db.ensure_family("9", "Fam")
        await self.db.save_account(10, "2", "9", "new-login", token_renewed_at=100.0)
        replacement = await self.db.account(10)
        await self.db.set_refresh_token(10, "stale-renewal", 200.0, expected_refresh_token=original)
        await self.db.set_refresh_token(10, None, expected_refresh_token=original)
        self.assertEqual(await self.db.account(10), replacement)

        await self.db.set_refresh_token(
            10, "current-renewal", 300.0, expected_refresh_token=replacement["refresh_token"]
        )
        renewed = await self.db.account(10)
        self.assertEqual((self.db.refresh_token(renewed), renewed["token_renewed_at"]), ("current-renewal", 300.0))
        await self.db.set_refresh_token(10, None, expected_refresh_token=renewed["refresh_token"])
        self.assertIsNone(self.db.refresh_token(await self.db.account(10)))

    async def test_renewal_schema_upgrade_keeps_existing_accounts(self):
        await self.db.save_account(10, "1", "9", "legacy", "KZ")
        await self.db.set_status(10, "approved")
        await self.db.pool.execute("DROP TABLE schema_migrations; ALTER TABLE accounts DROP COLUMN token_renewed_at")
        await self.migrate_db()
        await self.migrate_db()
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
