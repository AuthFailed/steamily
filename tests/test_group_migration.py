"""Telegram migration service messages pass through the real dispatcher without a sender account."""

import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, Update, User
from aiogram_dialog.test_tools import BotClient, MockMessageManager
from aiogram_dialog.test_tools.memory_storage import JsonMemoryStorage

from bot.__main__ import create_dispatcher, router
from bot.family import Families
from tests.pg import fresh_db, needs_pg

FAMILY, OLD_CHAT, NEW_CHAT = "9", -500, -100500


@needs_pg
class GroupMigrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.created_dispatcher = router.parent_router is None
        cls.dp = router.parent_router or create_dispatcher(
            None, None, None, storage=JsonMemoryStorage(), message_manager=MockMessageManager()
        )
        cls.original_data = cls.dp.workflow_data.copy()

    @classmethod
    def tearDownClass(cls):
        cls.dp.workflow_data.clear()
        cls.dp.workflow_data.update(cls.original_data)
        if cls.created_dispatcher:
            for child in cls.dp.sub_routers:
                child._parent_router = None

    async def asyncSetUp(self):
        self.db = await fresh_db()
        self.families = Families(self.db, http=None, bot=AsyncMock())
        self.dp.workflow_data.update(db=self.db, families=self.families)
        self.bot = BotClient(self.dp, user_id=100).bot
        await self.db.ensure_family(FAMILY, "Fam")
        await self.db.update_family(FAMILY, notify_chat=OLD_CHAT, language="ru")

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def notice(self, direction, sender=False):
        chat_id, chat_type, field, target = (
            (OLD_CHAT, "group", "migrate_to_chat_id", NEW_CHAT)
            if direction == "to"
            else (NEW_CHAT, "supergroup", "migrate_from_chat_id", OLD_CHAT)
        )
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    date=datetime.now(),
                    chat=Chat(id=chat_id, type=chat_type, title="Group"),
                    from_user=User(id=100, is_bot=False, first_name="U") if sender else None,
                    **{field: target},
                ),
            ),
        )

    async def test_both_notices_migrate_without_loading_sender(self):
        with (
            patch.object(self.db, "account", AsyncMock(side_effect=AssertionError("must not load account"))),
            patch.object(self.db, "language", AsyncMock(side_effect=AssertionError("must not load language"))),
        ):
            for direction in ("to", "from"):
                for sender in (False, True):
                    with self.subTest(direction=direction, sender=sender):
                        await self.db.update_family(FAMILY, notify_chat=OLD_CHAT)
                        await self.notice(direction, sender)
                        family = await self.db.family(FAMILY)
                        self.assertEqual(family["notify_chat"], NEW_CHAT)
                        self.assertEqual(family["language"], "ru")
        self.families.bot.send_message.assert_not_awaited()

    async def test_repeated_notices_leave_the_migrated_link(self):
        await self.notice("to")
        await self.notice("from")
        await self.notice("to")
        self.assertEqual((await self.db.family(FAMILY))["notify_chat"], NEW_CHAT)

    async def test_delayed_notices_do_not_restore_an_unlinked_or_relinked_group(self):
        for replacement in (None, -900):
            with self.subTest(replacement=replacement):
                await self.db.update_family(FAMILY, notify_chat=replacement)
                await self.notice("to")
                await self.notice("from")
                self.assertEqual((await self.db.family(FAMILY))["notify_chat"], replacement)

    async def test_migration_does_not_take_another_familys_chat(self):
        await self.db.ensure_family("other", "Other")
        await self.db.update_family("other", notify_chat=NEW_CHAT)
        await self.notice("to")
        await self.notice("from")
        self.assertEqual((await self.db.family(FAMILY))["notify_chat"], OLD_CHAT)
        self.assertEqual((await self.db.family("other"))["notify_chat"], NEW_CHAT)

    async def test_ordinary_group_message_is_still_ignored(self):
        with patch.object(self.db, "account", AsyncMock()) as account:
            await BotClient(self.dp, user_id=100, chat_id=OLD_CHAT, chat_type="group").send("/start")
        account.assert_not_awaited()
        self.families.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.db.family(FAMILY))["notify_chat"], OLD_CHAT)
