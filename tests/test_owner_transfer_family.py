import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot.family import Families, role
from tests.pg import fresh_db, needs_pg


@needs_pg
class OwnerTransferFamilyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        self.families = Families(self.db, http=None, bot=AsyncMock())
        await self.db.ensure_family("9", "Family")
        for telegram_id, steam_id in ((10, "1"), (20, "2"), (30, "3")):
            await self.db.save_account(telegram_id, steam_id, "9", "token")
            await self.db.set_status(telegram_id, "approved")
        await self.db.update_family("9", owner_telegram_id=10)
        self.snapshot = {"family_id": "9", "members": ["1", "2", "3"], "names": {}, "apps": []}
        self.families.get("9").snapshot = self.snapshot
        self.fetch = AsyncMock(return_value=self.snapshot)
        fetch_patch = patch.object(self.families, "_fetch", self.fetch)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def test_transfer_requires_acceptance_and_preserves_approval_and_reader(self):
        await self.db.update_family("9", reader_telegram_id=10)
        invitation = await self.families.create_owner_transfer("9", 10, 20)
        self.assertIsNotNone(invitation)
        self.assertEqual(role(await self.db.account(10)), "owner")
        self.fetch.reset_mock()
        result = await self.families.accept_owner_transfer("9", 20, invitation["id"])
        self.assertIsNotNone(result)
        self.fetch.assert_awaited_once_with("9")
        self.assertEqual(role(await self.db.account(20)), "owner")
        self.assertEqual(role(await self.db.account(10)), "member")
        self.assertEqual((await self.db.family("9"))["reader_telegram_id"], 10)

    async def test_wrong_actor_cannot_initiate_or_accept(self):
        self.assertIsNone(await self.families.create_owner_transfer("9", 20, 30))
        invitation = await self.families.create_owner_transfer("9", 10, 20)
        self.fetch.reset_mock()
        self.assertIsNone(await self.families.accept_owner_transfer("9", 30, invitation["id"]))
        self.fetch.assert_not_awaited()
        self.assertEqual(role(await self.db.account(10)), "owner")

    async def test_departure_of_either_party_rejects_acceptance(self):
        for departed in ("1", "2"):
            with self.subTest(departed=departed):
                self.fetch.return_value = self.snapshot
                invitation = await self.families.create_owner_transfer("9", 10, 20)
                self.fetch.return_value = {**self.snapshot, "members": [s for s in ["1", "2", "3"] if s != departed]}
                self.assertIsNone(await self.families.accept_owner_transfer("9", 20, invitation["id"]))
                self.assertEqual(role(await self.db.account(10)), "owner")

    async def test_steam_outage_does_not_use_cached_membership_for_acceptance(self):
        invitation = await self.families.create_owner_transfer("9", 10, 20)
        for failure in (None, TimeoutError(), RuntimeError("Steam unavailable")):
            with self.subTest(failure=failure):
                self.fetch.return_value = failure
                self.fetch.side_effect = failure if isinstance(failure, Exception) else None
                self.assertIsNone(await self.families.accept_owner_transfer("9", 20, invitation["id"]))
                self.assertEqual(role(await self.db.account(10)), "owner")

    async def test_wrong_family_snapshot_does_not_grant_ownership(self):
        invitation = await self.families.create_owner_transfer("9", 10, 20)
        self.fetch.return_value = {**self.snapshot, "family_id": "other"}
        self.assertIsNone(await self.families.accept_owner_transfer("9", 20, invitation["id"]))

    async def test_membership_fetch_cannot_restore_a_concurrently_blocked_target(self):
        invitation = await self.families.create_owner_transfer("9", 10, 20)

        async def fetch_and_block(family_id):
            await self.db.set_status(20, "blocked")
            return self.snapshot

        self.fetch.side_effect = fetch_and_block
        self.assertIsNone(await self.families.accept_owner_transfer("9", 20, invitation["id"]))
        self.assertEqual(role(await self.db.account(20)), "blocked")

    async def test_two_acceptances_only_apply_once(self):
        invitation = await self.families.create_owner_transfer("9", 10, 20)
        results = await asyncio.gather(
            self.families.accept_owner_transfer("9", 20, invitation["id"]),
            self.families.accept_owner_transfer("9", 20, invitation["id"]),
        )
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(role(await self.db.account(20)), "owner")

    async def test_logout_and_subsequent_poll_never_promote_pending_or_blocked_members(self):
        await self.db.set_status(20, "pending")
        await self.db.set_status(30, "blocked")
        with patch("bot.steam_auth.SteamAuth.revoke", AsyncMock()):
            self.assertIsNone(await self.families.leave(10))
        await self.db.reconcile_members("9", ["2", "3"])
        self.assertEqual(role(await self.db.account(20)), "pending")
        self.assertEqual(role(await self.db.account(30)), "blocked")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
