import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram_dialog import StartMode

from bot.dialogs.admin import approve, block, remove
from bot.dialogs.menu import on_logout
from bot.states import Admin, Menu


class AccountDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = SimpleNamespace(
            account=AsyncMock(
                side_effect=lambda telegram_id: {
                    "telegram_id": telegram_id,
                    "steam_id": "alice" if telegram_id == 100 else "bob",
                    "family_id": "9",
                    "status": "approved",
                    "owner_telegram_id": 100,
                }
            ),
            language=AsyncMock(return_value="en"),
            set_status=AsyncMock(return_value=True),
            delete_account=AsyncMock(return_value=True),
        )
        self.family = SimpleNamespace(family_id="9", members={"alice", "bob"}, snapshot={"name": "Fam"})
        self.families = SimpleNamespace(
            db=self.db, bot=AsyncMock(), leave=AsyncMock(return_value=200), get=MagicMock(return_value=self.family)
        )
        self.background = SimpleNamespace(start=AsyncMock())
        self.manager = MagicMock(
            middleware_data={
                "db": self.db,
                "families": self.families,
                "family": self.family,
                "account": {"telegram_id": 100},
                "has_language": True,
                "lang": "en",
            },
            dialog_data={"telegram_id": 200, "family_id": "9"},
            start=AsyncMock(),
            switch_to=AsyncMock(),
        )
        self.manager.bg.return_value = self.background
        self.callback = SimpleNamespace(from_user=SimpleNamespace(id=100), answer=AsyncMock())
        self.manager.event = self.callback

    def assert_logged_out(self):
        self.families.leave.assert_awaited_once_with(100)
        for key in ("account", "family"):
            self.assertIsNone(self.manager.middleware_data[key])
        self.assertFalse(self.manager.middleware_data["has_language"])
        self.manager.start.assert_awaited_once_with(Menu.language, mode=StartMode.RESET_STACK)

    async def test_unreachable_successor_does_not_interrupt_logout_or_menu_refresh(self):
        menu_opened = []

        async def blocked_successor(*args):
            menu_opened.append(self.manager.start.await_count == 1)
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=200, text="owner"), message="bot was blocked by the user"
            )

        self.families.bot.send_message.side_effect = blocked_successor
        with self.assertLogs("bot.dialogs.menu", level="ERROR"):
            await on_logout(self.callback, None, self.manager)
        self.assert_logged_out()
        self.families.bot.send_message.assert_awaited_once()
        self.background.start.assert_awaited_once_with(Menu.main, mode=StartMode.RESET_STACK)
        self.callback.answer.assert_awaited_once()
        self.assertEqual(menu_opened, [True])

    async def test_successor_language_failure_still_attempts_background_menu(self):
        self.db.language.side_effect = RuntimeError("language lookup unavailable")
        with self.assertLogs("bot.dialogs.menu", level="ERROR"):
            await on_logout(self.callback, None, self.manager)
        self.assert_logged_out()
        self.background.start.assert_awaited_once_with(Menu.main, mode=StartMode.RESET_STACK)

    async def test_background_menu_failure_does_not_interrupt_logout(self):
        for error in (RuntimeError("dialog unavailable"), TimeoutError("delivery timeout")):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.background.start.side_effect = error
                with self.assertLogs("bot.dialogs.menu", level="ERROR"):
                    await on_logout(self.callback, None, self.manager)
                self.assert_logged_out()
                self.families.bot.send_message.assert_awaited_once()
                self.background.start.assert_awaited_once()
                self.callback.answer.assert_awaited_once()

    async def test_callback_answer_failure_does_not_prevent_logout_menu_reset(self):
        self.callback.answer.side_effect = TimeoutError("callback expired")
        with self.assertLogs("bot.dialogs.menu", level="ERROR"):
            await on_logout(self.callback, None, self.manager)
        self.assert_logged_out()

    async def test_actor_menu_failure_does_not_fail_committed_logout(self):
        self.manager.start.side_effect = RuntimeError("actor dialog unavailable")
        with self.assertLogs("bot.dialogs.menu", level="ERROR"):
            await on_logout(self.callback, None, self.manager)
        self.assert_logged_out()
        self.families.bot.send_message.assert_awaited_once()
        self.background.start.assert_awaited_once()

    async def test_failed_leave_preserves_actor_state_and_propagates(self):
        self.families.leave.side_effect = RuntimeError("database unavailable")
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await on_logout(self.callback, None, self.manager)
        self.families.leave.assert_awaited_once_with(100)
        self.assertIsNotNone(self.manager.middleware_data["account"])
        self.manager.start.assert_not_awaited()
        self.background.start.assert_not_awaited()

    async def test_logout_delivery_cancellation_propagates(self):
        self.background.start.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await on_logout(self.callback, None, self.manager)
        self.families.leave.assert_awaited_once_with(100)
        self.assertIsNone(self.manager.middleware_data["account"])

    async def test_member_mutation_succeeds_once_despite_background_menu_failure(self):
        for handler, status in ((approve, "approved"), (block, "blocked"), (remove, None)):
            for error in (RuntimeError("dialog unavailable"), TimeoutError("delivery timeout")):
                with self.subTest(status=status, error=type(error).__name__):
                    self.setUp()
                    self.background.start.side_effect = error
                    with self.assertLogs("bot.dialogs.admin", level="ERROR"):
                        await handler(self.callback, None, self.manager)
                    if status:
                        self.db.set_status.assert_awaited_once_with(
                            200, status, family_id="9", members=self.family.members if status == "approved" else None
                        )
                        self.db.delete_account.assert_not_awaited()
                    else:
                        self.db.delete_account.assert_awaited_once_with(200, family_id="9")
                        self.db.set_status.assert_not_awaited()
                        self.manager.switch_to.assert_awaited_once_with(Admin.members)
                    self.background.start.assert_awaited_once_with(Menu.main, mode=StartMode.RESET_STACK)

    async def test_unchanged_member_does_not_receive_a_menu(self):
        self.db.set_status.return_value = False
        await approve(self.callback, None, self.manager)
        self.db.set_status.assert_awaited_once()
        self.background.start.assert_not_awaited()

    async def test_member_database_failure_propagates(self):
        self.db.delete_account.side_effect = RuntimeError("database unavailable")
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await remove(self.callback, None, self.manager)
        self.db.delete_account.assert_awaited_once_with(200, family_id="9")
        self.background.start.assert_not_awaited()
        self.manager.switch_to.assert_not_awaited()

    async def test_member_delivery_cancellation_propagates(self):
        self.background.start.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await approve(self.callback, None, self.manager)
        self.db.set_status.assert_awaited_once()
