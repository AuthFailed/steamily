import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from asyncpg import CheckViolationError

from bot.db import SteamAlreadyLinked
from bot.family import Families, role
from bot.steam import NoFamily
from bot.steam_auth import TokenRejected
from tests.pg import fresh_db, needs_pg

ALICE, BOB = "76561198000000001", "76561198000000002"


def snapshot(apps, family_id="9"):
    return {
        "family_id": family_id,
        "name": "Fam",
        "members": [ALICE, BOB],
        "names": {ALICE: "Alice", BOB: "Bob"},
        "apps": apps,
    }


@needs_pg
class FamiliesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        self.bot = AsyncMock()
        self.families = Families(self.db, http=None, bot=self.bot)
        self.patches = [
            patch("bot.family.update_store_cache", AsyncMock(return_value=False)),
            patch("bot.family.fetch_user_country", AsyncMock(return_value="KZ")),
            patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(return_value="access")),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.db.pool.close()

    async def test_first_registrant_owns_and_new_games_are_announced(self):
        portal = {"appid": 620, "name": "Portal 2", "owners": [ALICE], "acquired": 1}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([portal]))):
            await self.families.register(1, "refresh", ALICE)
            await self.families.register(2, "refresh", BOB)
        account = await self.db.account(1)
        self.assertEqual((account["status"], account["country"], account["owner_telegram_id"]), ("approved", "KZ", 1))
        self.assertEqual((await self.db.account(2))["status"], "pending")
        self.assertEqual(set(self.families.stores), {"KZ", "US"})  # prices for every member's currency
        self.bot.send_message.assert_not_called()  # the first check is a silent baseline

        await self.db.update_family("9", notify_chat=-100)
        cs = {"appid": 730, "name": "CS2", "owners": [BOB], "acquired": 2}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([portal, cs]))):
            await self.families.refresh("9")
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.args[0], -100)
        self.assertIn("CS2", self.bot.send_message.await_args.args[1])

    async def test_concurrent_registration_keeps_one_link_and_its_owner(self):
        ready = asyncio.Event()
        arrivals = 0

        async def fetch_snapshot(*args):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await ready.wait()  # both registrations passed the initial link check
            return snapshot([])

        with patch("bot.family.fetch_family_snapshot", side_effect=fetch_snapshot):
            results = await asyncio.wait_for(
                asyncio.gather(
                    self.families.register(1, "first", ALICE),
                    self.families.register(2, "second", ALICE),
                    return_exceptions=True,
                ),
                timeout=5,
            )
        self.assertEqual(sum(isinstance(result, SteamAlreadyLinked) for result in results), 1)
        accounts = await self.db.family_accounts("9")
        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account["steam_id"], ALICE)
        self.assertEqual(account["status"], "approved")
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], account["telegram_id"])
        self.assertEqual(self.db.refresh_token(account), "first" if account["telegram_id"] == 1 else "second")

    async def test_concurrent_first_registrations_approve_only_the_owner(self):
        await self.db.ensure_family("9", "Fam")
        for telegram_id, status in ((3, "approved"), (4, "blocked")):
            await self.db.save_account(telegram_id, str(telegram_id), "9", "refresh", "KZ")
            await self.db.set_status(telegram_id, status)
        saved, unclaimed = asyncio.Barrier(2), asyncio.Barrier(2)
        save_account, family = self.db.save_account, self.db.family

        async def save_together(*args, **kwargs):
            await save_account(*args, **kwargs)
            await saved.wait()

        async def read_together(family_id):
            row = await family(family_id)
            if row["owner_telegram_id"] is None:
                await unclaimed.wait()  # force both old check-then-write paths to see no owner
            return row

        with (
            patch(
                "bot.family.fetch_family_snapshot",
                AsyncMock(return_value={**snapshot([]), "members": [ALICE, BOB, "3", "4"]}),
            ),
            patch.object(self.db, "save_account", save_together),
            patch.object(self.db, "family", read_together),
        ):
            await asyncio.wait_for(
                asyncio.gather(
                    self.families.register(1, "refresh", ALICE),
                    self.families.register(2, "refresh", BOB),
                ),
                timeout=10,
            )
        owner = (await self.db.family("9"))["owner_telegram_id"]
        self.assertIn(owner, (1, 2))
        statuses = {a["telegram_id"]: a["status"] for a in await self.db.family_accounts("9")}
        self.assertEqual(statuses, {owner: "approved", 3 - owner: "pending", 3: "approved", 4: "blocked"})

    async def test_repeated_registration_preserves_owner_and_member_statuses(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
            await self.families.register(2, "refresh", BOB)
            await self.db.set_status(2, "approved")
            await self.families.register(3, "refresh", "76561198000000003")
            await self.db.set_status(3, "blocked")
            for telegram_id, steam_id in ((1, ALICE), (2, BOB), (3, "76561198000000003")):
                await self.families.register(telegram_id, "replacement", steam_id)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 1)
        statuses = {a["telegram_id"]: a["status"] for a in await self.db.family_accounts("9")}
        self.assertEqual(statuses, {1: "approved", 2: "approved", 3: "blocked"})

    async def test_failed_initial_approval_rolls_back_owner_claim(self):
        await self.db.pool.execute(
            "ALTER TABLE accounts ADD CONSTRAINT reject_owner_approval CHECK (telegram_id <> 1 OR status <> 'approved')"
        )
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            with self.assertRaisesRegex(CheckViolationError, "reject_owner_approval"):
                await self.families.register(1, "refresh", ALICE)
            self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
            self.assertEqual((await self.db.account(1))["status"], "pending")
            await self.families.register(2, "refresh", BOB)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 2)
        self.assertEqual((await self.db.account(2))["status"], "approved")

    async def test_rejected_token_is_dropped_but_outage_keeps_it(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        with patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(side_effect=OSError("network down"))):
            with self.assertRaises(OSError):
                await self.families.refresh("9")
        self.assertIsNotNone((await self.db.account(1))["refresh_token"])
        with patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(side_effect=TokenRejected("revoked"))):
            await self.families.refresh("9")
        self.assertIsNone((await self.db.account(1))["refresh_token"])

    async def test_missing_country_is_filled_on_refresh(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
            await self.db.pool.execute("UPDATE accounts SET country = NULL")
            with patch("bot.family.fetch_user_country", AsyncMock(return_value="TR")):
                await self.families.refresh("9")
        self.assertEqual((await self.db.account(1))["country"], "TR")
        self.assertIn("TR", self.families.stores)

    async def test_owner_logout_hands_the_family_over(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
            await self.families.register(2, "refresh", BOB)
            await self.families.register(3, "refresh", "76561198000000003")
        await self.db.set_language(1, "ru")
        await self.db.set_status(3, "blocked")  # blocked members never inherit

        self.assertEqual(await self.families.leave(1), 2)
        self.assertIsNone(await self.db.account(1))
        self.assertIsNone(await self.db.language(1))  # settings are gone too
        new_owner = await self.db.account(2)
        self.assertEqual((new_owner["owner_telegram_id"], new_owner["status"]), (2, "approved"))

        self.assertIsNone(await self.families.leave(2))  # nobody left to inherit
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        await self.db.set_status(3, "pending")
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(4, "refresh", BOB)  # the next to register becomes owner
        self.assertEqual((await self.db.account(4))["owner_telegram_id"], 4)

    async def test_member_logout_keeps_the_owner(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
            await self.families.register(2, "refresh", BOB)
        self.assertIsNone(await self.families.leave(2))
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 1)

    async def test_presence_and_membership_alerts_go_to_the_group(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        await self.db.update_family("9", notify_chat=-100, alerts={"playing": True})
        sent = self.bot.send_message
        sent.reset_mock()
        self.families.presence["9"] = {ALICE: {"online": True, "game": None}}
        with patch(
            "bot.family.fetch_presence",
            AsyncMock(return_value={ALICE: {"online": True, "game": 620, "game_name": "Portal 2"}}),
        ):
            await self.families.check_presence("9")
        self.assertEqual(sent.await_args.args[:2], (-100, "🎮 Alice started playing Portal 2"))
        self.assertEqual(self.families.items["9"].snapshot["players"][ALICE]["game"], 620)

        sent.reset_mock()
        moved = {**snapshot([]), "members": [ALICE]}  # BOB left; "members" alerts are on by default
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=moved)):
            await self.families.refresh("9")
        self.assertIn("left the Steam family", sent.await_args.args[1])

    async def test_member_who_moved_family_is_not_used(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([], family_id="other"))):
            await self.families.refresh("9")
        self.assertEqual((await self.db.family("9"))["state"]["ownership"], {})

    async def test_snapshot_from_bob_revokes_only_departed_alice(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "approved")
        current = {**snapshot([]), "members": [BOB]}
        with (
            patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(side_effect=[TokenRejected("gone"), "bob"])),
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)),
        ):
            await self.families.refresh("9")
        self.assertEqual(role(await self.db.account(1)), "pending")
        self.assertEqual(role(await self.db.account(2)), "owner")
        self.assertEqual(self.families.get("9").members, [BOB])

    async def test_unvalidated_steam_failures_keep_approval_and_owner(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        before = await self.db.account(1)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=RuntimeError("Steam HTTP 503"))):
            await self.families.refresh("9")
        self.assertEqual(await self.db.account(1), before)

    async def test_valid_different_family_revokes_old_access_even_without_another_login(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([], family_id="other"))):
            await self.families.refresh("9")
        self.assertEqual(role(await self.db.account(1)), "pending")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_confirmed_no_family_revokes_old_access(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=NoFamily("left every family"))):
            await self.families.refresh("9")
        self.assertEqual(role(await self.db.account(1)), "pending")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_confirmed_no_family_during_relogin_revokes_old_access(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=NoFamily("left every family"))):
            with self.assertRaises(NoFamily):
                await self.families.register(1, "replacement", ALICE)
        self.assertEqual(role(await self.db.account(1)), "pending")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_failed_login_to_a_different_steam_account_keeps_original_access(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        before = await self.db.account(1)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=NoFamily("bob has no family"))):
            with self.assertRaises(NoFamily):
                await self.families.register(1, "bob", BOB)
        self.assertEqual(await self.db.account(1), before)

    async def test_blocked_relogin_cannot_inherit_departed_owners_access(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "blocked")
        current = {**snapshot([]), "members": [BOB]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)):
            await self.families.refresh("9")
            await self.families.register(2, "bob-new", BOB)
        self.assertEqual(role(await self.db.account(1)), "pending")
        self.assertEqual(role(await self.db.account(2)), "blocked")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_relogin_into_another_family_requires_approval_and_resolves_old_owner(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "approved")
        carol = "76561198000000003"
        target = {**snapshot([], family_id="10"), "members": [ALICE, carol]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=target)):
            await self.families.register(3, "carol", carol)

        async def read_family(http, access_token):
            return target if access_token in (ALICE, carol) else {**snapshot([]), "members": [BOB]}

        async def token(auth):
            return auth.steam_id

        with (
            patch("bot.steam_auth.SteamAuth.get_access_token", new=token),
            patch("bot.family.fetch_family_snapshot", side_effect=read_family),
        ):
            await self.families.register(1, "alice-new", ALICE)
        alice = await self.db.account(1)
        self.assertEqual((alice["family_id"], alice["status"]), ("10", "pending"))
        self.assertEqual(role(await self.db.account(2)), "owner")
        self.assertEqual(role(await self.db.account(3)), "owner")

    async def test_replacing_owners_steam_account_does_not_preempt_an_approved_member(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "approved")
        carol = "76561198000000003"
        with patch(
            "bot.family.fetch_family_snapshot", AsyncMock(return_value={**snapshot([]), "members": [BOB, carol]})
        ):
            await self.families.register(1, "carol", carol)
        self.assertEqual(role(await self.db.account(1)), "pending")
        self.assertEqual(role(await self.db.account(2)), "owner")

    async def test_logout_handover_cannot_restore_access_revoked_by_a_concurrent_poll(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        selected, resume = asyncio.Event(), asyncio.Event()
        make_owner = self.families.make_owner

        async def paused_handover(*args):
            selected.set()
            await resume.wait()
            await make_owner(*args)

        with (
            patch.object(self.families, "make_owner", paused_handover),
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value={**snapshot([]), "members": [ALICE]})),
        ):
            logout = asyncio.create_task(self.families.leave(1))
            await asyncio.wait_for(selected.wait(), timeout=5)
            refresh = asyncio.create_task(self.families.refresh("9"))
            try:
                await asyncio.wait({refresh}, timeout=0.1)
            finally:
                resume.set()
            await asyncio.wait_for(asyncio.gather(logout, refresh), timeout=5)
        self.assertEqual(role(await self.db.account(2)), "pending")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_logout_handover_cannot_approve_a_member_moving_to_another_family(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        carol = "76561198000000003"
        target = {**snapshot([], family_id="10"), "members": [BOB, carol]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=target)):
            await self.families.register(3, "carol", carol)
        selected, resume = asyncio.Event(), asyncio.Event()
        make_owner = self.families.make_owner

        async def paused_handover(*args):
            selected.set()
            await resume.wait()
            await make_owner(*args)

        with (
            patch.object(self.families, "make_owner", paused_handover),
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=target)),
        ):
            logout = asyncio.create_task(self.families.leave(1))
            await asyncio.wait_for(selected.wait(), timeout=5)
            migration = asyncio.create_task(self.families.register(2, "bob-new", BOB))
            try:
                await asyncio.wait({migration}, timeout=0.1)
            finally:
                resume.set()
            await asyncio.wait_for(asyncio.gather(logout, migration), timeout=5)
        bob = await self.db.account(2)
        self.assertEqual((bob["family_id"], bob["status"]), ("10", "pending"))
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_logout_uses_current_family_if_account_moves_while_waiting_for_lock(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        carol = "76561198000000003"
        target = {**snapshot([], family_id="10"), "members": [ALICE, carol]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=target)):
            await self.families.register(3, "carol", carol)
        read = asyncio.Event()
        account = self.db.account

        async def read_account(telegram_id):
            result = await account(telegram_id)
            read.set()
            return result

        with patch.object(self.db, "account", read_account):
            async with self.families.get("9").lock:
                logout = asyncio.create_task(self.families.leave(1))
                await asyncio.wait_for(read.wait(), timeout=5)
                # Complete a migration while logout is queued behind the old family's operation.
                await self.db.save_account(1, ALICE, "10", "alice-new")
                await self.families.make_owner("10", 1)
            await asyncio.wait_for(logout, timeout=5)
        self.assertIsNone(await self.db.account(1))
        self.assertEqual(role(await self.db.account(3)), "owner")

    async def test_valid_registration_snapshot_reconciles_even_when_next_poll_fails(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "approved")
        current = {**snapshot([]), "members": [BOB]}
        with patch(
            "bot.family.fetch_family_snapshot",
            AsyncMock(side_effect=[current, RuntimeError("Steam HTTP 503"), RuntimeError("Steam HTTP 503")]),
        ):
            await self.families.register(2, "bob-new", BOB)
        self.assertEqual(role(await self.db.account(1)), "pending")
        self.assertEqual(role(await self.db.account(2)), "owner")

    async def test_pending_owner_field_does_not_grant_access(self):
        self.assertEqual(role({"telegram_id": 1, "owner_telegram_id": 1, "status": "pending"}), "pending")

    async def test_logout_does_not_promote_a_departed_pending_member(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        self.families.get("9").snapshot["members"] = [ALICE]
        self.assertIsNone(await self.families.leave(1))
        self.assertEqual(role(await self.db.account(2)), "pending")


@needs_pg
class RegistrationTokenTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        self.families = Families(self.db, http=None, bot=AsyncMock())
        self.patches = [
            patch("bot.family.update_store_cache", AsyncMock(return_value=False)),
            patch("bot.family.fetch_user_country", AsyncMock(return_value="KZ")),
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))),
            patch("bot.steam_auth.time.time", return_value=100_000.0),
            patch("bot.steam_auth.call", AsyncMock(return_value={1: b"access", 2: b"rotated"})),
        ]
        self.steam_call = self.patches[-1].start()
        for p in self.patches[:-1]:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.db.pool.close()

    async def test_first_registration_keeps_replacement_token_after_restart(self):
        await self.families.register(1, "original", ALICE)
        account = await self.db.account(1)
        self.assertEqual(self.db.refresh_token(account), "rotated")
        self.assertEqual(account["token_renewed_at"], 100_000.0)
        self.assertEqual([dict(c.args[2])[3] for c in self.steam_call.await_args_list], [1, 0])

        restarted = Families(self.db, http=None, bot=AsyncMock())
        self.steam_call.reset_mock()
        await restarted.refresh("9")
        fields = dict(self.steam_call.await_args.args[2])
        self.assertEqual((fields[1], fields[3]), ("rotated", 0))
        self.assertEqual((await self.db.account(1))["token_renewed_at"], 100_000.0)

    async def test_relogin_keeps_rotated_token_and_existing_approval(self):
        await self.families.register(1, "original", ALICE)
        with patch("bot.steam_auth.time.time", return_value=200_000.0):
            self.steam_call.return_value = {1: b"access", 2: b"relogin-rotated"}
            await self.families.register(1, "new-login", ALICE)
        account = await self.db.account(1)
        self.assertEqual(self.db.refresh_token(account), "relogin-rotated")
        self.assertEqual((account["token_renewed_at"], account["status"]), (200_000.0, "approved"))

        restarted = Families(self.db, http=None, bot=AsyncMock())
        self.steam_call.reset_mock()
        with patch("bot.steam_auth.time.time", return_value=200_001.0):
            await restarted.refresh("9")
        fields = dict(self.steam_call.await_args.args[2])
        self.assertEqual((fields[1], fields[3]), ("relogin-rotated", 0))

    async def test_relogin_within_day_keeps_renewal_time(self):
        await self.families.register(1, "original", ALICE)
        self.steam_call.reset_mock()
        self.steam_call.return_value = {1: b"access"}
        with patch("bot.steam_auth.time.time", return_value=100_001.0):
            await self.families.register(1, "new-login", ALICE)
        account = await self.db.account(1)
        self.assertEqual((self.db.refresh_token(account), account["token_renewed_at"]), ("new-login", 100_000.0))
        self.assertTrue(all(dict(c.args[2])[3] == 0 for c in self.steam_call.await_args_list))

    async def test_duplicate_link_is_rejected_before_steam_calls_or_account_changes(self):
        await self.families.register(1, "original", ALICE)
        await self.families.register(2, "other", BOB)
        await self.db.set_telegram_profile(1, "alice_tg", "Alice")
        await self.db.set_language(1, "ru")
        await self.db.set_notify_requests(1, False)
        await self.db.update_family("9", alerts={"online": True}, notify_chat=-100)

        for status in ("approved", "pending", "blocked"):
            await self.db.set_status(1, status)
            accounts = await self.db.family_accounts("9")
            family = await self.db.family("9")
            cached_auths = dict(self.families.auths)
            self.steam_call.reset_mock()
            for telegram_id in (2, 3):  # an existing different link and a first login
                with self.subTest(status=status, telegram_id=telegram_id):
                    with self.assertRaises(SteamAlreadyLinked):
                        await self.families.register(telegram_id, "duplicate", ALICE)
                    self.assertEqual(await self.db.family_accounts("9"), accounts)
                    self.assertEqual(await self.db.family("9"), family)
                    self.assertEqual(self.families.auths, cached_auths)
            self.steam_call.assert_not_awaited()
        self.assertEqual(await self.db.language(1), "ru")
        self.assertFalse(await self.db.notify_requests(1))

    async def test_different_steam_account_does_not_inherit_renewal_time(self):
        await self.families.register(1, "original", ALICE)
        self.steam_call.reset_mock()
        with patch("bot.steam_auth.time.time", return_value=100_001.0):
            await self.families.register(1, "other-login", BOB)
        account = await self.db.account(1)
        self.assertEqual((account["steam_id"], account["token_renewed_at"]), (BOB, 100_001.0))
        self.assertEqual(dict(self.steam_call.await_args_list[0].args[2])[3], 1)

    async def test_cached_auth_keeps_pending_credentials_during_database_failure(self):
        await self.families.register(1, "original", ALICE)
        account = await self.db.account(1)
        self.steam_call.return_value = {1: b"x.eyJleHAiOjMwMDAwMH0.y", 2: b"pending"}
        self.steam_call.reset_mock()
        with (
            patch("bot.steam_auth.time.time", return_value=200_000.0),
            patch.object(self.db, "set_refresh_token", AsyncMock(side_effect=OSError("unavailable"))),
        ):
            with self.assertRaises(OSError):
                await self.families.owner_token(account)
        cached = self.families.auths[1]
        with patch("bot.steam_auth.time.time", return_value=200_001.0):
            await self.families.owner_token(await self.db.account(1))
        self.assertIs(self.families.auths[1], cached)
        self.steam_call.assert_awaited_once()
        account = await self.db.account(1)
        self.assertEqual((self.db.refresh_token(account), account["token_renewed_at"]), ("pending", 200_000.0))

    async def test_failed_registration_does_not_replace_saved_credentials(self):
        await self.families.register(1, "original", ALICE)
        with (
            patch("bot.steam_auth.time.time", return_value=200_000.0),
            patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=OSError("unavailable"))),
        ):
            self.steam_call.return_value = {1: b"access", 2: b"uncommitted"}
            with self.assertRaises(OSError):
                await self.families.register(1, "new-login", ALICE)
        account = await self.db.account(1)
        self.assertEqual((self.db.refresh_token(account), account["token_renewed_at"]), ("rotated", 100_000.0))


if __name__ == "__main__":
    unittest.main()
