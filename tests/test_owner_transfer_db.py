import asyncio
import unittest
from datetime import timedelta

import asyncpg

from bot.migrations import migrate
from tests.pg import fresh_db, needs_pg


@needs_pg
class OwnerTransferDbTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        await self.db.ensure_family("9", "Family")
        for telegram_id, steam_id in ((10, "1"), (20, "2"), (30, "3")):
            await self.db.save_account(telegram_id, steam_id, "9", "token")
            await self.db.set_status(telegram_id, "approved")
        await self.db.update_family("9", owner_telegram_id=10, reader_telegram_id=10, notify_chat=-100)
        self.members = ["1", "2", "3"]

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def create(self, target_id=20):
        return await self.db.create_owner_transfer("9", 10, target_id, self.members)

    async def test_invitation_changes_nothing_until_selected_recipient_accepts(self):
        family = await self.db.family("9")
        accounts = await self.db.family_accounts("9")
        invitation = await self.create()
        self.assertEqual((invitation["owner_id"], invitation["target_id"]), (10, 20))
        self.assertEqual(invitation["owner_steam_id"], "1")
        self.assertEqual(invitation["target_steam_id"], "2")
        remaining = invitation["expires_at"] - await self.db.pool.fetchval("SELECT clock_timestamp()")
        self.assertTrue(timedelta(hours=23, minutes=59) < remaining <= timedelta(hours=24))
        self.assertEqual(await self.db.family("9"), family)
        self.assertEqual(await self.db.family_accounts("9"), accounts)
        self.assertEqual(await self.db.owner_transfer("9"), invitation)
        self.assertIsNone(await self.db.accept_owner_transfer("9", 30, invitation["id"], self.members))
        self.assertIsNone(await self.db.accept_owner_transfer("9", 20, "stale", self.members))
        accepted = await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members)
        self.assertEqual(accepted, invitation)
        self.assertEqual(dict(await self.db.family("9")), {**dict(family), "owner_telegram_id": 20})
        self.assertEqual(await self.db.family_accounts("9"), accounts)
        self.assertIsNone(await self.db.owner_transfer("9"))
        self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))

    async def test_creation_rechecks_owner_recipient_approval_identity_and_live_membership(self):
        for owner_id, target_id, members in (
            (20, 30, self.members),
            (10, 10, self.members),
            (10, 999, self.members),
            (10, 20, ["2", "3"]),
            (10, 20, ["1", "3"]),
        ):
            with self.subTest(owner_id=owner_id, target_id=target_id, members=members):
                self.assertIsNone(await self.db.create_owner_transfer("9", owner_id, target_id, members))
        for telegram_id in (10, 20):
            for status in ("pending", "blocked"):
                with self.subTest(telegram_id=telegram_id, status=status):
                    await self.db.set_status(telegram_id, status)
                    self.assertIsNone(await self.create())
                    await self.db.set_status(telegram_id, "approved")
            await self.db.set_refresh_token(telegram_id, None)
            self.assertIsNone(await self.create())
            await self.db.save_account(telegram_id, str(telegram_id // 10), "9", "token")
        await self.db.ensure_family("8", "Other")
        await self.db.save_account(20, "2", "8", "token")
        await self.db.set_status(20, "approved")
        self.assertIsNone(await self.create())
        self.assertIsNone(await self.db.owner_transfer("9"))

    async def test_replacing_and_cancelling_cannot_act_on_a_different_invitation(self):
        first = await self.create()
        second = await self.create(30)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(await self.db.owner_transfer("9"), second)
        self.assertIsNone(await self.db.cancel_owner_transfer("9", 20, second["id"]))
        self.assertIsNone(await self.db.cancel_owner_transfer("9", 10, first["id"]))
        self.assertIsNone(await self.db.accept_owner_transfer("9", 20, first["id"], self.members))
        self.assertEqual(await self.db.cancel_owner_transfer("9", 10, second["id"]), second)
        self.assertIsNone(await self.db.cancel_owner_transfer("9", 10, second["id"]))
        self.assertIsNone(await self.db.accept_owner_transfer("9", 30, second["id"], self.members))
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 10)

    async def test_cancellation_rechecks_the_owners_current_approval(self):
        for status in ("pending", "blocked"):
            with self.subTest(status=status):
                invitation = await self.create()
                await self.db.set_status(10, status)
                self.assertIsNone(await self.db.cancel_owner_transfer("9", 10, invitation["id"]))
                self.assertEqual(await self.db.pool.fetchval("SELECT id FROM owner_transfers"), invitation["id"])
                await self.db.set_status(10, "approved")

    async def test_expired_invitation_cannot_be_read_or_accepted(self):
        invitation = await self.create()
        await self.db.pool.execute("UPDATE owner_transfers SET expires_at = clock_timestamp() - interval '1 second'")
        self.assertIsNone(await self.db.owner_transfer("9"))
        self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 10)

    async def test_acceptance_rechecks_both_participants_live_membership(self):
        invitation = await self.create()
        for members in (["2", "3"], ["1", "3"]):
            with self.subTest(members=members):
                self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], members))
                self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 10)

    async def test_relogin_or_steam_identity_change_invalidates_each_participants_invitation(self):
        for telegram_id in (10, 20):
            for replacement in (str(telegram_id // 10), f"new-{telegram_id}"):
                with self.subTest(telegram_id=telegram_id, replacement=replacement):
                    invitation = await self.create()
                    await self.db.save_account(telegram_id, replacement, "9", "replacement")
                    await self.db.set_status(telegram_id, "approved")
                    await self.db.update_family("9", owner_telegram_id=10)
                    self.assertIsNone(await self.db.owner_transfer("9"))
                    self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))
                    await self.db.save_account(telegram_id, str(telegram_id // 10), "9", "restored")
                    await self.db.set_status(telegram_id, "approved")
                    await self.db.update_family("9", owner_telegram_id=10)
                    self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))

    async def test_block_then_approve_cannot_revive_an_invitation(self):
        for telegram_id in (10, 20):
            for status in ("pending", "blocked"):
                with self.subTest(telegram_id=telegram_id, status=status):
                    invitation = await self.create()
                    await self.db.set_status(telegram_id, status)
                    self.assertIsNone(await self.db.owner_transfer("9"))
                    await self.db.set_status(telegram_id, "approved")
                    self.assertIsNone(await self.db.owner_transfer("9"))
                    self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))

    async def test_credential_rotation_preserves_invitation_but_revocation_does_not(self):
        invitation = await self.create()
        for telegram_id in (10, 20):
            await self.db.set_refresh_token(telegram_id, "rotated")
        self.assertEqual(await self.db.owner_transfer("9"), invitation)
        await self.db.set_refresh_token(20, None)
        self.assertIsNone(await self.db.owner_transfer("9"))
        await self.db.set_refresh_token(20, "restored")
        self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))

    async def test_owner_changes_including_change_back_permanently_remove_invitation(self):
        invitation = await self.create()
        await self.db.update_family("9", owner_telegram_id=30)
        await self.db.update_family("9", owner_telegram_id=10)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM owner_transfers"), 0)
        self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))

    async def test_logout_erases_the_invitation_and_recreation_cannot_accept_it(self):
        for telegram_id in (10, 20):
            with self.subTest(telegram_id=telegram_id):
                invitation = await self.create()
                await self.db.delete_user(telegram_id)
                self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM owner_transfers"), 0)
                await self.db.save_account(telegram_id, str(telegram_id // 10), "9", "replacement")
                await self.db.set_status(telegram_id, "approved")
                await self.db.update_family("9", owner_telegram_id=10)
                self.assertIsNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))

    async def test_two_concurrent_acceptances_change_owner_once(self):
        invitation = await self.create()
        results = await asyncio.wait_for(
            asyncio.gather(*(self.db.accept_owner_transfer("9", 20, invitation["id"], self.members) for _ in range(2))),
            timeout=5,
        )
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 20)
        self.assertIsNone(await self.db.owner_transfer("9"))

    async def test_concurrent_cancel_and_accept_consume_the_invitation_once(self):
        invitation = await self.create()
        cancelled, accepted = await asyncio.wait_for(
            asyncio.gather(
                self.db.cancel_owner_transfer("9", 10, invitation["id"]),
                self.db.accept_owner_transfer("9", 20, invitation["id"], self.members),
            ),
            timeout=5,
        )
        self.assertEqual(sum(result is not None for result in (cancelled, accepted)), 1)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 20 if accepted else 10)

    async def test_acceptance_uses_family_before_account_lock_order(self):
        invitation = await self.create()
        acceptance = None
        try:
            async with self.db.pool.acquire() as connection, connection.transaction():
                await connection.fetchval("SELECT family_id FROM families WHERE family_id = '9' FOR UPDATE")
                acceptance = asyncio.create_task(self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(acceptance), 0.05)
                await asyncio.wait_for(
                    connection.execute("UPDATE accounts SET country = 'KZ' WHERE telegram_id = 20"), 3
                )
        finally:
            if acceptance is not None:
                await asyncio.wait_for(acceptance, 5)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 20)

    async def test_failed_owner_update_rolls_back_invitation_consumption(self):
        invitation = await self.create()
        await self.db.pool.execute("""
            CREATE FUNCTION reject_owner_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'owner update unavailable'; END $$;
            CREATE TRIGGER reject_owner_transfer BEFORE UPDATE OF owner_telegram_id ON families
            FOR EACH ROW EXECUTE FUNCTION reject_owner_transfer();
        """)
        with self.assertRaisesRegex(asyncpg.RaiseError, "owner update unavailable"):
            await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members)
        self.assertEqual(await self.db.owner_transfer("9"), invitation)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 10)

    async def test_logout_handover_rechecks_target_and_cannot_overwrite_a_new_owner(self):
        await self.db.set_status(20, "blocked")
        self.assertIsNone(await self.db.handover_owner("9", 10, 20))
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        self.assertEqual((await self.db.account(20))["status"], "blocked")
        await self.db.update_family("9", owner_telegram_id=30)
        self.assertIsNone(await self.db.handover_owner("9", 10, 20))
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 30)
        self.assertEqual(await self.db.handover_owner("9", 30, 10), 10)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 10)

    async def test_explicit_owner_recovery_cannot_approve_a_replaced_or_revoked_session(self):
        await self.db.update_family("9", owner_telegram_id=None)
        generation = (await self.db.account(20))["token_generation"]
        await self.db.save_account(20, "2", "9", "replacement")
        await self.db.set_status(20, "pending")
        await self.db.claim_initial_owner("9", 20, expected_generation=generation)
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        self.assertEqual((await self.db.account(20))["status"], "pending")
        current_generation = (await self.db.account(20))["token_generation"]
        await self.db.set_refresh_token(20, None)
        await self.db.claim_initial_owner("9", 20, expected_generation=current_generation)
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        self.assertEqual((await self.db.account(20))["status"], "pending")
        await self.db.save_account(20, "2", "9", "new-login")
        generation = (await self.db.account(20))["token_generation"]
        await self.db.claim_initial_owner("9", 20, expected_generation=generation)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 20)
        self.assertEqual((await self.db.account(20))["status"], "approved")

    async def test_upgrade_preserves_existing_data_and_is_repeatable(self):
        family = await self.db.family("9")
        accounts = await self.db.family_accounts("9")
        await self.db.set_game_status(20, "2", "9", 440, "Game", "playing")
        await self.db.pool.execute("""
            DROP TRIGGER IF EXISTS invalidate_owner_transfer ON families;
            DROP FUNCTION IF EXISTS invalidate_owner_transfer();
            DROP TABLE IF EXISTS owner_transfers;
            DELETE FROM schema_migrations WHERE version = 9;
        """)
        async with self.db.pool.acquire() as connection:
            await migrate(connection)
            await migrate(connection)
        self.assertEqual(await self.db.family("9"), family)
        self.assertEqual(await self.db.family_accounts("9"), accounts)
        self.assertEqual(await self.db.game_marks(20), {440: {"name": "Game", "status": "playing"}})
        invitation = await self.create()
        self.assertIsNotNone(await self.db.accept_owner_transfer("9", 20, invitation["id"], self.members))
