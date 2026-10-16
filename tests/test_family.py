import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot.family import Families
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

    async def test_valid_snapshot_through_bob_revokes_only_departed_alice(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
        await self.db.set_status(2, "approved")
        await self.db.update_family("9", notify_chat=-100)
        current = {**snapshot([]), "members": [BOB]}
        with (
            patch(
                "bot.steam_auth.SteamAuth.get_access_token",
                AsyncMock(side_effect=[TokenRejected("revoked"), "bob-access"]),
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
            with (
                self.subTest(failure=failure),
                patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=failure)),
            ):
                try:
                    await self.families.refresh("9")
                except OSError:
                    pass
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

    async def test_changed_steam_identity_cannot_keep_ownership(self):
        with patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))):
            await self.families.register(1, "alice", ALICE)
            await self.families.register(2, "bob", BOB)
            await self.db.set_status(2, "approved")
            await self.families.register(1, "bob-login", BOB)
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
