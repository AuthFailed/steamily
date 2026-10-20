import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import LinkPreviewOptions

from bot.family import Families
from tests.pg import fresh_db, needs_pg
from tests.test_family import ALICE, BOB, snapshot


@needs_pg
class PublicationDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        await self.db.ensure_family("9", "Fam")
        for telegram_id, steam_id in ((1, ALICE), (2, BOB)):
            await self.db.save_account(telegram_id, steam_id, "9", "refresh", "KZ")
            await self.db.set_status(telegram_id, "approved")
        self.bot = AsyncMock()
        self.families = Families(self.db, None, self.bot)
        await self.families.make_owner("9", 1)
        await self.db.update_family("9", notify_chat=-100, alerts={"online": True})
        self.snapshot = snapshot([])
        self.families.get("9").snapshot = self.snapshot
        self.families.presence["9"] = {ALICE: {"online": False}, BOB: {"online": False}}
        self.clock = patch("bot.family.time.time", return_value=1000.0)
        self.now = self.clock.start()
        self.token = patch("bot.steam_auth.SteamAuth.get_access_token", AsyncMock(return_value="access"))
        self.token.start()
        self.steam = patch("bot.family.fetch_family_snapshot", AsyncMock(side_effect=lambda *_: self.snapshot))
        self.fetch = self.steam.start()
        await self.publication(True)

    async def asyncTearDown(self):
        self.steam.stop()
        self.token.stop()
        self.clock.stop()
        await self.db.pool.close()

    async def publication(self, on):
        account = await self.db.account(1)
        changed = await self.db.set_publication(
            1, ALICE, "9", "publish_presence", on, expected_version=account["publication_version"]
        )
        self.assertTrue(changed)

    async def queued(self):
        return await self.db.pool.fetch("SELECT * FROM pending_alerts ORDER BY id")

    async def fail_first_delivery(self):
        self.bot.send_message.side_effect = TelegramNetworkError(SendMessage(chat_id=-100, text="alert"), "offline")
        with patch("bot.family.fetch_presence", AsyncMock(return_value={ALICE: {"online": True}})):
            await self.families.check_presence("9")
        self.assertEqual(self.bot.send_message.await_count, 1)
        pending = await self.queued()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["publication"]["event"]["steamid"], ALICE)
        self.assertEqual(pending[0]["publication"]["telegram_id"], 1)
        self.assertTrue(pending[0]["publication"]["publication_version"])
        return pending[0]

    async def retry_after_restart(self):
        self.now.return_value = 1061.0
        self.bot.send_message.side_effect = None
        restarted = Families(self.db, None, self.bot)
        await restarted.deliver_alerts("9")

    async def assert_suppressed(self):
        await self.retry_after_restart()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(await self.queued(), [])

    async def test_valid_retry_survives_restart_and_renders_current_name(self):
        await self.fail_first_delivery()
        self.snapshot["names"][ALICE] = "Alice renamed"
        await self.retry_after_restart()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIn("Alice renamed", self.bot.send_message.await_args.args[1])
        self.assertEqual(await self.queued(), [])

    async def test_opt_out_suppresses_durable_retry(self):
        await self.fail_first_delivery()
        await self.publication(False)
        await self.assert_suppressed()

    async def test_deletion_suppresses_durable_retry(self):
        await self.fail_first_delivery()
        await self.db.delete_user(1)
        await self.assert_suppressed()

    async def test_departure_suppresses_durable_retry(self):
        await self.fail_first_delivery()
        self.snapshot["members"] = [BOB]
        await self.assert_suppressed()

    async def test_reconsent_does_not_restore_old_pending_disclosure(self):
        pending = await self.fail_first_delivery()
        await self.publication(False)
        await self.publication(True)
        account = await self.db.account(1)
        self.assertNotEqual(pending["publication"]["publication_version"], account["publication_version"])
        await self.assert_suppressed()

    async def test_logout_and_relogin_do_not_restore_old_pending_disclosure(self):
        await self.fail_first_delivery()
        await self.db.revoke_account(1, ALICE, "9")
        await self.db.save_account(1, ALICE, "9", "new-refresh", "KZ")
        await self.db.set_status(1, "approved")
        await self.publication(True)
        await self.assert_suppressed()

    async def test_deleted_and_recreated_account_cannot_publish_old_event(self):
        await self.fail_first_delivery()
        await self.db.delete_user(1)
        await self.db.save_account(1, ALICE, "9", "new-refresh", "KZ")
        await self.db.set_status(1, "approved")
        await self.publication(True)
        await self.assert_suppressed()

    async def test_disabled_notification_suppresses_durable_retry(self):
        await self.fail_first_delivery()
        await self.db.update_family("9", alerts={"online": False})
        await self.assert_suppressed()

    async def test_legacy_presence_is_discarded_without_suppressing_other_alerts(self):
        await self.db.enqueue_alerts(
            "9",
            [{"text": "legacy presence", "coalesce_key": f"online:{ALICE}"}, {"text": "ordinary game alert"}],
        )
        await self.families.deliver_alerts("9")
        self.bot.send_message.assert_awaited_once_with(
            -100, "ordinary game alert", parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        self.fetch.assert_not_awaited()
        self.assertEqual(await self.queued(), [])
