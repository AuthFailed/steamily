import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from asyncpg import CheckViolationError

from bot.db import SteamAlreadyLinked
from bot.family import Families, SessionRevocationFailed, role
from bot.steam import NoFamily
from bot.steam_auth import SteamAuth, TokenRejected
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
            patch("bot.steam_auth.SteamAuth.revoke", AsyncMock(), create=True),
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

        current = {**snapshot([]), "members": [ALICE, BOB, "3", "4"]}
        with (
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)),
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
        current = {**snapshot([]), "members": [ALICE, BOB, "76561198000000003"]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)):
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
            self.assertIsNone(await self.db.account(1))
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
        restarted = Families(self.db, http=None, bot=self.bot)
        with patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(side_effect=TokenRejected("revoked"))):
            await restarted.refresh("9")
        self.assertIsNone((await self.db.account(1))["refresh_token"])
        self.assertEqual((await self.db.account(1))["status"], "approved")

    async def test_missing_country_is_filled_on_refresh(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
            await self.db.pool.execute("UPDATE accounts SET country = NULL")
            with patch("bot.family.fetch_user_country", AsyncMock(return_value="TR")):
                await self.families.refresh("9")
        self.assertEqual((await self.db.account(1))["country"], "TR")
        self.assertIn("TR", self.families.stores)

    async def test_owner_logout_hands_the_family_over(self):
        current = {**snapshot([]), "members": [ALICE, BOB, "76561198000000003"]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)):
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

    async def test_last_logout_clears_runtime_data_and_cannot_reuse_old_credentials(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        account = await self.db.account(1)
        auth = self.families.auths[1]
        self.families.stats_cache[ALICE] = (0, {"level": 10})
        self.families.stats_cache[BOB] = (0, {"level": 20})
        self.families.stats_cache["76561198000000003"] = (0, {"level": 30})  # a former family member
        self.assertIn("9", self.families.presence)
        await self.families.leave(1)
        self.assertNotIn(1, self.families.auths)
        self.assertEqual(self.families.stats_cache, {})
        self.assertNotIn("9", self.families.items)
        self.assertNotIn("9", self.families.presence)
        self.assertNotIn("9", self.families.delivery_locks)
        self.assertNotIn("9", self.families.last_family_check)
        self.assertNotIn("9", self.families.health.families)
        self.assertNotIn("9", self.families.health.first_seen)
        tasks = {name: Mock(done=lambda: False) for name in self.families.health.workers}
        self.assertEqual(self.families.health.snapshot(tasks)["steam"]["families"], [])
        with self.assertRaises(TokenRejected):
            await self.families.owner_token(account)
        with self.assertRaises(TokenRejected):
            await auth.on_renew("late-token", 123)
        restarted = Families(self.db, http=None, bot=self.bot)
        self.assertIsNone(await restarted.any_token("9"))
        self.assertIsNone(await self.db.family("9"))

    async def test_admin_removal_clears_user_cache_and_keeps_shared_family(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        family = self.families.items["9"]
        await self.families.owner_token(await self.db.account(2))
        self.families.stats_cache[ALICE] = (0, {"level": 1})
        self.families.stats_cache[BOB] = (0, {"level": 2})
        self.assertFalse(await self.families.remove(2, family_id="unrelated"))
        self.assertIn(2, self.families.auths)
        self.assertTrue(await self.families.remove(2, family_id="9"))
        self.assertNotIn(2, self.families.auths)
        self.assertNotIn(BOB, self.families.stats_cache)
        self.assertIn(ALICE, self.families.stats_cache)
        self.assertIs(self.families.items["9"], family)
        self.assertIn("9", self.families.presence)
        self.assertIn("9", self.families.health.families)
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 1)

    async def test_inflight_snapshot_cannot_restore_a_deleted_family(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        started, released = asyncio.Event(), asyncio.Event()

        async def fetch(http, token):
            started.set()
            await released.wait()
            return snapshot([])

        with patch("bot.family.fetch_family_snapshot", fetch):
            poll = asyncio.create_task(self.families.refresh("9"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.families.remove(1, family_id="9")
            finally:
                released.set()
                await poll
        self.assertNotIn("9", self.families.items)
        self.assertNotIn("9", self.families.last_family_check)
        self.assertIsNone(await self.db.family("9"))

    async def test_inflight_member_stats_are_not_cached_after_deletion(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        started, released = asyncio.Event(), asyncio.Event()

        async def fetch(http, token, steam_id):
            started.set()
            await released.wait()
            return {"level": 10}

        with patch("bot.family.member_stats", fetch):
            request = asyncio.create_task(self.families.member_stats("9", ALICE))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.families.leave(1)
            finally:
                released.set()
                await request
        self.assertNotIn(ALICE, self.families.stats_cache)

    async def test_removal_during_observation_cannot_restore_snapshot_or_pending_alerts(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        await self.db.update_family("9", notify_chat=-100)
        started, released = asyncio.Event(), asyncio.Event()
        observe = self.db.observe_family

        async def delayed_observe(*args):
            started.set()
            await released.wait()
            await observe(*args)

        current = snapshot([{"appid": 620, "name": "Portal 2", "owners": [ALICE]}])
        with (
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)),
            patch.object(self.db, "observe_family", side_effect=delayed_observe),
        ):
            check = asyncio.create_task(self.families.refresh("9"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.families.remove(1, family_id="9")
            finally:
                released.set()
                await check
        self.assertNotIn("9", self.families.items)
        self.assertNotIn("9", self.families.presence)
        self.assertNotIn("9", self.families.last_family_check)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts"), 0)
        self.bot.send_message.assert_not_awaited()

    async def test_status_time_advances_only_after_a_successful_steam_snapshot(self):
        with (
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))),
            patch("bot.family.time.time", return_value=123.0),
        ):
            await self.families.register(1, "alice", ALICE)
        self.assertEqual(self.families.last_steam_check, 123.0)
        self.assertEqual(self.families.last_family_check, {"9": 123.0})
        with (
            patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=RuntimeError("unavailable"))),
            patch("bot.family.time.time", return_value=456.0),
        ):
            with self.assertRaises(RuntimeError):
                await self.families.refresh("9")
        self.assertEqual(self.families.last_steam_check, 123.0)
        self.assertEqual(self.families.last_family_check, {"9": 123.0})

    async def test_presence_and_membership_alerts_go_to_the_group(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        await self.db.set_publication(
            1,
            ALICE,
            "9",
            "publish_presence",
            True,
            expected_version=(await self.db.account(1))["publication_version"],
        )
        await self.db.update_family("9", notify_chat=-100, alerts={"playing": True})
        sent = self.bot.send_message
        sent.reset_mock()
        self.families.presence["9"] = {ALICE: {"online": True, "game": None}}
        with (
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))),
            patch(
                "bot.family.fetch_presence",
                AsyncMock(return_value={ALICE: {"online": True, "game": 620, "game_name": "Portal 2"}}),
            ),
        ):
            await self.families.check_presence("9")
        self.assertEqual(sent.await_args.args[:2], (-100, "🎮 Alice started playing Portal 2"))
        self.assertEqual(self.families.items["9"].snapshot["players"][ALICE]["game"], 620)

        sent.reset_mock()
        moved = {**snapshot([]), "members": [ALICE]}  # BOB left; "members" alerts are on by default
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=moved)):
            await self.families.refresh("9")
        self.assertIn("left the Steam family", sent.await_args.args[1])

    async def test_presence_check_stops_after_logout_during_an_alert(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        await self.db.set_publication(
            1,
            ALICE,
            "9",
            "publish_presence",
            True,
            expected_version=(await self.db.account(1))["publication_version"],
        )
        await self.db.update_family("9", notify_chat=-100, alerts={"online": True, "playing": True})
        self.families.presence["9"] = {m: {"online": False, "game": None} for m in (ALICE, BOB)}
        current = {m: {"online": True, "game": 620, "game_name": "Portal 2"} for m in (ALICE, BOB)}
        started, released = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            started.set()
            await released.wait()

        with (
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))),
            patch("bot.family.fetch_presence", AsyncMock(return_value=current)),
            patch.object(self.bot, "send_message", side_effect=send) as sent,
        ):
            check = asyncio.create_task(self.families.check_presence("9"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertEqual(len(await self.db.pending_alerts("9", 0)), 2)
                await self.families.leave(1)
            finally:
                released.set()
                await check
        sent.assert_awaited_once()
        self.assertNotIn("9", self.families.presence)
        self.assertNotIn("9", self.families.items)

    async def test_member_who_moved_family_is_not_used(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "refresh", ALICE)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([], family_id="other"))):
            await self.families.refresh("9")
        self.assertEqual((await self.db.family("9"))["state"]["ownership"], {})

    async def test_valid_snapshot_through_bob_revokes_only_departed_alice(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "approved")
        await self.db.update_family("9", notify_chat=-100, reader_telegram_id=2)
        current = {**snapshot([]), "members": [BOB]}
        with (
            patch(
                "bot.steam_auth.SteamAuth.get_access_token",
                AsyncMock(return_value="bob-access"),
            ),
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)),
        ):
            await self.families.refresh("9")
        alice, bob = await self.db.account(1), await self.db.account(2)
        self.assertEqual((alice["status"], alice["refresh_token"]), ("pending", None))
        self.assertEqual((bob["status"], bob["owner_telegram_id"]), ("approved", 2))
        self.assertIsNotNone(bob["refresh_token"])
        self.assertIn("left the Steam family", self.bot.send_message.await_args.args[1])
        # Logging out must not hand the family back to a departed pending account.
        self.assertIsNone(await self.families.leave(2))
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        self.assertEqual((await self.db.account(1))["status"], "pending")

    async def test_confirmed_departure_clears_owner_without_promoting_blocked_member(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "blocked")
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=NoFamily("confirmed departure"))):
            await self.families.refresh("9")
        self.assertEqual((await self.db.account(1))["status"], "pending")
        self.assertIsNone((await self.db.account(1))["refresh_token"])
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        self.assertEqual((await self.db.account(2))["status"], "blocked")
        current = {**snapshot([]), "members": [BOB]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)):
            await self.families.register(2, "bob-again", BOB)
        self.assertEqual((await self.db.account(2))["status"], "blocked")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_incomplete_snapshot_or_outage_keeps_approval_and_owner(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        for failure in (RuntimeError("incomplete response"), OSError("network down")):
            self.families.read_backoff.clear()
            with (
                self.subTest(failure=failure),
                patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=failure)),
            ):
                with self.assertRaises(type(failure)):
                    await self.families.refresh("9")
            account = await self.db.account(1)
            self.assertEqual((account["status"], account["owner_telegram_id"]), ("approved", 1))
            self.assertIsNotNone(account["refresh_token"])

    async def test_snapshot_without_fetching_member_does_not_revoke_others(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value={**snapshot([]), "members": []})):
            await self.families.refresh("9")
        account = await self.db.account(1)
        self.assertEqual((account["status"], account["owner_telegram_id"]), ("approved", 1))
        self.assertIsNotNone(account["refresh_token"])

    async def test_relogin_to_another_family_resets_approval_and_hands_over_old_family(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "approved")
        await self.db.ensure_family("other", "Other")
        await self.db.save_account(3, "76561198000000003", "other", "carol")
        await self.families.make_owner("other", 3)
        target = {**snapshot([], family_id="other"), "members": [ALICE, "76561198000000003"]}
        old = {**snapshot([]), "members": [BOB]}

        async def fetch(http, token):
            return old if token == "bob-access" else target

        async def token(auth):
            return "bob-access" if auth.steam_id == BOB else "new-access"

        with (
            patch("bot.family.fetch_family_snapshot", side_effect=fetch),
            patch("bot.steam_auth.SteamAuth.get_access_token", token),
        ):
            await self.families.register(1, "new-login", ALICE)
        account = await self.db.account(1)
        self.assertEqual(
            (account["family_id"], account["status"], account["owner_telegram_id"]), ("other", "pending", 3)
        )
        self.assertEqual(self.db.refresh_token(account), "new-login")
        self.assertEqual((await self.db.account(2))["owner_telegram_id"], 2)
        self.assertEqual((await self.db.account(2))["status"], "approved")
        self.assertIsNotNone(await self.db.family("9"))
        self.assertIn("9", self.families.items)
        self.assertIn("9", self.families.presence)

    async def test_migrating_last_account_removes_abandoned_family_data(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "old-login", ALICE)
        await self.db.update_family("9", notify_chat=-100)
        target = snapshot([], family_id="other")
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=target)):
            await self.families.register(1, "new-login", ALICE)
        self.assertIsNone(await self.db.family("9"))
        self.assertNotIn("9", self.families.items)
        self.assertNotIn("9", self.families.presence)
        self.assertNotIn("9", self.families.last_family_check)
        account = await self.db.account(1)
        self.assertEqual((account["family_id"], account["status"]), ("other", "approved"))
        self.assertEqual(self.db.refresh_token(account), "new-login")
        self.assertIsNotNone(await self.db.family("other"))
        self.assertEqual(self.families.items["other"].snapshot, target)
        self.assertIn(1, self.families.auths)

    async def test_changed_steam_identity_cannot_keep_ownership(self):
        current = {**snapshot([]), "members": [ALICE, BOB, "76561198000000003"]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
            await self.db.set_status(2, "approved")
            await self.families.register(1, "carol-login", "76561198000000003")
        self.assertEqual((await self.db.account(1))["status"], "pending")
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 2)

    async def test_confirmed_departure_cannot_inherit_from_cached_membership(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.families.make_owner("9", 2)
        with patch(
            "bot.family.fetch_family_snapshot",
            AsyncMock(side_effect=[snapshot([], family_id="other"), RuntimeError("temporary failure")]),
        ):
            await self.families.refresh("9")
        # A renewal already in flight may finish after the departure was confirmed.
        await self.db.set_refresh_token(1, "late-renewal")
        self.assertIsNone(await self.families.leave(2))
        self.assertEqual((await self.db.account(1))["status"], "pending")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])

    async def test_old_poll_cannot_revoke_a_newly_migrated_account(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
        started, migrated = asyncio.Event(), asyncio.Event()

        async def fetch(http, token):
            started.set()
            await migrated.wait()
            return snapshot([], family_id="other")

        with patch("bot.family.fetch_family_snapshot", fetch):
            poll = asyncio.create_task(self.families._fetch("9"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.db.ensure_family("other", "Other")
                await self.db.save_account(1, ALICE, "other", "new-login")
                await self.db.set_status(1, "approved")
            finally:
                migrated.set()
                await poll
        account = await self.db.account(1)
        self.assertEqual((account["family_id"], account["status"]), ("other", "approved"))
        self.assertEqual(self.db.refresh_token(account), "new-login")

    async def test_snapshot_from_bob_revokes_only_departed_alice(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "approved")
        await self.db.update_family("9", reader_telegram_id=2)
        current = {**snapshot([]), "members": [BOB]}
        with (
            patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(return_value="bob")),
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
            with self.assertRaises(RuntimeError):
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
        self.families.stats_cache[ALICE] = (0, {"level": 10})
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=NoFamily("left every family"))):
            with self.assertRaises(NoFamily):
                await self.families.register(1, "replacement", ALICE)
        self.assertEqual(role(await self.db.account(1)), "pending")
        self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
        self.assertNotIn(1, self.families.auths)
        self.assertNotIn(ALICE, self.families.stats_cache)
        await self.families.leave(1)
        self.assertIsNone(await self.db.family("9"))
        self.assertNotIn("9", self.families.items)

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
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=NoFamily("Alice left every family"))):
            await self.families.refresh("9")
        current = {**snapshot([]), "members": [BOB]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=current)):
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

    async def test_pending_only_old_family_waits_for_a_fresh_member_login(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        carol = "76561198000000003"
        target = {**snapshot([], family_id="10"), "members": [ALICE, carol]}
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=target)):
            await self.families.register(3, "carol", carol)
        attempted = []

        async def token(auth):
            attempted.append(auth.steam_id)
            return auth.steam_id

        async def fetch(http, access):
            return {**snapshot([]), "members": [BOB]} if access == BOB else target

        with (
            patch("bot.steam_auth.SteamAuth.get_access_token", token),
            patch("bot.family.fetch_family_snapshot", side_effect=fetch),
        ):
            await self.families.register(1, "alice-new", ALICE)
            self.assertIsNone((await self.db.family("9"))["owner_telegram_id"])
            self.assertEqual(role(await self.db.account(2)), "pending")
            self.assertNotIn(BOB, attempted)
            await self.families.register(2, "bob-fresh", BOB)
        self.assertEqual(role(await self.db.account(2)), "owner")
        self.assertEqual((await self.db.family("9"))["reader_telegram_id"], 2)

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
            patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=NoFamily("Bob left every family"))),
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
        self.assertIsNone(await self.db.family("9"))

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
class ReaderSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        await self.db.ensure_family("9", "Fam")
        await self.db.save_account(2, BOB, "9", "bob-refresh", "US")
        await self.db.save_account(1, ALICE, "9", "alice-refresh", "US")
        await self.db.set_status(1, "approved")
        await self.db.set_status(2, "approved")
        await self.db.update_family("9", owner_telegram_id=1, notify_chat=-100, alerts={"playing": True})
        self.families = Families(self.db, http=None, bot=AsyncMock())
        self.families.get("9").snapshot = snapshot([])
        self.tokens = []

        async def access(auth):
            self.tokens.append(auth.steam_id)
            return "access-" + auth.steam_id

        self.patches = [
            patch.object(SteamAuth, "get_access_token", access),
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))),
            patch("bot.family.update_store_cache", AsyncMock(return_value=False)),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        await self.db.pool.close()

    async def test_reader_prefers_owner_and_survives_restart_and_owner_change(self):
        await self.families.refresh("9")
        self.assertEqual((await self.db.family("9"))["reader_telegram_id"], 1)
        await self.db.update_family("9", owner_telegram_id=2)
        restarted = Families(self.db, http=None, bot=AsyncMock())
        await restarted.any_token("9")
        self.assertEqual(self.tokens, [ALICE, ALICE])

    async def test_reader_uses_an_approved_member_when_owner_is_blocked(self):
        await self.db.set_status(1, "blocked")
        await self.families.any_token("9")
        self.assertEqual((await self.db.family("9"))["reader_telegram_id"], 2)
        self.assertEqual(self.tokens, [BOB])

    async def test_rejected_reader_stops_without_trying_other_accounts(self):
        async def reject(auth):
            self.tokens.append(auth.steam_id)
            if auth.steam_id == ALICE:
                raise TokenRejected("revoked")
            return "other-account-access"

        with patch.object(SteamAuth, "get_access_token", reject):
            await self.families.refresh("9")
            await self.families.refresh("9")
            self.assertIsNone(await self.families.any_token("9"))
            await self.families.check_presence("9")
        self.assertEqual(self.tokens, [ALICE])
        self.assertIsNone((await self.db.account(1))["refresh_token"])
        self.assertIsNotNone((await self.db.account(2))["refresh_token"])
        self.assertEqual((await self.db.family("9"))["reader_telegram_id"], 1)
        self.assertNotIn(1, self.families.auths)

    async def test_api_rejection_also_stops_the_designated_reader(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=TokenRejected("refused"))):
            await self.families.refresh("9")
        self.assertIsNone((await self.db.account(1))["refresh_token"])
        self.assertIsNone(await self.families.any_token("9"))
        self.assertEqual(self.tokens, [ALICE])

    async def test_refresh_backoff_also_pauses_presence_and_resets_after_success(self):
        with patch("bot.family.time") as clock:
            clock.monotonic.return_value = 1000.0
            with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=OSError("outage"))):
                with self.assertRaises(OSError):
                    await self.families.refresh("9")
                with patch(
                    "bot.family.fetch_presence", AsyncMock(side_effect=AssertionError("presence during backoff"))
                ):
                    await self.families.check_presence("9")
                clock.monotonic.return_value = 1059.0
                await self.families.refresh("9")
                clock.monotonic.return_value = 1060.0
                with self.assertRaises(OSError):
                    await self.families.refresh("9")
                clock.monotonic.return_value = 1179.0
                await self.families.refresh("9")
            clock.monotonic.return_value = 1180.0
            await self.families.refresh("9")
            with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=OSError("new outage"))):
                with self.assertRaises(OSError):
                    await self.families.refresh("9")
                clock.monotonic.return_value = 1239.0
                await self.families.refresh("9")
                clock.monotonic.return_value = 1240.0
                with self.assertRaises(OSError):
                    await self.families.refresh("9")
        self.assertEqual(self.tokens, [ALICE] * 5)

    async def test_presence_failure_backs_off_family_refresh(self):
        with patch("bot.family.time") as clock:
            clock.monotonic.return_value = 1000.0
            with patch("bot.family.fetch_presence", AsyncMock(side_effect=OSError("outage"))):
                with self.assertRaises(OSError):
                    await self.families.check_presence("9")
            with patch(
                "bot.family.fetch_family_snapshot", AsyncMock(side_effect=AssertionError("refresh during backoff"))
            ):
                await self.families.refresh("9")
        self.assertEqual(self.tokens, [ALICE])

    async def test_repeated_failures_back_off_for_at_most_one_hour(self):
        retry_at = 1000.0
        with (
            patch("bot.family.time") as clock,
            patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=OSError("outage"))),
            patch("bot.family.fetch_presence", AsyncMock(side_effect=AssertionError("presence during backoff"))),
        ):
            for delay in [60, 120, 240, 480, 960, 1920, 3600, 3600]:
                clock.monotonic.return_value = retry_at
                with self.assertRaises(OSError):
                    await self.families.refresh("9")
                clock.monotonic.return_value = retry_at + delay - 1
                await self.families.refresh("9")
                await self.families.check_presence("9")
                retry_at += delay
        self.assertEqual(self.tokens, [ALICE] * 8)

    async def test_presence_api_rejection_stops_all_scheduled_reads(self):
        with patch("bot.family.fetch_presence", AsyncMock(side_effect=TokenRejected("refused"))):
            await self.families.check_presence("9")
            await self.families.check_presence("9")
        await self.families.refresh("9")
        self.assertIsNone((await self.db.account(1))["refresh_token"])
        self.assertIsNotNone((await self.db.account(2))["refresh_token"])
        self.assertEqual(self.tokens, [ALICE])

    async def test_stats_api_rejection_clears_only_the_reader_and_stops_retrying(self):
        with patch("bot.family.member_stats", AsyncMock(side_effect=TokenRejected("refused"))):
            self.assertEqual(await self.families.member_stats("9", BOB), {})
            self.assertEqual(await self.families.member_stats("9", ALICE), {})
        self.assertIsNone((await self.db.account(1))["refresh_token"])
        self.assertIsNotNone((await self.db.account(2))["refresh_token"])
        self.assertNotIn(1, self.families.auths)
        self.assertEqual(self.tokens, [ALICE])

    async def test_scheduled_country_lookup_uses_only_the_reader_session(self):
        await self.db.pool.execute("UPDATE accounts SET country = NULL")
        countries = []

        async def country(http, token, steam_id):
            countries.append((token, steam_id))
            return "KZ"

        with patch("bot.family.fetch_user_country", side_effect=country):
            await self.families.refresh("9")
        self.assertEqual(countries, [("access-" + ALICE, ALICE)])
        self.assertEqual((await self.db.account(1))["country"], "KZ")
        self.assertIsNone((await self.db.account(2))["country"])
        self.assertEqual(self.tokens, [ALICE, ALICE])

    async def test_country_api_rejection_stops_the_reader_without_fallback(self):
        await self.db.pool.execute("UPDATE accounts SET country = NULL")
        with patch("bot.family.fetch_user_country", AsyncMock(side_effect=TokenRejected("refused"))):
            await self.families.refresh("9")
            await self.families.refresh("9")
        self.assertIsNone((await self.db.account(1))["refresh_token"])
        self.assertIsNotNone((await self.db.account(2))["refresh_token"])
        self.assertNotIn(1, self.families.auths)
        self.assertEqual(self.tokens, [ALICE, ALICE])

    async def test_logout_revokes_its_session_before_account_or_owner_changes(self):
        revoked = []

        async def revoke(auth):
            revoked.append(auth.refresh_token)
            self.assertEqual(auth.refresh_token, "alice-refresh")
            self.assertIsNotNone(await self.db.account(1))
            self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 1)

        with patch.object(SteamAuth, "revoke", revoke, create=True):
            self.assertEqual(await self.families.leave(1), 2)
        self.assertEqual(revoked, ["alice-refresh"])
        self.assertIsNone(await self.db.account(1))
        self.assertIsNotNone(await self.db.account(2))
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 2)
        self.assertNotIn(1, self.families.account_locks)

    async def test_failed_logout_keeps_credentials_and_family_roles(self):
        await self.db.set_language(1, "ru")
        with patch.object(SteamAuth, "revoke", AsyncMock(side_effect=RuntimeError("Steam unavailable")), create=True):
            with self.assertRaises(SessionRevocationFailed):
                await self.families.leave(1)
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "alice-refresh")
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 1)
        self.assertEqual(await self.db.language(1), "ru")

    async def test_queued_presence_waits_for_backoff_without_another_steam_read(self):
        account = await self.db.account(1)
        await self.db.set_publication(
            1, ALICE, "9", "publish_presence", True, expected_version=account["publication_version"]
        )
        account = await self.db.account(1)
        await self.db.enqueue_alerts(
            "9",
            [
                {
                    "text": "presence",
                    "coalesce_key": "playing:" + ALICE,
                    "publication": {
                        "event": {"kind": "playing", "steamid": ALICE},
                        "telegram_id": 1,
                        "publication_version": account["publication_version"],
                    },
                }
            ],
        )
        with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=OSError("outage"))):
            with self.assertRaises(OSError):
                await self.families.refresh("9")
        with patch.object(self.families, "_fetch", AsyncMock(side_effect=AssertionError("Steam during backoff"))):
            await self.families.deliver_alerts("9")
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts"), 1)
        self.families.bot.send_message.assert_not_awaited()

    async def test_late_rejection_cannot_clear_a_replacement_login_or_cache(self):
        stale = await self.db.account(1)
        await self.db.save_account(1, ALICE, "9", "replacement", "US")
        await self.families.owner_token(await self.db.account(1))
        current_auth = self.families.auths[1]
        await self.families._reject_token(stale)
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "replacement")
        self.assertIs(self.families.auths[1], current_auth)

    async def test_late_rejection_cannot_clear_a_login_recreated_after_logout(self):
        stale = await self.db.account(1)
        await self.db.delete_user(1)
        await self.db.save_account(1, ALICE, "9", "replacement", "US")
        await self.families.owner_token(await self.db.account(1))
        current_auth = self.families.auths[1]
        await self.families._reject_token(stale)
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "replacement")
        self.assertIs(self.families.auths[1], current_auth)

    async def test_old_renewal_callback_cannot_overwrite_a_replacement_login(self):
        stale = await self.db.account(1)
        old_auth = self.families.auth(1, "alice-refresh", ALICE, generation=stale["token_generation"])
        await self.db.save_account(1, ALICE, "9", "replacement", "US")
        await self.families.owner_token(await self.db.account(1))
        current_auth = self.families.auths[1]
        with self.assertRaises(TokenRejected):
            await old_auth.on_renew("obsolete-rotation", 1000.0)
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "replacement")
        self.assertIs(self.families.auths[1], current_auth)
        self.assertNotEqual((await self.db.account(1))["token_generation"], stale["token_generation"])

    async def test_stale_action_context_is_rejected_before_network(self):
        stale = await self.db.account(1)
        await self.db.save_account(1, ALICE, "9", "replacement", "US")
        with self.assertRaises(TokenRejected):
            await self.families.owner_token(stale)
        current = dict(await self.db.account(1))
        for field, value in [("steam_id", BOB), ("family_id", "other-family")]:
            with self.subTest(field=field), self.assertRaises(TokenRejected):
                await self.families.owner_token({**current, field: value})
        self.assertEqual(self.tokens, [])
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "replacement")

    async def test_stats_and_presence_late_rejection_preserve_a_new_login(self):
        for operation in ["stats", "presence"]:
            with self.subTest(operation=operation):
                self.families.stats_cache.clear()

                async def late_rejection(*args, operation=operation):
                    await self.db.save_account(1, ALICE, "9", "replacement-" + operation, "US")
                    raise TokenRejected("old request refused")

                target = "member_stats" if operation == "stats" else "fetch_presence"
                with patch("bot.family." + target, side_effect=late_rejection):
                    if operation == "stats":
                        await self.families.member_stats("9", ALICE)
                    else:
                        await self.families.check_presence("9")
                self.assertEqual(self.db.refresh_token(await self.db.account(1)), "replacement-" + operation)

    async def test_stats_transient_failure_pauses_scheduled_reads(self):
        for phase in ["token", "stats"]:
            with self.subTest(phase=phase), patch("bot.family.time") as clock:
                self.families.read_backoff.clear()
                self.families.stats_cache.clear()
                clock.monotonic.return_value = 1000.0
                target = "bot.steam_auth.SteamAuth.get_access_token" if phase == "token" else "bot.family.member_stats"
                with patch(target, AsyncMock(side_effect=OSError("outage"))):
                    with self.assertRaises(OSError):
                        await self.families.member_stats("9", ALICE)
                with patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=AssertionError("no backoff"))):
                    clock.monotonic.return_value = 1059.0
                    await self.families.refresh("9")
                self.assertIsNotNone((await self.db.account(1))["refresh_token"])

    async def test_logout_and_new_registration_are_serialized(self):
        revoking, release, new_auth_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def revoke(auth):
            self.assertEqual(auth.refresh_token, "alice-refresh")
            revoking.set()
            await release.wait()

        async def access(auth):
            if auth.refresh_token == "replacement":
                new_auth_started.set()
            return "access"

        with (
            patch.object(SteamAuth, "revoke", revoke),
            patch.object(SteamAuth, "get_access_token", access),
            patch("bot.family.fetch_user_country", AsyncMock(return_value="US")),
        ):
            leaving = asyncio.create_task(self.families.leave(1))
            await asyncio.wait_for(revoking.wait(), 1)
            registering = asyncio.create_task(self.families.register(1, "replacement", ALICE))
            try:
                await asyncio.wait_for(new_auth_started.wait(), 0.05)
                started_during_logout = True
            except TimeoutError:
                started_during_logout = False
            finally:
                release.set()
                await asyncio.gather(leaving, registering)
        self.assertFalse(started_during_logout)
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "replacement")


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
            patch("bot.steam_auth.SteamAuth.revoke", AsyncMock()),
        ]
        self.steam_call = self.patches[-2].start()
        for p in [*self.patches[:-2], self.patches[-1]]:
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

    async def test_uncommitted_registration_does_not_retain_health_family_identifiers(self):
        with patch.object(self.db, "save_account", AsyncMock(side_effect=OSError("unavailable"))):
            with self.assertRaises(OSError):
                await self.families.register(1, "original", ALICE)
        self.assertEqual(self.families.health.families, {})
        self.assertEqual(self.families.health.first_seen, {})
        self.assertIsNone(self.families.last_steam_check)

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

    async def test_committed_rotation_is_retried_after_a_lost_database_reply(self):
        await self.families.register(1, "original", ALICE)
        account = await self.db.account(1)
        access = "x.eyJleHAiOjMwMDAwMH0.y"
        self.steam_call.return_value = {1: access.encode(), 2: b"committed"}
        self.steam_call.reset_mock()
        persist = self.db.set_refresh_token
        calls = 0

        async def commit_then_fail(*args, **kwargs):
            nonlocal calls
            saved = await persist(*args, **kwargs)
            calls += 1
            if calls == 1:
                raise OSError("reply lost after commit")
            return saved

        with (
            patch("bot.steam_auth.time.time", return_value=200_000.0),
            patch.object(self.db, "set_refresh_token", side_effect=commit_then_fail),
        ):
            with self.assertRaises(OSError):
                await self.families.owner_token(account)
            cached = self.families.auths[1]
            self.assertEqual(self.db.refresh_token(await self.db.account(1)), "committed")
            self.assertIsNotNone(cached._pending_renewal)
            self.assertEqual(await self.families.owner_token(await self.db.account(1)), access)
        self.assertIs(self.families.auths[1], cached)
        self.assertEqual((cached.refresh_token, cached.last_renewal), ("committed", 200_000.0))
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "committed")
        self.assertEqual(calls, 2)
        self.steam_call.assert_awaited_once()

    async def test_stale_account_row_does_not_restore_a_rotated_session(self):
        await self.families.register(1, "original", ALICE)
        stale_account = await self.db.account(1)
        self.steam_call.return_value = {1: b"x.eyJleHAiOjMwMDAwMH0.y", 2: b"latest"}
        self.steam_call.reset_mock()
        with patch("bot.steam_auth.time.time", return_value=200_000.0):
            await self.families.owner_token(stale_account)
        cached = self.families.auths[1]
        with patch("bot.steam_auth.time.time", return_value=200_001.0):
            await self.families.owner_token(stale_account)
        self.assertIs(self.families.auths[1], cached)
        self.assertEqual(self.families.auths[1].refresh_token, "latest")
        self.steam_call.assert_awaited_once()

    async def test_logout_rejects_an_inflight_token_renewal(self):
        await self.families.register(1, "original", ALICE)
        account = await self.db.account(1)
        started, released = asyncio.Event(), asyncio.Event()

        async def renew(*args):
            started.set()
            await released.wait()
            return {1: b"late-access", 2: b"late-refresh"}

        self.steam_call.side_effect = renew
        with patch("bot.steam_auth.time.time", return_value=200_000.0):
            renewal = asyncio.create_task(self.families.owner_token(account))
            try:
                await asyncio.wait_for(started.wait(), 2)
                logout = asyncio.create_task(self.families.leave(1))
                await asyncio.sleep(0)
                self.assertFalse(logout.done())
            finally:
                released.set()
            self.assertEqual(await renewal, "late-access")
            await asyncio.wait_for(logout, 2)
            with self.assertRaises(TokenRejected):
                await self.families.owner_token(account)
        self.assertIsNone(await self.db.account(1))
        self.assertNotIn(1, self.families.auths)

    async def test_old_token_write_cannot_overwrite_credentials_after_logout_and_relogin(self):
        await self.families.register(1, "original", ALICE)
        account = await self.db.account(1)
        started, released = asyncio.Event(), asyncio.Event()
        set_token = self.db.set_refresh_token

        async def delayed_write(*args, **kwargs):
            started.set()
            await released.wait()
            return await set_token(*args, **kwargs)

        self.steam_call.return_value = {1: b"late-access", 2: b"late-refresh"}
        with (
            patch("bot.steam_auth.time.time", return_value=200_000.0),
            patch.object(self.db, "set_refresh_token", side_effect=delayed_write),
        ):
            renewal = asyncio.create_task(self.families.owner_token(account))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.db.delete_user(1)
                await self.db.ensure_family("9", "Fam")
                await self.db.save_account(1, BOB, "9", "new-login")
            finally:
                released.set()
            with self.assertRaises(TokenRejected):
                await renewal
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "new-login")

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

    async def test_failed_registration_reports_latest_cookie_for_cleanup(self):
        await self.families.register(1, "original", ALICE)
        remembered = []

        async def remember(token):
            remembered.append(token)

        with (
            patch("bot.steam_auth.time.time", return_value=200_000.0),
            patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=OSError("unavailable"))),
        ):
            self.steam_call.return_value = {1: b"access", 2: b"latest-cookie"}
            with self.assertRaises(OSError):
                await self.families.register(1, "new-login", ALICE, on_refresh=remember)
        self.assertEqual(remembered, ["latest-cookie"])
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "rotated")

    async def test_registration_acknowledges_commit_before_cancellable_refresh(self):
        acknowledged = []

        async def refresh(family_id):
            self.assertEqual(acknowledged, [True])
            self.assertEqual(self.db.refresh_token(await self.db.account(1)), "rotated")
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

        with patch.object(self.families, "refresh", side_effect=refresh):
            task = asyncio.create_task(
                self.families.register(1, "original", ALICE, on_registered=lambda: acknowledged.append(True))
            )
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(acknowledged, [True])
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "rotated")


if __name__ == "__main__":
    unittest.main()
