import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from bot.family import Families
from tests.pg import fresh_db, needs_pg
from tests.test_family import ALICE, BOB, snapshot


@needs_pg
class AlertDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        self.bot = AsyncMock()
        self.families = Families(self.db, None, self.bot)
        self.clock = patch("bot.family.time.time", return_value=1000.0)
        self.now = self.clock.start()
        self.patches = [
            patch("bot.family.update_store_cache", AsyncMock(return_value=False)),
            patch("bot.family.fetch_user_country", AsyncMock(return_value="KZ")),
            patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(return_value="access")),
            patch("bot.family.fetch_family_snapshot", AsyncMock(return_value=snapshot([]))),
        ]
        for p in self.patches:
            p.start()
        self.fetch = self.patches[-1].new
        await self.families.register(1, "refresh", ALICE)
        await self.db.update_family("9", notify_chat=-100)
        self.method = SendMessage(chat_id=-100, text="alert")

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.clock.stop()
        await self.db.pool.close()

    def acquisition(self):
        self.fetch.return_value = snapshot([{"appid": 620, "name": "Portal 2", "owners": [ALICE]}])

    async def queued(self):
        return await self.db.pool.fetch("SELECT * FROM pending_alerts ORDER BY id")

    async def test_retryable_failure_keeps_observation_and_retries_once(self):
        self.acquisition()
        self.bot.send_message.side_effect = [TelegramNetworkError(self.method, "offline"), None]
        await self.families.refresh("9")
        self.now.return_value = 1061.0
        await self.families.refresh("9")
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual((await self.db.family("9"))["state"]["ownership"], {"620": [ALICE]})
        self.assertEqual(await self.queued(), [])

    async def test_restart_retries_pending_membership_even_without_steam_login(self):
        await self.db.save_account(2, BOB, "9", "other-refresh")
        self.fetch.return_value = {**snapshot([]), "members": [ALICE]}
        self.bot.send_message.side_effect = [TelegramNetworkError(self.method, "offline"), None]
        await self.families.refresh("9")
        pending = await self.queued()
        self.assertEqual(len(pending), 1)
        await self.db.delete_account(1)
        restarted = Families(self.db, None, self.bot)
        self.now.return_value = 1061.0
        await restarted.deliver_alerts("9")
        self.assertIn("left the Steam family", self.bot.send_message.await_args.args[1])
        self.assertEqual(await self.queued(), [])

    async def test_retry_after_blocks_all_events_across_refresh_and_restart(self):
        self.acquisition()
        self.bot.send_message.side_effect = [TelegramRetryAfter(self.method, "slow down", retry_after=120), None]
        await self.families.refresh("9")
        restarted = Families(self.db, None, self.bot)
        self.now.return_value = 1119.0
        await restarted.deliver_alerts("9")
        await self.families.refresh("9")
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.now.return_value = 1120.0
        await restarted.deliver_alerts("9")
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(await self.queued(), [])

    async def test_permanent_chat_failures_unlink_and_discard_pending(self):
        for error in (
            TelegramForbiddenError(self.method, "bot was kicked"),
            TelegramBadRequest(self.method, "Bad Request: chat not found"),
        ):
            with self.subTest(error=type(error).__name__):
                await self.db.update_family("9", notify_chat=-100)
                await self.db.enqueue_alerts("9", [{"text": "first"}, {"text": "second"}])
                self.bot.send_message.side_effect = error
                await self.families.deliver_alerts("9")
                self.assertIsNone((await self.db.family("9"))["notify_chat"])
                self.assertEqual(await self.queued(), [])

    async def test_permanent_message_failure_does_not_poison_next_event(self):
        await self.db.enqueue_alerts("9", [{"text": "bad"}, {"text": "good"}])
        self.bot.send_message.side_effect = [TelegramBadRequest(self.method, "message is too long"), None]
        await self.families.deliver_alerts("9")
        self.assertEqual(self.bot.send_message.await_args.args[:2], (-100, "good"))
        self.assertEqual((await self.db.family("9"))["notify_chat"], -100)
        self.assertEqual(await self.queued(), [])

    async def test_migration_response_preserves_event_and_uses_delayed_retry(self):
        self.acquisition()
        self.bot.send_message.side_effect = [
            TelegramMigrateToChat(self.method, "upgraded", migrate_to_chat_id=-200),
            None,
        ]
        await self.families.refresh("9")
        pending = await self.queued()
        self.assertEqual((await self.db.family("9"))["notify_chat"], -200)
        self.assertEqual(pending[0]["chat_id"], -200)
        await self.families.deliver_alerts("9")
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.now.return_value = 1061.0
        await self.families.deliver_alerts("9")
        self.assertEqual(self.bot.send_message.await_args.args[:2], (-200, pending[0]["text"]))
        self.assertEqual(await self.queued(), [])

    async def test_delayed_migration_or_forbidden_response_cannot_overwrite_relink(self):
        for error in (
            TelegramMigrateToChat(self.method, "upgraded", migrate_to_chat_id=-200),
            TelegramForbiddenError(self.method, "kicked"),
        ):
            with self.subTest(error=type(error).__name__):
                await self.db.update_family("9", notify_chat=-100)
                await self.db.enqueue_alerts("9", [{"text": "old destination"}])

                async def relink_then_fail(*args, send_error=error, **kwargs):
                    await self.db.update_family("9", notify_chat=-300)
                    raise send_error

                self.bot.send_message.side_effect = relink_then_fail
                await self.families.deliver_alerts("9")
                self.assertEqual((await self.db.family("9"))["notify_chat"], -300)
                self.assertEqual(await self.queued(), [])

    async def test_presence_coalesces_and_expires_without_stalling_observation(self):
        await self.db.update_family("9", alerts={"online": True, "playing": True})
        self.families.presence["9"] = {ALICE: {"online": False, "game": None}}
        self.bot.send_message.side_effect = TelegramNetworkError(self.method, "offline")
        with patch("bot.family.fetch_presence", AsyncMock()) as fetch:
            fetch.return_value = {ALICE: {"online": True, "game": 620, "game_name": "Portal 2"}}
            await self.families.check_presence("9")
            self.now.return_value = 1001.0
            fetch.return_value = {ALICE: {"online": False, "game": None}}
            await self.families.check_presence("9")
        pending = await self.queued()
        self.assertEqual(len(pending), 2)
        self.assertTrue(any("offline" in p["text"] for p in pending))
        self.assertTrue(any("stopped playing" in p["text"] for p in pending))
        self.assertEqual(self.families.presence["9"][ALICE], {"online": False, "game": None})
        self.now.return_value = 1302.0
        self.bot.send_message.reset_mock()
        await self.families.deliver_alerts("9")
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.queued(), [])

    async def test_overlapping_refreshes_and_deliveries_produce_one_acquisition(self):
        self.acquisition()
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()

        self.bot.send_message.side_effect = send
        first = asyncio.create_task(self.families.refresh("9"))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(self.families.refresh("9"))
        delivery = asyncio.create_task(self.families.deliver_alerts("9"))
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second, delivery), 5)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(await self.queued(), [])

    async def test_failed_observation_does_not_advance_membership_baseline(self):
        self.fetch.return_value = {**snapshot([]), "members": [ALICE]}
        with patch.object(self.db, "observe_family", AsyncMock(side_effect=RuntimeError("database down"))):
            with self.assertRaisesRegex(RuntimeError, "database down"):
                await self.families.refresh("9")
        self.assertEqual(self.families.get("9").members, [ALICE, BOB])
        await self.families.refresh("9")
        self.assertIn("left the Steam family", self.bot.send_message.await_args.args[1])

    async def test_cancelled_delivery_keeps_event_and_releases_family_lock(self):
        await self.db.enqueue_alerts("9", [{"text": "durable"}])
        entered = asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        self.bot.send_message.side_effect = send
        sending = asyncio.create_task(self.families.deliver_alerts("9"))
        await asyncio.wait_for(entered.wait(), 5)
        sending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await sending
        self.assertEqual(len(await self.queued()), 1)
        self.bot.send_message.side_effect = None
        await asyncio.wait_for(self.families.deliver_alerts("9"), 5)
        self.assertEqual(await self.queued(), [])

    async def test_delivery_loop_drains_after_restart_without_fetching_steam(self):
        await self.db.save_account(2, BOB, "9", "other-refresh")
        await self.db.enqueue_alerts("9", [{"text": "saved before restart"}])
        await self.db.delete_account(1)
        restarted = Families(self.db, None, self.bot)
        with patch("bot.family.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await restarted.delivery_loop()
        self.assertEqual(self.bot.send_message.await_args.args[:2], (-100, "saved before restart"))
        self.assertEqual(await self.queued(), [])

    async def test_delivery_loop_recovers_from_temporary_database_scan_failure(self):
        await self.db.enqueue_alerts("9", [{"text": "durable"}])
        scan = self.db.alert_families
        calls = 0

        async def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("database unavailable")
            return await scan()

        with (
            patch.object(self.db, "alert_families", fail_once),
            patch("bot.family.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.families.delivery_loop()
        self.assertEqual(self.bot.send_message.await_args.args[:2], (-100, "durable"))
        self.assertEqual(await self.queued(), [])

    async def test_stale_send_failure_cannot_change_another_familys_new_link(self):
        await self.db.ensure_family("other", "Other")
        for error in (
            TelegramMigrateToChat(self.method, "upgraded", migrate_to_chat_id=-200),
            TelegramForbiddenError(self.method, "kicked"),
        ):
            with self.subTest(error=type(error).__name__):
                await self.db.update_family("other", notify_chat=None)
                await self.db.update_family("9", notify_chat=-100)
                await self.db.enqueue_alerts("9", [{"text": "old link"}])

                async def reassign_then_fail(*args, send_error=error, **kwargs):
                    await self.db.update_family("9", notify_chat=-300)
                    await self.db.update_family("other", notify_chat=-100)
                    await self.db.enqueue_alerts("other", [{"text": "other family"}])
                    raise send_error

                self.bot.send_message.side_effect = reassign_then_fail
                await self.families.deliver_alerts("9")
                self.assertEqual((await self.db.family("9"))["notify_chat"], -300)
                self.assertEqual((await self.db.family("other"))["notify_chat"], -100)
                pending = await self.queued()
                self.assertEqual([(p["family_id"], p["text"]) for p in pending], [("other", "other family")])

    async def test_retry_after_survives_presence_replacement_and_service_migration(self):
        await self.db.enqueue_alerts("9", [{"text": "online", "coalesce_key": f"online:{ALICE}"}])

        async def replace_and_migrate(*args, **kwargs):
            await self.db.enqueue_alerts("9", [{"text": "offline", "coalesce_key": f"online:{ALICE}"}])
            await self.db.migrate_chat(-100, -200)
            raise TelegramRetryAfter(self.method, "slow down", retry_after=120)

        self.bot.send_message.side_effect = replace_and_migrate
        await self.families.deliver_alerts("9")
        self.bot.send_message.side_effect = None
        self.now.return_value = 1119.0
        await self.families.deliver_alerts("9")
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.now.return_value = 1120.0
        await self.families.deliver_alerts("9")
        self.assertEqual(self.bot.send_message.await_args.args[:2], (-200, "offline"))
        self.assertEqual(await self.queued(), [])

    async def test_stale_failure_preserves_group_readded_during_send(self):
        for error in (
            TelegramMigrateToChat(self.method, "upgraded", migrate_to_chat_id=-200),
            TelegramForbiddenError(self.method, "kicked"),
        ):
            with self.subTest(error=type(error).__name__):
                await self.db.update_family("9", notify_chat=None)
                await self.db.update_family("9", notify_chat=-100)
                await self.db.enqueue_alerts("9", [{"text": "old link"}])

                async def readd_then_fail(*args, send_error=error, **kwargs):
                    await self.db.unlink_chat(-100)
                    await self.db.update_family("9", notify_chat=-100)
                    await self.db.enqueue_alerts("9", [{"text": "restored link"}])
                    raise send_error

                self.bot.send_message.side_effect = readd_then_fail
                await self.families.deliver_alerts("9")
                self.assertEqual((await self.db.family("9"))["notify_chat"], -100)
                self.assertEqual([p["text"] for p in await self.queued()], ["restored link"])
                self.assertEqual((await self.db.family("9"))["alert_retry_at"], 0)
                self.bot.send_message.side_effect = None
                await self.families.deliver_alerts("9")
                self.assertEqual(await self.queued(), [])
