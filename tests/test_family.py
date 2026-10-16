import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot.family import Families
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

    async def test_failed_logout_keeps_credentials_and_family_roles(self):
        await self.db.set_language(1, "ru")
        with patch.object(SteamAuth, "revoke", AsyncMock(side_effect=RuntimeError("Steam unavailable")), create=True):
            with self.assertRaises(RuntimeError):
                await self.families.leave(1)
        self.assertEqual(self.db.refresh_token(await self.db.account(1)), "alice-refresh")
        self.assertEqual((await self.db.family("9"))["owner_telegram_id"], 1)
        self.assertEqual(await self.db.language(1), "ru")

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
