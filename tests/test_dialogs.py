"""Clicks through the real dialogs with aiogram-dialog's test client, backed by a test Postgres (no Steam)."""

import asyncio
import tempfile
import unittest
from datetime import datetime
from itertools import count
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    Message,
    Update,
    User,
)
from aiogram_dialog.api.entities import DialogAction, DialogUpdateEvent
from aiogram_dialog.manager.updater import Updater
from aiogram_dialog.test_tools import BotClient, MockMessageManager
from aiogram_dialog.test_tools.keyboard import InlineButtonTextLocator as Btn
from aiogram_dialog.test_tools.memory_storage import JsonMemoryStorage

from bot.__main__ import AccountMiddleware, create_dispatcher
from bot.db import PgStorage, SteamAlreadyLinked
from bot.family import Families
from bot.login import qr_png
from bot.steam import SteamActionError
from bot.steam_auth import TokenRejected
from tests.pg import fresh_db, needs_pg

OWNER, FRIEND, PENDING, NEWCOMER = 100, 200, 300, 400
ALICE, BOB, CAROL = "76561198000000001", "76561198000000002", "76561198000000003"
FAMILY = "9"


class StubLogins:
    def __init__(self):
        self.started, self.cancelled = [], []
        self.qr = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        self.qr.write(qr_png("https://s.team/q/1/42"))
        self.qr.close()

    async def start(self, telegram_id, manager):
        self.started.append(telegram_id)
        return {"login": "waiting", "qr": self.qr.name, "url": "https://s.team/q/1/42"}

    def cancel(self, telegram_id):
        self.cancelled.append(telegram_id)

    async def forget(self, telegram_id):
        self.cancel(telegram_id)


class AccountMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_updates_in_unloaded_groups_are_rejected(self):
        event = DialogUpdateEvent(
            from_user=User(id=PENDING, is_bot=False, first_name=""),
            chat=Chat(id=-100, type=""),
            action=DialogAction.UPDATE,
            data={},
            intent_id=None,
            stack_id=None,
            thread_id=None,
            business_connection_id=None,
        )
        handler = AsyncMock()
        await AccountMiddleware()(handler, event, {})
        handler.assert_not_awaited()


@needs_pg
class DialogFlowTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = MockMessageManager()
        cls.logins = StubLogins()
        cls.dp = create_dispatcher(None, None, cls.logins, storage=JsonMemoryStorage(), message_manager=cls.messages)

    async def asyncSetUp(self):
        chart_dir = tempfile.TemporaryDirectory()
        self.addCleanup(chart_dir.cleanup)
        chart_patch = patch("bot.charts.CHART_DIR", Path(chart_dir.name))
        chart_patch.start()
        self.addCleanup(chart_patch.stop)
        self.db = await fresh_db()
        self.families = Families(self.db, http=None, bot=None)
        self.families.refresh = AsyncMock()  # never call Steam from tests
        revoke = patch("bot.steam_auth.SteamAuth.revoke", new_callable=AsyncMock)
        self.revoke = revoke.start()
        self.addCleanup(revoke.stop)
        self.dp.workflow_data.update(db=self.db, families=self.families)

        await self.db.ensure_family(FAMILY, "Fam")
        for telegram_id, steam_id, status in (
            (OWNER, ALICE, "pending"),
            (FRIEND, BOB, "approved"),
            (PENDING, CAROL, "pending"),
        ):
            await self.db.save_account(telegram_id, steam_id, FAMILY, "token")
            await self.db.set_status(telegram_id, status)
        await self.families.make_owner(FAMILY, OWNER)
        await self.db.set_telegram_profile(OWNER, "alice_tg", "Alice T")
        await self.db.set_telegram_profile(FRIEND, None, "Bob T")
        for telegram_id in (OWNER, FRIEND, PENDING, NEWCOMER):
            await self.db.set_language(telegram_id, "en")

        family = self.families.get(FAMILY)
        family.snapshot = {
            "family_id": FAMILY,
            "name": "Fam",
            "members": [ALICE, BOB, CAROL],
            "names": {ALICE: "Alice", BOB: "Bob", CAROL: "Carol"},
            "joined": {ALICE: 1, BOB: 2, CAROL: 3},
            "roles": {ALICE: 1, BOB: 1, CAROL: 2},
            "free_spots": 3,
            "pending_invites": [{"steamid": "76561198000000009", "role": 1}],
            "players": {
                ALICE: {"vanity": "alice", "online": True, "created": 1_400_000_000},
                BOB: {"vanity": None, "online": False, "last_online": 1_790_000_000},
            },
            "playtime": [{"steamid": BOB, "appid": 1, "seconds": 7200}],
            "apps": [
                {"appid": i, "name": f"Game {i:02}", "owners": [ALICE] if i % 3 else [ALICE, BOB], "acquired": i}
                for i in range(1, 21)
            ],
        }
        self.families.stores["US"] = {
            "tag_names": {"19": "Action"},
            "apps": {
                str(i): {
                    "price": i * 100,
                    "price_text": f"${i}.00",
                    "tags": [19] if i % 2 else [],
                    "play_mode": "multiplayer" if i % 3 == 0 else "singleplayer",
                }
                for i in range(1, 21)
            },
        }
        self.messages.reset_history()
        self.background_updates = []
        process_update = Updater._process_update

        async def track_background(updater, bot, update):
            self.background_updates.append(asyncio.current_task())
            return await process_update(updater, bot, update)

        background = patch.object(Updater, "_process_update", track_background)
        background.start()
        self.addCleanup(background.stop)

    async def asyncTearDown(self):
        try:
            await asyncio.gather(*self.background_updates)
        finally:
            for family in self.families.items.values():
                family.cleanup_charts()
            await self.db.pool.close()

    def last(self, chat_id=None):
        if chat_id is None:
            return self.messages.last_message()
        return next(message for message in reversed(self.messages.sent_messages) if message.chat.id == chat_id)

    def buttons(self, chat_id=None):
        return [b.text for row in self.last(chat_id).reply_markup.inline_keyboard for b in row]

    def client(self, user_id, chat_id=None, chat_type="private"):
        return BotClient(self.dp, user_id=user_id, chat_id=chat_id or user_id, chat_type=chat_type)

    async def fetch_members(self, members):
        family = self.families.get(FAMILY)
        snapshot = {**family.snapshot, "members": members}

        async def access_token(auth):
            return auth.steam_id

        async def steam_snapshot(http, steam_id):
            return snapshot if steam_id in members else {**snapshot, "family_id": "other", "members": [steam_id]}

        with (
            patch("bot.family.SteamAuth.get_access_token", access_token),
            patch("bot.family.fetch_family_snapshot", steam_snapshot),
        ):
            family.snapshot = await self.families._fetch(FAMILY)
        self.assertIsNotNone(family.snapshot)

    async def test_first_start_asks_language_then_shows_russian(self):
        client = self.client(500)
        await client.send("/start")
        self.assertIn("Выберите язык", self.last().text)
        self.assertEqual(self.buttons(), ["🇬🇧 English", "🇷🇺 Русский"])
        await client.click(self.last(), Btn("🇷🇺 Русский"))
        self.assertEqual(await self.db.language(500), "ru")
        self.assertIn("Войдите через свой аккаунт Steam", self.last().text)
        self.assertIn("Войти через Steam", self.buttons())
        await client.send("/start")  # remembered
        self.assertIn("Войти через Steam", self.buttons())

        await client.click(self.last(), Btn("Войти через Steam"))
        self.assertIn("сеанс сайта Steam", self.last().text)
        await client.click(self.last(), Btn("Перейти к QR-коду"))
        self.assertIn("Веб-браузер", self.last().caption)
        self.assertIn("город, где стоит сервер бота", self.last().caption)  # LOGIN_LOCATION not set

    async def test_public_info_before_login_and_safe_instance_values(self):
        client = self.client(500)
        with (
            patch("bot.dialogs.menu.INSTANCE_OPERATOR", "Owner <admin>"),
            patch("bot.dialogs.menu.INSTANCE_HOSTING", "Provider & region"),
            patch("bot.dialogs.menu.LOGIN_LOCATION", "Vienna, AT"),
        ):
            await client.send("/about")
        self.assertIn("Owner &lt;admin&gt;", self.last().text)
        self.assertIn("Provider &amp; region", self.last().text)
        self.assertIn("Vienna, AT", self.last().text)
        self.assertIn("github.com/AuthFailed/steamily", self.last().text)
        self.assertIsNone(await self.db.account(500))
        await client.click(self.last(), Btn("Privacy and deletion"))
        self.assertIn("refresh token", self.last().text)
        self.assertIn("/delete", self.last().text)
        self.assertIn("authorizeddevices", self.last().text)
        registered = self.client(FRIEND)
        await registered.send("/about")
        await registered.click(self.last(), Btn("Privacy and deletion"))
        self.assertIn("refresh token", self.last().text)

    async def test_status_uses_only_successful_check_time(self):
        client = self.client(500)
        await client.send("/status")
        self.assertIn("responding", self.last().text)
        self.assertIn("No successful check since restart", self.last().text)
        self.families.last_steam_check = 1_700_000_000.0
        await client.send("/status")
        self.assertIn("2023-11-14 22:13:20 UTC", self.last().text)
        self.assertNotIn(ALICE, self.last().text)
        self.families.last_family_check[FAMILY] = 1_700_000_060.0
        await self.client(FRIEND).send("/status")
        self.assertIn("2023-11-14 22:14:20 UTC", self.last().text)

    async def test_public_info_in_russian_and_preferences_only_deletion(self):
        client = self.client(NEWCOMER)
        await self.db.set_language(NEWCOMER, "ru")
        await client.send("/privacy")
        self.assertIn("Срок хранения и удаление", self.last().text)
        await client.send("/about")
        self.assertIn("Оператор не указал", self.last().text)
        await client.send("/delete")
        await client.click(self.last(), Btn("Да, выйти"))
        self.assertIsNone(await self.db.language(NEWCOMER))
        self.assertIn("/start", self.last().text)

    async def test_logout_does_not_recreate_saved_dialogs(self):
        storage = self.dp.fsm.storage
        self.dp.fsm.storage = PgStorage(self.db)
        try:
            client = self.client(FRIEND)
            await client.send("/start")
            old_menu = self.last()
            await client.send("/delete")
            self.assertIn("saved dialogs", self.last().text)
            self.assertGreater(await self.db.pool.fetchval("SELECT count(*) FROM fsm"), 0)
            await client.click(self.last(), Btn("Yes, log out"))
            self.assertIsNone(await self.db.account(FRIEND))
            self.assertIsNone(await self.db.language(FRIEND))
            self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM fsm"), 0)
            self.assertIn("/start", self.last().text)
            self.dp.fsm.storage = PgStorage(self.db)
            self.dp.workflow_data["families"] = Families(self.db, http=None, bot=None)
            await client.click(old_menu, Btn("Family library"))
            self.assertIsNone(await self.db.account(FRIEND))
            self.assertNotIn("Family library", self.buttons())
            await client.send("/start")
            self.assertIn("Choose the bot language", self.last().text)
        finally:
            self.dp.fsm.storage = storage

    async def test_newcomer_logs_in_from_qr_screen(self):
        client = self.client(NEWCOMER)
        await client.send("/start")
        self.assertIn("Log in with your Steam account", self.last().text)
        self.assertEqual(self.buttons(), ["Log in with Steam", "Settings", "About this bot"])
        started_before = len(self.logins.started)
        with patch("bot.dialogs.menu.LOGIN_LOCATION", "Vienna, Wien, AT"):
            await client.click(self.last(), Btn("Log in with Steam"))
            self.assertIn("website session", self.last().text)
            self.assertEqual(len(self.logins.started), started_before)
            await client.click(self.last(), Btn("Continue to QR"))
        self.assertEqual(self.logins.started[-1], NEWCOMER)
        login = self.last()
        self.assertIsNotNone(login.photo)  # the QR is part of the dialog message
        self.assertIn("Web browser", login.caption)
        self.assertIn("Vienna, Wien, AT", login.caption)
        self.assertEqual(login.reply_markup.inline_keyboard[0][0].url, "https://s.team/q/1/42")
        await client.click(login, Btn("Cancel"))
        self.assertEqual(self.logins.cancelled[-1], NEWCOMER)
        self.assertIn("Log in with Steam", self.buttons())

    async def test_pending_member_waits(self):
        await self.client(PENDING).send("/start")
        self.assertIn("Waiting for the family owner", self.last().text)
        self.assertEqual(self.buttons(), ["Settings", "About this bot"])

    async def test_departed_member_loses_open_library_and_inline_access(self):
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        library = self.last()
        await self.db.set_status(FRIEND, "pending")
        await client.click(library, Btn("Game 01 · \\$1.00"))
        self.assertIn("Waiting for the family owner", self.last().text)
        self.assertEqual(self.buttons(), ["Settings", "About this bot"])

        await client.click(self.last(), Btn("Settings"))
        self.assertIn("Log out", self.buttons())
        answer = AsyncMock()
        with patch("aiogram.types.InlineQuery.answer", answer):
            await self.dp.feed_update(
                client.bot,
                Update(
                    update_id=4,
                    inline_query=InlineQuery(id="left", from_user=client.user, query="game", offset=""),
                ),
            )
        self.assertEqual(answer.await_args.args[0], [])
        await self.client(OWNER).send("/start")
        self.assertIn("Family library", self.buttons())

    async def test_departed_member_cannot_use_old_main_menu_library_button(self):
        client = self.client(FRIEND)
        await client.send("/start")
        old_menu = self.last()
        await self.db.set_status(FRIEND, "pending")
        await client.click(old_menu, Btn("Family library"))
        self.assertIn("Waiting for the family owner", self.last().text)
        self.assertEqual(self.buttons(), ["Settings", "About this bot"])

    async def test_departed_owner_cannot_approve_from_open_admin_dialog(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        await client.click(self.last(), Btn("⏳ Carol"))
        old_admin = self.last()
        await self.db.set_status(OWNER, "pending")
        await client.click(old_admin, Btn("Approve"))
        self.assertEqual((await self.db.account(PENDING))["status"], "pending")
        self.assertEqual((await self.db.account(OWNER))["status"], "pending")
        self.assertIn("Waiting for the family owner", self.last().text)

    async def test_owner_cannot_approve_departed_target_from_old_admin_card(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        await client.click(self.last(), Btn("⏳ Carol"))
        old_admin = self.last()
        self.families.get(FAMILY).snapshot["members"].remove(CAROL)
        await client.click(old_admin, Btn("Approve"))
        self.assertEqual((await self.db.account(PENDING))["status"], "pending")

    async def test_owner_can_remove_departed_target_and_release_their_steam_link(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        await client.click(self.last(), Btn("✅ Bob"))
        old_admin = self.last()
        self.families.get(FAMILY).snapshot["members"].remove(BOB)
        await self.db.set_status(FRIEND, "pending")
        await client.click(old_admin, Btn("Remove"))
        self.assertIsNone(await self.db.account(FRIEND))
        await self.db.check_steam_link(NEWCOMER, BOB)

    async def test_pending_former_owner_cannot_approve_themself_with_forged_admin_button(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        # Callback payloads can name an account or action even when no such button was rendered.
        self_button = self.button("✅ Bob").model_copy(
            update={
                "text": "Self",
                "callback_data": self.button("✅ Bob").callback_data.rsplit(":", 1)[0] + f":{ALICE}",
            }
        )
        message = self.last().model_copy(update={"reply_markup": InlineKeyboardMarkup(inline_keyboard=[[self_button]])})
        await client.click(message, Btn("Self"))
        approve = InlineKeyboardButton(
            text="Approve", callback_data=self.button("Block").callback_data.removesuffix("block") + "approve"
        )
        message = self.last().model_copy(update={"reply_markup": InlineKeyboardMarkup(inline_keyboard=[[approve]])})
        await self.db.set_status(OWNER, "pending")
        await client.click(message, Btn("Approve"))
        self.assertEqual((await self.db.account(OWNER))["status"], "pending")
        self.assertIn("Waiting for the family owner", self.last().text)

    async def test_owner_cannot_change_moved_target_from_old_admin_card(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        await client.click(self.last(), Btn("✅ Bob"))
        old_admin = self.last()
        await self.db.ensure_family("10", "Other family")
        await self.db.save_account(FRIEND, BOB, "10", "token")
        await self.families.make_owner("10", FRIEND)
        await client.click(old_admin, Btn("Block"))
        self.assertEqual((await self.db.account(FRIEND))["status"], "approved")

    async def test_former_owner_cannot_rename_from_open_dialog(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn("✏️ Rename family"))
        await self.families.make_owner(FAMILY, FRIEND)
        with (
            patch("bot.dialogs.family.rename_family", AsyncMock()) as rename,
            patch.object(Message, "answer", AsyncMock()) as answer,
        ):
            await client.send("Should not rename")
        rename.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertIn("Family library", self.buttons())

    async def test_family_move_invalidates_existing_family_action(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn("✏️ Rename family"))
        await self.db.ensure_family("10", "Other family")
        await self.db.save_account(OWNER, ALICE, "10", "token")
        await self.families.make_owner("10", OWNER)
        self.families.get("10").snapshot = {
            **self.families.get(FAMILY).snapshot,
            "family_id": "10",
            "name": "Other family",
        }
        with (
            patch("bot.dialogs.family.rename_family", AsyncMock()) as rename,
            patch.object(Message, "answer", AsyncMock()) as answer,
        ):
            await client.send("Should not rename either family")
        rename.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertIn("Other family", self.last().text)

    async def test_revoked_member_cannot_use_old_menu_or_library_buttons(self):
        client = self.client(FRIEND)
        await client.send("/start")
        menu = self.last()
        await client.click(menu, Btn("Family library"))
        library = self.last()
        await self.fetch_members([ALICE, CAROL])

        await client.click(library, Btn("Game 01 · \\$1.00"))
        self.assertNotIn("store.steampowered.com", self.last().text)
        self.assertIn("Log in with Steam", self.buttons())
        await client.click(menu, Btn("Family library"))
        self.assertNotIn("Games: <b>20</b>", self.last().text)
        self.assertNotIn("Family library", self.buttons())

    async def test_revoked_member_cannot_open_library_from_old_main_menu(self):
        client = self.client(FRIEND)
        await client.send("/start")
        menu = self.last()
        await self.fetch_members([ALICE, CAROL])

        await client.click(menu, Btn("Family library"))
        self.assertNotIn("Games: <b>20</b>", self.last().text)
        self.assertEqual(self.buttons(), ["Log in with Steam", "Settings", "About this bot"])

    async def test_revoked_owner_cannot_submit_old_family_action(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn("✏️ Rename family"))
        await self.db.update_family(FAMILY, reader_telegram_id=FRIEND)
        await self.fetch_members([BOB, CAROL])

        with (
            patch("bot.dialogs.family.rename_family", AsyncMock()) as rename,
            patch.object(Message, "answer", AsyncMock()) as answer,
        ):
            await client.send("Unauthorized name")
        rename.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertNotIn("Manage family", self.last().text)
        self.assertIn("Log in with Steam", self.buttons())

        await self.client(FRIEND).send("/start")
        self.assertIn("Family library", self.buttons())

    async def test_former_owner_cannot_use_old_bot_management_buttons(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        await client.click(self.last(), Btn("✅ Bob"))
        member = self.last()
        await self.families.make_owner(FAMILY, FRIEND)

        await client.click(member, Btn("Block"))
        self.assertEqual((await self.db.account(FRIEND))["status"], "approved")
        self.assertIn("Family library", self.buttons())

    async def test_command_for_another_bot_cannot_bypass_owner_access(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn("✏️ Rename family"))
        await self.families.make_owner(FAMILY, FRIEND)
        self.families.owner_token = AsyncMock(return_value="token")
        with (
            patch("bot.dialogs.family.rename_family", AsyncMock()) as rename,
            patch("aiogram.types.Message.answer", AsyncMock()),
            patch.object(
                client.bot,
                "me",
                AsyncMock(
                    return_value=User(
                        id=1,
                        is_bot=True,
                        first_name="Bot",
                        username="this_bot",
                    )
                ),
            ),
        ):
            await client.send("/about@another_bot")
        rename.assert_not_awaited()

    async def test_old_bot_management_card_cannot_change_migrated_member(self):
        await self.db.ensure_family("10", "New family")
        client = self.client(OWNER)
        for action in ("Approve", "Block", "Remove"):
            with self.subTest(action=action):
                await self.db.save_account(PENDING, CAROL, FAMILY, "token")
                await self.db.set_status(PENDING, "pending")
                await self.open_bot_members(client)
                await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
                await client.click(self.last(), Btn("⏳ Carol"))
                member = self.last()
                await self.db.save_account(PENDING, CAROL, "10", "new-token")

                await client.click(member, Btn(action))
                account = await self.db.account(PENDING)
                self.assertIsNotNone(account)
                self.assertEqual((account["family_id"], account["status"]), ("10", "pending"))
                self.assertEqual(self.db.refresh_token(account), "new-token")

    async def test_old_approve_button_cannot_reapprove_departed_member(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        await client.click(self.last(), Btn("⏳ Carol"))
        member = self.last()
        await self.fetch_members([ALICE, BOB])

        await client.click(member, Btn("Approve"))
        self.assertEqual((await self.db.account(PENDING))["status"], "pending")

    async def test_inline_search_denies_revoked_member_and_keeps_survivor(self):
        await self.fetch_members([ALICE, CAROL])
        answer = AsyncMock()
        with (
            patch("bot.inline.store_cards", AsyncMock(return_value={})),
            patch("aiogram.types.InlineQuery.answer", answer),
        ):
            for telegram_id in (FRIEND, OWNER):
                await self.dp.feed_update(
                    self.client(telegram_id).bot,
                    Update(
                        update_id=telegram_id,
                        inline_query=InlineQuery(
                            id=str(telegram_id),
                            from_user=User(id=telegram_id, is_bot=False, first_name="U"),
                            query="",
                            offset="",
                        ),
                    ),
                )
        revoked, survivor = answer.await_args_list
        self.assertEqual(revoked.args[0], [])
        self.assertEqual(revoked.kwargs["button"].start_parameter, "login")
        self.assertTrue(survivor.args[0])
        self.assertIn("Alice", survivor.args[0][0].input_message_content.message_text)

    async def test_group_chats_ignore_everything_but_link(self):
        await self.client(OWNER, chat_id=-5, chat_type="supergroup").send("/start")
        self.assertEqual(self.messages.sent_messages, [])

    async def test_owner_links_group_and_others_cannot(self):
        with self.assertRaisesRegex(RuntimeError, "Fake bot"):  # the "not allowed" reply
            await self.client(FRIEND, chat_id=-500, chat_type="group").send("/link")
        self.assertIsNone((await self.db.family(FAMILY))["notify_chat"])
        with self.assertRaisesRegex(RuntimeError, "Fake bot"):  # the "linked" reply
            await self.client(OWNER, chat_id=-500, chat_type="group").send("/link")
        self.assertEqual((await self.db.family(FAMILY))["notify_chat"], -500)

    def button(self, text):
        return next(b for row in self.last().reply_markup.inline_keyboard for b in row if b.text == text)

    async def test_personal_game_status_can_be_marked_changed_and_cleared(self):
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        await client.click(self.last(), Btn(r"Game 01 · \$1.00"))
        self.assertIn("Personal status: Unmarked", self.last().text)
        self.assertNotIn("Clear status", self.buttons())
        for label, status in (("Backlog", "backlog"), ("Playing", "playing"), ("Completed", "completed")):
            await client.click(self.last(), Btn(label))
            self.assertIn("Personal status: " + label, self.last().text)
            self.assertIn("• " + label, self.buttons())
            self.assertEqual(await self.db.game_marks(FRIEND), {1: {"status": status, "name": "Game 01"}})
        self.assertIn("manual", self.last().text.lower())
        self.assertIn("private", self.last().text.lower())
        await client.click(self.last(), Btn("Clear status"))
        self.assertIn("Personal status: Unmarked", self.last().text)
        self.assertEqual(await self.db.game_marks(FRIEND), {})

    async def test_personal_status_is_private_and_forged_unknown_games_cannot_be_marked(self):
        await self.db.set_game_status(OWNER, ALICE, FAMILY, 1, "Game 01", "completed")
        await self.db.set_game_status(OWNER, ALICE, FAMILY, 999, "Owner's private game", "backlog")
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        game_button = self.button("Game 01 · $1.00")
        await client.click(self.last(), Btn(r"Game 01 · \$1.00"))
        self.assertIn("Personal status: Unmarked", self.last().text)
        mark_button = self.button("Backlog")
        await client.click(self.last(), Btn("Playing"))
        self.assertEqual(await self.db.game_marks(FRIEND), {1: {"status": "playing", "name": "Game 01"}})
        await client.click(self.last(), Btn("◀️ Back"))
        forged_game = game_button.model_copy(
            update={"text": "Forged game", "callback_data": game_button.callback_data.rsplit(":", 1)[0] + ":999"}
        )
        message = self.last().model_copy(update={"reply_markup": InlineKeyboardMarkup(inline_keyboard=[[forged_game]])})
        await client.click(message, Btn("Forged game"))
        self.assertNotIn("Owner's private game", self.last().text)
        self.assertNotIn("Personal status: Backlog", self.last().text)
        message = self.last().model_copy(update={"reply_markup": InlineKeyboardMarkup(inline_keyboard=[[mark_button]])})
        await client.click(message, Btn("Backlog"))
        self.assertEqual(await self.db.game_marks(FRIEND), {1: {"status": "playing", "name": "Game 01"}})
        self.assertEqual(
            await self.db.game_marks(OWNER),
            {1: {"status": "completed", "name": "Game 01"}, 999: {"status": "backlog", "name": "Owner's private game"}},
        )

    async def test_personal_status_filter_combines_with_library_filters_and_resets(self):
        for appid, status in ((3, "backlog"), (6, "playing"), (9, "backlog"), (15, "completed")):
            await self.db.set_game_status(FRIEND, BOB, FAMILY, appid, f"Game {appid:02}", status)
        await self.db.set_game_status(OWNER, ALICE, FAMILY, 1, "Game 01", "backlog")
        self.families.member_stats = AsyncMock(return_value={})
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family"))
        await client.click(self.last(), Btn("Members"))
        await client.click(self.last(), Btn("Alice"))
        await client.click(self.last(), Btn("Games we both have"))
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn("Backlog"))
        await client.click(self.last(), Btn(".*Multiplayer only"))
        await client.click(self.last(), Btn("🤝 Players: 1"))
        await client.click(self.last(), Btn("⬜ Alice"))
        await client.click(self.last(), Btn("◀️ Back"))
        await client.click(self.last(), Btn("🏷 Genre: any"))
        await client.click(self.last(), Btn(r"Action \(10\)"))
        await client.click(self.last(), Btn(r"Maximum price \(\$\)"))
        await client.send("10")
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>2</b>", self.last().text)  # 3 and 9 satisfy every filter
        self.assertIn("Game 03 ×2 · $3.00", self.buttons())
        self.assertIn("Game 09 ×2 · $9.00", self.buttons())
        await client.send("09")
        self.assertIn("Games: <b>1</b>", self.last().text)
        self.assertIn("Game 09 ×2 · $9.00", self.buttons())
        await client.click(self.last(), Btn("✖️ Clear search"))
        await client.click(self.last(), Btn("Filters"))
        self.assertIn("• Backlog", self.buttons())
        await client.click(self.last(), Btn("♻️ Reset filters"))
        self.assertIn("• Any status", self.buttons())
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>20</b>", self.last().text)
        self.assertIn("1 / 4", self.buttons())

    async def test_missing_marked_games_are_retained_filterable_and_clearable(self):
        await self.db.set_game_status(FRIEND, BOB, FAMILY, 1, "Game 01", "backlog")
        await self.db.set_game_status(FRIEND, BOB, FAMILY, 2, "Game 02", "playing")
        self.families.get(FAMILY).snapshot["apps"] = self.families.get(FAMILY).snapshot["apps"][2:]
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        self.assertIn("Unavailable marked games (2)", self.buttons())
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn("Backlog"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>0</b>", self.last().text)
        self.assertIn("Unavailable marked games (1)", self.buttons())
        await client.click(self.last(), Btn(r"Unavailable marked games \(1\)"))
        self.assertIn("Game 01", self.buttons())
        self.assertNotIn("Game 02", self.buttons())
        await client.click(self.last(), Btn("Game 01"))
        self.assertIn("Game 01", self.last().text)
        self.assertIn("Personal status: Backlog", self.last().text)
        self.assertIn("no longer", self.last().text.lower())
        self.assertNotIn("Playing", self.buttons())
        self.assertNotIn("Completed", self.buttons())
        await client.click(self.last(), Btn("Clear status"))
        self.assertEqual(await self.db.game_marks(FRIEND), {2: {"status": "playing", "name": "Game 02"}})
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertNotIn("Unavailable marked games", " ".join(self.buttons()))

    async def test_old_game_card_cannot_create_a_mark_after_the_game_leaves(self):
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        await client.click(self.last(), Btn(r"Game 01 · \$1.00"))
        card = self.last()
        self.families.get(FAMILY).snapshot["apps"].pop(0)
        await client.click(card, Btn("Backlog"))
        self.assertEqual(await self.db.game_marks(FRIEND), {})

    async def test_pending_member_cannot_change_status_from_an_old_game_card(self):
        await self.db.set_game_status(FRIEND, BOB, FAMILY, 1, "Game 01", "backlog")
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        await client.click(self.last(), Btn(r"Game 01 · \$1.00"))
        card = self.last()
        await self.db.set_status(FRIEND, "pending")
        await client.click(card, Btn("Completed"))
        self.assertEqual(await self.db.game_marks(FRIEND), {})
        self.assertIn("Waiting for the family owner", self.last().text)
        await self.db.set_status(FRIEND, "approved")
        self.assertEqual(await self.db.game_marks(FRIEND), {1: {"status": "backlog", "name": "Game 01"}})

    async def test_inflight_status_callback_cannot_restore_marks_after_logout_and_relogin(self):
        await self.db.set_game_status(FRIEND, BOB, FAMILY, 1, "Game 01", "backlog")
        old_generation = (await self.db.account(FRIEND))["token_generation"]
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        await client.click(self.last(), Btn(r"Game 01 · \$1.00"))
        save_status = self.db.set_game_status

        async def logout_and_relogin_before_save(*args, **kwargs):
            await self.db.delete_account(FRIEND)
            await self.db.save_account(FRIEND, BOB, FAMILY, "new-token")
            await self.db.set_status(FRIEND, "approved")
            return await save_status(*args, **kwargs)

        with patch.object(self.db, "set_game_status", side_effect=logout_and_relogin_before_save):
            await client.click(self.last(), Btn("Completed"))
        self.assertNotEqual((await self.db.account(FRIEND))["token_generation"], old_generation)
        self.assertEqual(await self.db.game_marks(FRIEND), {})

    async def test_library_pagination_search_filters_and_game(self):
        await self.db.set_publication(
            OWNER,
            ALICE,
            FAMILY,
            "publish_telegram",
            True,
            expected_version=(await self.db.account(OWNER))["publication_version"],
        )
        with self.assertRaises(SteamAlreadyLinked):
            await self.families.register(NEWCOMER, "duplicate", ALICE)
        client = self.client(FRIEND)
        await client.send("/start")
        self.assertNotIn("Admin", self.buttons())
        library = self.button("Family library")
        self.assertEqual((library.style, library.icon_custom_emoji_id), ("success", "5337037846475720165"))
        await client.click(self.last(), Btn("Family library"))
        self.assertIn("Games: <b>20</b>", self.last().text)
        self.assertEqual(
            self.buttons()[:6], [f"Game 0{i} · ${i}.00" if i % 3 else f"Game 0{i} ×2 · ${i}.00" for i in range(1, 7)]
        )
        self.assertEqual(self.buttons()[6:11], ["\u2800", "\u2800", "1 / 4", "›", "»"])  # nothing before page 1
        self.assertEqual(self.button("Filters").icon_custom_emoji_id, "5188217332748527444")
        self.assertEqual(self.button("Sorting").style, "primary")

        await client.click(self.last(), Btn("›"))
        self.assertIn("Game 07 · $7.00", self.buttons())
        await client.click(self.last(), Btn("»"))
        self.assertEqual(self.buttons()[2:7], ["«", "‹", "4 / 4", "\u2800", "\u2800"])  # nothing after the last
        self.assertIn("Game 20 · $20.00", self.buttons())
        await client.click(self.last(), Btn("4 / 4"))  # page picker
        self.assertEqual(self.buttons()[:4], ["1", "2", "3", "4"])
        self.assertEqual(self.buttons()[-2:], ["◀️ Back", "Home"])
        await client.click(self.last(), Btn("2"))
        self.assertIn("2 / 4", self.buttons())
        await client.click(self.last(), Btn("«"))
        self.assertIn("1 / 4", self.buttons())

        await client.send("game 1")
        self.assertIn("Games: <b>11</b>", self.last().text)  # 1, 10..19
        await client.click(self.last(), Btn("✖️ Clear search"))
        self.assertIn("Games: <b>20</b>", self.last().text)

        await client.click(self.last(), Btn("Sorting"))
        self.assertIn("Use the buttons below to sort", self.last().text)
        self.assertIn('<tg-emoji emoji-id="5431736674147114227">', self.last().text)
        self.assertEqual(self.buttons()[:4], ["Name ↑", "Price", "Copies", "Date added"])
        await client.click(self.last(), Btn("Price"))
        self.assertEqual(self.buttons()[1], "Price ↑")
        await client.click(self.last(), Btn("Price ↑"))  # tapping again flips it
        self.assertEqual(self.buttons()[:2], ["Name", "Price ↓"])
        self.assertEqual(self.buttons()[-2:], ["◀️ Back", "Home"])
        await client.click(self.last(), Btn("◀️ Back"))
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn(".*Multiplayer only"))
        await client.click(self.last(), Btn("🏷 Genre: any"))
        await client.click(self.last(), Btn(r"Action \(10\)"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>3</b>", self.last().text)  # odd multiples of 3: 3, 9, 15
        self.assertNotIn("1 / 1", self.buttons())  # no pager for a single page
        self.assertEqual(self.buttons()[0], "Game 15 ×2 · $15.00")
        await client.click(self.last(), Btn("Game 15 ×2 · \\$15.00"))
        self.assertIn("store.steampowered.com/app/15/", self.last().text)
        self.assertIn('<a href="https://t.me/alice_tg">@alice_tg</a>', self.last().text)
        await client.click(self.last(), Btn("Home"))
        self.assertIn("Family library", self.buttons())

    async def test_prices_are_in_the_viewers_currency(self):
        await self.db.pool.execute("UPDATE accounts SET country = 'KZ' WHERE telegram_id = $1", FRIEND)
        self.families.stores["KZ"] = {"apps": {"1": {"price": 100000, "price_text": "1 000₸"}}}
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        self.assertIn("Game 01 · 1 000₸", self.buttons())
        owner = self.client(OWNER)
        await owner.send("/start")  # no country: default store
        await owner.click(self.last(), Btn("Family library"))
        self.assertIn("Game 01 · $1.00", self.buttons())

    async def test_maximum_price_accepts_decimals_and_resets_filters(self):
        store = self.families.stores["US"]["apps"]
        store["1"].update(price=0, price_text="Free")
        store["2"].update(price=None, price_text=None)
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        await client.click(self.last(), Btn("»"))
        await client.click(self.last(), Btn("Filters"))
        self.assertIn("Unknown prices", self.last().text)
        await client.click(self.last(), Btn(r"Maximum price \(\$\)"))
        self.assertIn("maximum price in $", self.last().text)
        for invalid in ("-1", "1.001", "$5", "NaN"):
            await client.send(invalid)
            self.assertIn("Enter a non-negative amount", self.last().text)
        await client.send("5,00")
        self.assertIn("• Up to 5.00 $", self.buttons())
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>4</b>", self.last().text)
        self.assertIn("Up to 5.00 $", self.last().text)
        self.assertIn("Game 05 · $5.00", self.buttons())
        self.assertNotIn("Game 02", " ".join(self.buttons()))
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn("Free"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>1</b>", self.last().text)
        self.assertIn("Game 01 · Free", self.buttons())
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn("Any price"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>20</b>", self.last().text)
        self.assertIn("1 / 4", self.buttons())
        self.assertNotIn("Up to", self.last().text)
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn(r"Maximum price \(\$\)"))
        await client.send("0")
        await client.click(self.last(), Btn("♻️ Reset filters"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>20</b>", self.last().text)

    async def test_maximum_price_uses_kzt_units_and_can_be_cancelled(self):
        await self.db.pool.execute("UPDATE accounts SET country = 'KZ' WHERE telegram_id = $1", FRIEND)
        self.families.stores["KZ"] = {
            "apps": {
                "1": {"price": 100050, "price_text": "1 000,50₸"},
                "2": {"price": 100051, "price_text": "1 000,51₸"},
            }
        }
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn(r"Maximum price \(₸\)"))
        self.assertIn("maximum price in ₸", self.last().text)
        await client.send("1000.50")
        self.assertIn("• Up to 1000.50 ₸", self.buttons())
        await client.click(self.last(), Btn(".*Up to 1000.50 ₸"))
        await client.send("-100")
        await client.click(self.last(), Btn("◀️ Back"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Up to 1000.50 ₸", self.last().text)
        self.assertIn("Games: <b>1</b>", self.last().text)
        self.assertIn("Game 01 · 1 000,50₸", self.buttons())

    async def test_budget_is_unavailable_without_local_currency_data(self):
        await self.db.pool.execute("UPDATE accounts SET country = 'KZ' WHERE telegram_id = $1", FRIEND)
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        self.assertNotIn("$", " ".join(self.buttons()))
        await client.click(self.last(), Btn("Filters"))
        self.assertNotIn("Maximum price", " ".join(self.buttons()))
        self.assertIn("Local currency is unavailable", self.last().text)
        await client.click(self.last(), Btn("Free"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>0</b>", self.last().text)

    async def test_budget_is_reset_when_the_viewers_store_country_changes(self):
        client = self.client(FRIEND)
        for country, price_text in (("KZ", "500₸"), ("CA", "$500.00")):
            with self.subTest(country=country):
                await self.db.pool.execute("UPDATE accounts SET country = NULL WHERE telegram_id = $1", FRIEND)
                await client.send("/start")
                await client.click(self.last(), Btn("Family library"))
                await client.click(self.last(), Btn("Filters"))
                await client.click(self.last(), Btn(r"Maximum price \(\$\)"))
                await client.send("5")
                self.families.stores[country] = {"apps": {"1": {"price": 50000, "price_text": price_text}}}
                await self.db.pool.execute("UPDATE accounts SET country = $1 WHERE telegram_id = $2", country, FRIEND)
                await client.click(self.last(), Btn("◀️ Back"))
                self.assertIn("Games: <b>20</b>", self.last().text)
                self.assertIn("Store region changed", self.last().text)
                self.assertNotIn("Up to", self.last().text)

    async def test_budget_input_is_not_reinterpreted_after_a_country_change(self):
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Family library"))
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn(r"Maximum price \(\$\)"))
        self.families.stores["KZ"] = {"apps": {"1": {"price": 50000, "price_text": "500₸"}}}
        await self.db.pool.execute("UPDATE accounts SET country = 'KZ' WHERE telegram_id = $1", FRIEND)
        await client.send("5")
        self.assertIn("Store region changed", self.last().text)
        self.assertIn("• Any price", self.buttons())
        self.assertIn("Maximum price (₸)", self.buttons())

    async def test_play_together_lives_in_filters(self):
        client = self.client(FRIEND)
        await client.send("/start")
        self.assertNotIn("Play together", " ".join(self.buttons()))
        await client.click(self.last(), Btn("Family library"))
        await client.click(self.last(), Btn("Filters"))
        await client.click(self.last(), Btn("🤝 Players: 1"))  # own account preselected
        self.assertIn("✅ Bob", self.buttons())
        await client.click(self.last(), Btn("⬜ Alice"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("✅ Multiplayer only", self.buttons())  # turned on for playing together
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Games: <b>6</b>", self.last().text)  # 3, 6, ..., 18 have two copies
        self.assertIn("needs 2+ copies", self.last().text)

    async def open_bot_members(self, client):
        await client.send("/start")
        self.assertNotIn("Admin", self.buttons(client.chat.id))  # merged into family management
        await client.click(self.last(client.chat.id), Btn("Family"))
        await client.click(self.last(client.chat.id), Btn("Manage family"))

    async def test_owner_approves_and_blocks_from_family_management(self):
        self.families.member_stats = AsyncMock(return_value={})
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(OWNER), Btn(r"Bot members \(1 pending\)"))
        self.assertEqual(self.buttons(OWNER)[:2], ["✅ Bob", "⏳ Carol"])  # the owner is not listed

        await client.click(self.last(OWNER), Btn("⏳ Carol"))
        await client.click(self.last(OWNER), Btn("Approve"))
        self.assertEqual((await self.db.account(PENDING))["status"], "approved")
        await self.client(PENDING).send("/start")
        self.assertIn("Family library", self.buttons(PENDING))

        await self.open_bot_members(client)
        await client.click(self.last(OWNER), Btn(r"Bot members \(0 pending\)"))
        await client.click(self.last(OWNER), Btn("✅ Bob"))
        await client.click(self.last(OWNER), Btn("Block"))
        self.assertEqual((await self.db.account(FRIEND))["status"], "blocked")
        await self.client(FRIEND).send("/start")
        self.assertIn("blocked", self.last(FRIEND).text)

        await self.open_bot_members(client)
        await client.click(self.last(OWNER), Btn(r"Bot members \(0 pending\)"))
        await client.click(self.last(OWNER), Btn("◀️ Back"))
        self.assertIn("Manage family", self.last(OWNER).text)  # back where it was opened

    async def test_approval_refreshes_member_menu_without_erasing_telegram_profile(self):
        await self.client(PENDING).send("/start")
        await self.db.set_telegram_profile(PENDING, "carol_tg", "Carol T")
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        await client.click(self.last(), Btn("⏳ Carol"))
        completed = asyncio.Event()
        release = asyncio.Event()
        escaped = []
        process_update = Updater._process_update

        async def process_background(updater, bot, update):
            await release.wait()
            try:
                await process_update(updater, bot, update)
            except Exception as error:
                escaped.append(error)
            finally:
                completed.set()

        with patch.object(Updater, "_process_update", process_background):
            try:
                await client.click(self.last(), Btn("Approve"))
                await client.click(self.last(), Btn("◀️ Back"))
                await client.click(self.last(), Btn("◀️ Back"))
            finally:
                release.set()
                await asyncio.wait_for(completed.wait(), timeout=2)
        self.assertFalse(escaped)
        self.assertEqual(self.last().chat.id, PENDING)
        self.assertIn("Manage family", self.last(OWNER).text)
        account = await self.db.account(PENDING)
        self.assertEqual(account["status"], "approved")
        self.assertEqual((account["tg_username"], account["tg_name"]), ("carol_tg", "Carol T"))
        member_menu = self.last(PENDING)
        buttons = [button.text for row in member_menu.reply_markup.inline_keyboard for button in row]
        self.assertIn("Family library", buttons)

    async def test_failed_approval_background_delivery_is_logged_without_failing_approval(self):
        await self.client(PENDING).send("/start")
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        await client.click(self.last(), Btn("⏳ Carol"))
        completed = asyncio.Event()
        escaped = []
        process_update = Updater._process_update
        show_message = self.messages.show_message

        async def process_background(updater, bot, update):
            try:
                await process_update(updater, bot, update)
            except Exception as error:
                escaped.append(error)
            finally:
                completed.set()

        async def show_for_reachable_user(bot, new_message, old_message):
            if new_message.chat.id == PENDING:
                raise TelegramForbiddenError(
                    method=SendMessage(chat_id=PENDING, text="menu"), message="bot was blocked by the user"
                )
            return await show_message(bot, new_message, old_message)

        with (
            patch.object(Updater, "_process_update", process_background),
            patch.object(self.messages, "show_message", side_effect=show_for_reachable_user) as show,
            patch.object(self.db, "set_status", wraps=self.db.set_status) as save_status,
            self.assertLogs("bot.__main__", level="ERROR") as logged,
        ):
            await client.click(self.last(), Btn("Approve"))
            await asyncio.wait_for(completed.wait(), timeout=2)
        self.assertFalse(escaped)
        self.assertTrue(any(call.args[1].chat.id == PENDING for call in show.await_args_list))
        self.assertTrue(any(str(PENDING) in message for message in logged.output))
        save_status.assert_awaited_once_with(
            PENDING, "approved", family_id=FAMILY, members=self.families.get(FAMILY).members
        )
        self.assertEqual((await self.db.account(PENDING))["status"], "approved")
        owner_menu = [message for message in self.messages.sent_messages if message.chat.id == OWNER][-1]
        buttons = [button.text for row in owner_menu.reply_markup.inline_keyboard for button in row]
        self.assertIn("Block", buttons)
        self.assertNotIn("Approve", buttons)

    async def test_owner_group_alert_settings(self):
        self.families.bot_username = "family_steambot"
        client = self.client(OWNER)
        await client.send("/start")
        await client.click(self.last(), Btn("Settings"))
        await client.click(self.last(), Btn("Group notifications"))
        self.assertIn("No group yet", self.last().text)
        self.assertEqual(self.button("➕ Add bot to a group").url, "https://t.me/family_steambot?startgroup=link")
        self.assertNotIn("✅ New games", self.buttons())  # toggles appear once a group is linked

        await self.db.update_family(FAMILY, notify_chat=-500)
        with patch.object(
            type(client.bot), "get_chat", AsyncMock(return_value=type("Chat", (), {"title": "Our <group>"}))
        ):
            await client.click(self.last(), Btn("◀️ Back"))
            await client.click(self.last(), Btn("Group notifications"))
            self.assertIn("Group: <b>Our &lt;group&gt;</b>", self.last().text)
            self.assertEqual(
                self.buttons()[1:6],
                [
                    "✅ New games",
                    "⬜ Games leaving the library",
                    "✅ Members joining or leaving",
                    "⬜ Online / offline",
                    "⬜ Started / stopped playing",
                ],
            )
            await client.click(self.last(), Btn("⬜ Online / offline"))
            await client.click(self.last(), Btn("✅ New games"))
        alerts = (await self.db.family(FAMILY))["alerts"]
        self.assertEqual((alerts["online"], alerts["games"]), (True, False))

        self.families.bot = AsyncMock()
        await self.db.enqueue_alerts(FAMILY, [{"text": "Pending before unlink"}])
        self.assertEqual(len(await self.db.pending_alerts(FAMILY, 0)), 1)
        await client.click(self.last(), Btn("✂️ Unlink group"))
        self.assertIsNone((await self.db.family(FAMILY))["notify_chat"])
        self.assertIn("No group yet", self.last().text)
        self.assertEqual(await self.db.pending_alerts(FAMILY, 0), [])
        await self.db.enqueue_alerts(FAMILY, [{"text": "After unlink"}])
        await self.families.deliver_alerts(FAMILY)
        self.assertEqual(await self.db.pending_alerts(FAMILY, 0), [])
        self.families.bot.send_message.assert_not_awaited()

    async def test_stale_admin_card_cannot_change_a_target_in_another_family(self):
        client = self.client(OWNER)
        await self.db.ensure_family("8", "Other")
        for button in ("Approve", "Block", "Remove"):
            with self.subTest(button=button):
                await self.db.save_account(PENDING, CAROL, FAMILY, "token")
                await self.db.set_status(PENDING, "pending")
                await self.open_bot_members(client)
                await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
                await client.click(self.last(), Btn("⏳ Carol"))
                card = self.last()
                await self.db.save_account(PENDING, CAROL, "8", "token")
                with patch.object(CallbackQuery, "answer", AsyncMock()) as answer:
                    await client.click(card, Btn(button))
                account = await self.db.account(PENDING)
                self.assertIsNotNone(account)
                self.assertEqual((account["family_id"], account["status"]), ("8", "pending"))
                self.assertTrue(any(call.args and call.args[0] for call in answer.await_args_list))

    async def test_stale_admin_card_denies_a_former_owner(self):
        client = self.client(OWNER)
        for button in ("Approve", "Block", "Remove"):
            with self.subTest(button=button):
                await self.db.save_account(PENDING, CAROL, FAMILY, "token")
                await self.db.set_status(PENDING, "pending")
                await self.families.make_owner(FAMILY, OWNER)
                await self.open_bot_members(client)
                await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
                await client.click(self.last(), Btn("⏳ Carol"))
                card = self.last()
                await self.families.make_owner(FAMILY, FRIEND)
                with patch.object(CallbackQuery, "answer", AsyncMock()) as answer:
                    await client.click(card, Btn(button))
                account = await self.db.account(PENDING)
                self.assertIsNotNone(account)
                self.assertEqual(account["status"], "pending")
                self.assertTrue(any(call.args and call.args[0] for call in answer.await_args_list))

    async def test_owner_only_settings_deny_role_revocation(self):
        client = self.client(OWNER)
        await self.db.update_family(FAMILY, notify_chat=-500)
        for button in ("New member alerts: on", "✅ New games", "✂️ Unlink group"):
            with self.subTest(button=button):
                await self.families.make_owner(FAMILY, OWNER)
                await self.db.set_notify_requests(OWNER, True)
                await client.send("/start")
                await client.click(self.last(), Btn("Settings"))
                if button != "New member alerts: on":
                    with patch.object(
                        type(client.bot), "get_chat", AsyncMock(return_value=Chat(id=-500, type="group", title="Group"))
                    ):
                        await client.click(self.last(), Btn("Group notifications"))
                card = self.last()
                before = dict(await self.db.family(FAMILY))
                await self.families.make_owner(FAMILY, FRIEND)
                with (
                    patch.object(CallbackQuery, "answer", AsyncMock()) as answer,
                    patch.object(
                        type(client.bot), "get_chat", AsyncMock(return_value=Chat(id=-500, type="group", title="Group"))
                    ),
                ):
                    await client.click(card, Btn(button))
                after = await self.db.family(FAMILY)
                self.assertEqual(after["notify_chat"], before["notify_chat"])
                self.assertEqual(after["alerts"], before["alerts"])
                self.assertTrue(await self.db.notify_requests(OWNER))
                self.assertTrue(any(call.args and call.args[0] for call in answer.await_args_list))

    async def test_saved_steam_rename_denies_owner_revocation(self):
        self.families.owner_token = AsyncMock(return_value="token")
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn("✏️ Rename family"))
        await self.families.make_owner(FAMILY, FRIEND)
        with (
            patch("bot.dialogs.family.rename_family", AsyncMock()) as rename,
            patch.object(Message, "answer", AsyncMock()) as answer,
        ):
            await client.send("Changed name")
        rename.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertIn("Family library", self.buttons())

    async def test_saved_invite_input_denies_owner_revocation_before_steam_lookup(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn("➕ Invite member"))
        await self.families.make_owner(FAMILY, FRIEND)
        with (
            patch("bot.dialogs.family.resolve_profile", AsyncMock()) as resolve,
            patch.object(Message, "answer", AsyncMock()) as answer,
        ):
            await client.send("https://steamcommunity.com/id/someone")
        resolve.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertIn("Family library", self.buttons())

    async def test_purchase_requests_deny_owner_revocation_before_steam_lookup(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        card = self.last()
        await self.families.make_owner(FAMILY, FRIEND)
        self.families.owner_token = AsyncMock(return_value="token")
        with (
            patch("bot.dialogs.family.purchase_requests", AsyncMock()) as requests,
            patch.object(CallbackQuery, "answer", AsyncMock()) as answer,
        ):
            await client.click(card, Btn("🛒 Purchase requests"))
        requests.assert_not_awaited()
        self.families.owner_token.assert_not_awaited()
        self.assertTrue(any(call.args and call.args[0] for call in answer.await_args_list))
        self.assertIn("Family library", self.buttons())

    async def test_purchase_request_details_and_confirmation_keep_the_selected_request(self):
        self.families.owner_token = AsyncMock(return_value="token")
        requests = [
            {"request_id": "101", "requester_steamid": CAROL, "time_requested": 1},
            {
                "request_id": "102",
                "requester_steamid": CAROL,
                "time_requested": 2,
                "requested_packageids": [10, 20],
                "requested_bundleids": [30],
                "gidshoppingcart": "404",
            },
        ]
        client = self.client(OWNER)
        with (
            patch("bot.dialogs.family.purchase_requests", AsyncMock(return_value=requests)),
            patch("bot.dialogs.family.decline_purchase", AsyncMock()) as decline,
        ):
            await self.open_bot_members(client)
            await client.click(self.last(), Btn("🛒 Purchase requests"))
            self.assertIn("Carol · 01.01.1970 · #101", self.buttons())
            self.assertIn("Carol · 01.01.1970 · #102", self.buttons())
            await client.click(self.last(), Btn("Carol · 01.01.1970 · #102"))
            self.assertIn("Purchase request", self.last().text)
            self.assertIn("Carol", self.last().text)
            self.assertIn("01.01.1970", self.last().text)
            self.assertIn("102", self.last().text)
            self.assertIn("Package #10", self.last().text)
            self.assertIn("Package #20", self.last().text)
            self.assertIn("Bundle #30", self.last().text)
            self.assertIn("Cart: 404", self.last().text)
            steam_link = next(
                button
                for row in self.last().reply_markup.inline_keyboard
                for button in row
                if button.text == "Open in Steam"
            )
            self.assertEqual(steam_link.url, "https://store.steampowered.com/cart/purchaserequest/9/102")
            decline.assert_not_awaited()
            await client.click(self.last(), Btn("Decline request"))
            self.assertIn("Decline purchase request #102?", self.last().text)
            self.assertIn("102", self.last().text)
            decline.assert_not_awaited()
            await client.click(self.last(), Btn("◀️ Back"))
            decline.assert_not_awaited()
            self.assertIn("Purchase request", self.last().text)
            await client.click(self.last(), Btn("Decline request"))
            await client.click(self.last(), Btn("Yes, decline"))
        decline.assert_awaited_once_with(None, "token", FAMILY, "102")
        self.assertIn("✅ Done", self.last().text)

    async def test_purchase_request_without_item_details_is_explicit_and_safe_to_leave(self):
        self.families.owner_token = AsyncMock(return_value="token")
        request = {"request_id": "101", "requester_steamid": CAROL, "time_requested": 1}
        client = self.client(OWNER)
        with (
            patch("bot.dialogs.family.purchase_requests", AsyncMock(return_value=[request])),
            patch("bot.dialogs.family.decline_purchase", AsyncMock()) as decline,
        ):
            await self.open_bot_members(client)
            await client.click(self.last(), Btn("🛒 Purchase requests"))
            await client.click(self.last(), Btn("Carol · 01.01.1970 · #101"))
            self.assertIn("Item names and prices are incomplete", self.last().text)
            self.assertIn("Carol", self.last().text)
            self.assertIn("101", self.last().text)
            await client.click(self.last(), Btn("◀️ Back"))
            self.assertIn("Carol · 01.01.1970 · #101", self.buttons())
        decline.assert_not_awaited()

    async def test_old_purchase_confirmation_cannot_decline_another_selected_request(self):
        self.families.owner_token = AsyncMock(return_value="token")
        requests = [
            {"request_id": request_id, "requester_steamid": CAROL, "time_requested": 1} for request_id in ("101", "102")
        ]
        client = self.client(OWNER)
        with (
            patch("bot.dialogs.family.purchase_requests", AsyncMock(return_value=requests)),
            patch("bot.dialogs.family.decline_purchase", AsyncMock()) as decline,
        ):
            await self.open_bot_members(client)
            await client.click(self.last(), Btn("🛒 Purchase requests"))
            await client.click(self.last(), Btn("Carol · 01.01.1970 · #101"))
            await client.click(self.last(), Btn("Decline request"))
            old_confirmation = self.last()
            await client.click(self.last(), Btn("◀️ Back"))
            await client.click(self.last(), Btn("◀️ Back"))
            await client.click(self.last(), Btn("Carol · 01.01.1970 · #102"))
            await client.click(self.last(), Btn("Decline request"))
            await client.click(old_confirmation, Btn("Yes, decline"))
            decline.assert_not_awaited()
            self.assertIn("Decline purchase request #102?", self.last().text)
            await client.click(self.last(), Btn("Yes, decline"))
        decline.assert_awaited_once_with(None, "token", FAMILY, "102")

    async def test_purchase_confirmation_rechecks_that_request_is_pending(self):
        self.families.owner_token = AsyncMock(return_value="token")
        request = {"request_id": "101", "requester_steamid": CAROL, "time_requested": 1}
        client = self.client(OWNER)
        for latest in ([], [{**request, "is_completed": True}]):
            with (
                self.subTest(latest=latest),
                patch("bot.steam.get_json", AsyncMock(return_value={"response": {"requests": [request]}})) as requests,
                patch("bot.steam.post_json", AsyncMock()) as decline,
            ):
                await self.open_bot_members(client)
                await client.click(self.last(), Btn("🛒 Purchase requests"))
                await client.click(self.last(), Btn("Carol · 01.01.1970 · #101"))
                await client.click(self.last(), Btn("Decline request"))
                requests.reset_mock()
                requests.return_value = {"response": {"requests": latest}}
                await client.click(self.last(), Btn("Yes, decline"))
                requests.assert_awaited()
                decline.assert_not_awaited()
                self.assertIn("no longer open", self.last().text)
                self.assertNotIn("✅ Done", self.last().text)

    async def test_purchase_confirmation_read_failure_never_declines(self):
        self.families.owner_token = AsyncMock(return_value="token")
        request = {"request_id": "101", "requester_steamid": CAROL, "time_requested": 1}
        client = self.client(OWNER)
        with (
            patch("bot.steam.get_json", AsyncMock(return_value={"response": {"requests": [request]}})) as requests,
            patch("bot.steam.post_json", AsyncMock()) as decline,
        ):
            await self.open_bot_members(client)
            await client.click(self.last(), Btn("🛒 Purchase requests"))
            await client.click(self.last(), Btn("Carol · 01.01.1970 · #101"))
            await client.click(self.last(), Btn("Decline request"))
            requests.side_effect = RuntimeError("Steam unavailable")
            await client.click(self.last(), Btn("Yes, decline"))
        decline.assert_not_awaited()
        self.assertRegex(self.last().text, r"(?i)could not|failed|unavailable|Steam refused")
        self.assertNotIn("✅ Done", self.last().text)

    async def test_purchase_decline_error_never_shows_success(self):
        self.families.owner_token = AsyncMock(return_value="token")
        request = {"request_id": "101", "requester_steamid": CAROL, "time_requested": 1}
        client = self.client(OWNER)
        with (
            patch("bot.dialogs.family.purchase_requests", AsyncMock(return_value=[request])),
            patch(
                "bot.dialogs.family.decline_purchase", AsyncMock(side_effect=SteamActionError("Request refused"))
            ) as decline,
        ):
            await self.open_bot_members(client)
            await client.click(self.last(), Btn("🛒 Purchase requests"))
            await client.click(self.last(), Btn("Carol · 01.01.1970 · #101"))
            await client.click(self.last(), Btn("Decline request"))
            await client.click(self.last(), Btn("Yes, decline"))
        decline.assert_awaited_once_with(None, "token", FAMILY, "101")
        self.assertIn("❌ Steam refused: Request refused", self.last().text)
        self.assertNotIn("✅ Done", self.last().text)

    async def test_purchase_confirmation_denies_owner_revocation_before_steam_lookup(self):
        self.families.owner_token = AsyncMock(return_value="token")
        request = {"request_id": "101", "requester_steamid": CAROL, "time_requested": 1}
        client = self.client(OWNER)
        with (
            patch("bot.dialogs.family.purchase_requests", AsyncMock(return_value=[request])) as requests,
            patch("bot.dialogs.family.decline_purchase", AsyncMock()) as decline,
        ):
            await self.open_bot_members(client)
            await client.click(self.last(), Btn("🛒 Purchase requests"))
            await client.click(self.last(), Btn("Carol · 01.01.1970 · #101"))
            await client.click(self.last(), Btn("Decline request"))
            card = self.last()
            requests.reset_mock()
            self.families.owner_token.reset_mock()
            await self.families.make_owner(FAMILY, FRIEND)
            with patch.object(CallbackQuery, "answer", AsyncMock()) as answer:
                await client.click(card, Btn("Yes, decline"))
        requests.assert_not_awaited()
        decline.assert_not_awaited()
        self.families.owner_token.assert_not_awaited()
        self.assertTrue(any(call.args and call.args[0] for call in answer.await_args_list))
        self.assertIn("Family library", self.buttons())

    async def test_saved_steam_invite_cannot_follow_owner_to_another_family(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn("➕ Invite member"))
        with patch("bot.dialogs.family.resolve_profile", AsyncMock(return_value="76561198000000042")):
            await client.send("https://steamcommunity.com/id/someone")
        card = self.last()
        await self.db.ensure_family("8", "Other")
        await self.db.save_account(OWNER, ALICE, "8", "token")
        await self.families.make_owner("8", OWNER)
        self.families.get("8").snapshot = {**self.families.get(FAMILY).snapshot, "family_id": "8"}
        self.families.owner_token = AsyncMock(return_value="token")
        with (
            patch("bot.dialogs.family.invite_member", AsyncMock()) as invite,
            patch.object(CallbackQuery, "answer", AsyncMock()) as answer,
        ):
            await client.click(card, Btn("Child"))
        invite.assert_not_awaited()
        self.families.owner_token.assert_not_awaited()
        self.assertTrue(any(call.args and call.args[0] for call in answer.await_args_list))

    async def test_purchase_requests_denies_a_deleted_account_and_keeps_home_usable(self):
        client = self.client(OWNER)
        await self.open_bot_members(client)
        card = self.last()
        await self.db.delete_account(OWNER)
        self.families.owner_token = AsyncMock(return_value="token")
        with (
            patch("bot.dialogs.family.purchase_requests", AsyncMock()) as requests,
            patch.object(CallbackQuery, "answer", AsyncMock()) as answer,
        ):
            await client.click(card, Btn("🛒 Purchase requests"))
        requests.assert_not_awaited()
        self.families.owner_token.assert_not_awaited()
        self.assertTrue(any(call.args and call.args[0] for call in answer.await_args_list))
        self.assertIn("Log in with Steam", self.buttons())

    async def test_adding_bot_to_group_links_it_for_the_owner(self):
        self.families.bot = AsyncMock()
        for user_id in (FRIEND, PENDING, NEWCOMER):
            with self.subTest(user_id=user_id):
                await self.membership(user_id, -700, "left", "member")
                self.assertIsNone((await self.db.family(FAMILY))["notify_chat"])
                self.assertIn("Only the Steam Family owner", self.families.bot.send_message.await_args.args[1])
        await self.membership(OWNER, -800, "left", "member")
        self.assertEqual((await self.db.family(FAMILY))["notify_chat"], -800)
        self.assertIn("will be posted here", self.families.bot.send_message.await_args.args[1])

    async def membership(self, user_id, chat_id, old, new):
        bot_user = User(id=1, is_bot=True, first_name="B")
        members = {
            "left": ChatMemberLeft(user=bot_user),
            "member": ChatMemberMember(user=bot_user),
            "kicked": ChatMemberBanned(user=bot_user, until_date=0),
        }
        await self.dp.feed_update(
            self.client(user_id).bot,
            Update(
                update_id=1,
                my_chat_member=ChatMemberUpdated(
                    chat=Chat(id=chat_id, type="supergroup", title="G"),
                    from_user=User(id=user_id, is_bot=False, first_name="U"),
                    date=datetime.now(),
                    old_chat_member=members[old],
                    new_chat_member=members[new] if isinstance(new, str) else new,
                ),
            ),
        )

    async def test_bot_promotion_does_not_relink_or_send_confirmation(self):
        self.families.bot = AsyncMock()
        await self.db.update_family(FAMILY, notify_chat=-800, language="ru")
        administrator = ChatMemberAdministrator(
            user=User(id=1, is_bot=True, first_name="B"),
            can_be_edited=False,
            is_anonymous=False,
            can_manage_chat=True,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
            can_send_welcome_messages=False,
        )
        await self.membership(OWNER, -900, "member", administrator)
        row = await self.db.family(FAMILY)
        self.assertEqual((row["notify_chat"], row["language"]), (-800, "ru"))
        self.families.bot.send_message.assert_not_awaited()

    async def test_bot_removal_or_kick_unlinks_group_and_stops_alerts(self):
        self.families.bot = AsyncMock()
        for status in ("left", "kicked"):
            with self.subTest(status=status):
                await self.db.update_family(FAMILY, notify_chat=-800)
                await self.db.enqueue_alerts(FAMILY, [{"text": "Before removal"}])
                await self.families.deliver_alerts(FAMILY)
                self.families.bot.send_message.assert_awaited_once_with(-800, "Before removal", parse_mode=None)
                self.families.bot.reset_mock()
                await self.db.enqueue_alerts(FAMILY, [{"text": "Pending at removal"}])
                self.assertEqual(
                    await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts WHERE family_id = $1", FAMILY), 1
                )

                await self.membership(FRIEND, -800, "member", status)

                row = await self.db.family(FAMILY)
                self.assertIsNone(row["notify_chat"])
                self.assertEqual(
                    await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts WHERE family_id = $1", FAMILY), 0
                )
                await self.db.enqueue_alerts(FAMILY, [{"text": "After removal"}])
                self.assertEqual(
                    await self.db.pool.fetchval("SELECT count(*) FROM pending_alerts WHERE family_id = $1", FAMILY), 0
                )
                await self.families.deliver_alerts(FAMILY)
                self.families.bot.send_message.assert_not_awaited()

    async def test_removal_from_old_group_keeps_the_new_group_linked(self):
        self.families.bot = AsyncMock()
        await self.membership(OWNER, -800, "left", "member")
        await self.membership(OWNER, -900, "left", "member")
        self.families.bot.reset_mock()
        await self.db.enqueue_alerts(FAMILY, [{"text": "New group alert"}])

        await self.membership(FRIEND, -800, "member", "kicked")

        row = await self.db.family(FAMILY)
        self.assertEqual(row["notify_chat"], -900)
        await self.families.deliver_alerts(FAMILY)
        self.families.bot.send_message.assert_awaited_once_with(-900, "New group alert", parse_mode=None)

    async def test_empty_inline_query_lists_recent_family_games_without_store_search(self):
        answer = AsyncMock()
        with (
            patch("bot.inline.search_store", AsyncMock()) as store_search,
            patch("bot.inline.store_cards", AsyncMock(return_value={})) as details,
            patch("aiogram.types.InlineQuery.answer", answer),
        ):
            await self.dp.feed_update(
                self.client(FRIEND).bot,
                Update(
                    update_id=4,
                    inline_query=InlineQuery(
                        id="empty", from_user=User(id=FRIEND, is_bot=False, first_name="B"), query="   ", offset=""
                    ),
                ),
            )
        store_search.assert_not_awaited()
        expected = list(range(20, 10, -1))
        self.assertEqual(details.await_args.args[1], expected)
        answer.assert_awaited_once()
        results = answer.await_args.args[0]
        self.assertEqual([result.id for result in results], [str(appid) for appid in expected])
        self.assertEqual(results[0].title, "Game 20")
        self.assertIn("Price unknown", results[0].input_message_content.message_text)
        self.assertIsNone(results[0].thumbnail_url)
        self.assertEqual(answer.await_args.kwargs, {"cache_time": 0, "is_personal": True})

    async def test_inline_card_failure_keeps_known_games_and_omits_unknown_store_games(self):
        answer = AsyncMock()
        with (
            patch("bot.inline.search_store", AsyncMock(return_value=[999, 3])),
            patch("bot.inline.store_cards", AsyncMock(side_effect=TimeoutError("Store unavailable"))),
            patch("aiogram.types.InlineQuery.answer", answer),
            self.assertLogs("bot.inline", level="WARNING"),
        ):
            await self.dp.feed_update(
                self.client(FRIEND).bot,
                Update(
                    update_id=5,
                    inline_query=InlineQuery(
                        id="details-failure",
                        from_user=User(id=FRIEND, is_bot=False, first_name="B"),
                        query="game 03",
                        offset="",
                    ),
                ),
            )
        answer.assert_awaited_once()
        results = answer.await_args.args[0]
        self.assertEqual([result.id for result in results], ["3"])
        self.assertEqual(results[0].title, "Game 03")
        card = results[0].input_message_content.message_text
        self.assertIn("Alice", card)
        self.assertIn("Bob", card)
        self.assertIn("Price unknown", card)
        self.assertIn("https://store.steampowered.com/app/3/", card)
        self.assertIsNone(results[0].thumbnail_url)

    async def test_inline_search_shows_family_owners_in_senders_currency(self):
        await self.db.set_publication(
            OWNER,
            ALICE,
            FAMILY,
            "publish_telegram",
            True,
            expected_version=(await self.db.account(OWNER))["publication_version"],
        )
        with self.assertRaises(SteamAlreadyLinked):
            await self.families.register(NEWCOMER, "duplicate", ALICE)
        await self.db.pool.execute("UPDATE accounts SET country = 'KZ' WHERE telegram_id = $1", FRIEND)
        await self.db.set_language(FRIEND, "ru")
        self.families.get(FAMILY).apps[2]["exclude_reason"] = 1
        cards = {
            3: {"name": "Game 03", "description": "Desc", "price_text": "300₸", "thumbnail": "https://img/3.jpg"},
            999: {"name": "Other", "description": "", "price_text": "1 000₸", "thumbnail": None},
        }
        answer = AsyncMock()
        with (
            patch("bot.inline.search_store", AsyncMock(return_value=[999, 3])) as store_search,
            patch("bot.inline.store_cards", AsyncMock(return_value=cards)) as details,
            patch("aiogram.types.InlineQuery.answer", answer),
        ):
            await self.dp.feed_update(
                self.client(FRIEND).bot,
                Update(
                    update_id=2,
                    inline_query=InlineQuery(
                        id="q", from_user=User(id=FRIEND, is_bot=False, first_name="B"), query="game 03", offset=""
                    ),
                ),
            )
        self.assertEqual(store_search.await_args.args[2:], ("ru", "KZ"))
        self.assertEqual(details.await_args.args[2:], ("ru", "KZ"))
        results = answer.await_args.args[0]
        self.assertEqual([r.id for r in results], ["3", "999"])  # family game first
        card = results[0].input_message_content.message_text
        self.assertIn("300₸", card)
        self.assertIn("Семейный доступ исключён: исключено издателем", card)
        self.assertIn(
            'Есть в семье у:\n• <a href="https://steamcommunity.com/id/alice/">Alice</a> · '
            '<a href="https://t.me/alice_tg">@alice_tg</a>\n• <a href="https://steamcommunity.com/profiles/'
            + BOB
            + '/">Bob</a>',
            card,
        )
        self.assertEqual(results[0].reply_markup.inline_keyboard[0][0].url, "https://store.steampowered.com/app/3/")
        self.assertIn("В семье ни у кого нет", results[1].input_message_content.message_text)
        self.assertNotIn("Семейный доступ исключён", results[1].input_message_content.message_text)

        answer.reset_mock()
        with patch("aiogram.types.InlineQuery.answer", answer):
            await self.dp.feed_update(
                self.client(NEWCOMER).bot,
                Update(
                    update_id=3,
                    inline_query=InlineQuery(
                        id="q2", from_user=User(id=NEWCOMER, is_bot=False, first_name="N"), query="x", offset=""
                    ),
                ),
            )
        self.assertEqual(answer.await_args.args[0], [])
        self.assertEqual(answer.await_args.kwargs["button"].start_parameter, "login")

    async def test_settings_language_and_member_logout(self):
        await self.db.set_game_status(FRIEND, BOB, FAMILY, 1, "Game 01", "backlog")
        client = self.client(FRIEND)
        await client.send("/start")
        self.assertEqual(self.buttons(), ["Family library", "Family", "Settings", "About this bot"])
        self.assertEqual(self.button("Settings").icon_custom_emoji_id, "5341715473882955310")
        await client.click(self.last(), Btn("Settings"))
        self.assertEqual(
            self.buttons(),
            ["Publish my presence: off", "Publish my Telegram: off", "Language", "Log out", "◀️ Back"],
        )
        await client.click(self.last(), Btn("Language"))
        await client.click(self.last(), Btn("🇷🇺 Русский"))
        self.assertIn("Настройки", self.last().text)  # back in settings, now in Russian
        await client.click(self.last(), Btn("Выйти"))
        self.assertNotIn("👑", self.last().text)
        await client.click(self.last(), Btn("Да, выйти"))
        self.assertIsNone(await self.db.account(FRIEND))
        self.assertIsNone(await self.db.language(FRIEND))  # settings removed
        self.assertEqual(await self.db.game_marks(FRIEND), {})
        self.assertIn("/start", self.last().text)
        await client.send("/start")
        self.assertIn("Choose the bot language", self.last().text)  # starts over

    async def test_two_members_control_their_own_publication_in_real_dialogs(self):
        # aiogram-dialog's random context IDs can collide when dialogs open within one second.
        intent_ids = count()
        self.enterContext(patch("aiogram_dialog.api.entities.stack.new_id", side_effect=lambda: f"t{next(intent_ids)}"))
        family = self.families.get(FAMILY)
        family.snapshot["players"][BOB]["online"] = True
        owner, friend = self.client(OWNER), self.client(FRIEND)
        with patch.object(self.families, "_fetch", AsyncMock(return_value=family.snapshot)):
            await owner.send("/start")
            await owner.click(self.last(), Btn("Settings"))
            self.assertIn("existing and new accounts", self.last().text)
            self.assertIn("Already delivered", self.last().text)
            await owner.click(self.last(), Btn("Publish my Telegram: off"))
            await friend.send("/start")
            await friend.click(self.last(), Btn("Settings"))
            await friend.click(self.last(), Btn("Publish my presence: off"))
        alice, bob = await self.db.account(OWNER), await self.db.account(FRIEND)
        self.assertEqual((alice["publish_presence"], alice["publish_telegram"]), (False, True))
        self.assertEqual((bob["publish_presence"], bob["publish_telegram"]), (True, False))

        await friend.send("/start")
        await friend.click(self.last(), Btn("Family"))
        self.assertIn("tg://user?id=100", self.last().text)
        self.assertNotIn("User_100</a> 👑 🟢", self.last().text)
        self.assertNotIn("tg://user?id=200", self.last().text)
        await friend.click(self.last(), Btn("Members"))
        self.assertIn("Alice", self.buttons())
        self.assertIn("🟢 Bob", self.buttons())

        await owner.send("/start")
        await owner.click(self.last(), Btn("Family"))
        self.assertIn("Bob</a> 🟢", self.last().text)
        self.assertNotIn("tg://user?id=200", self.last().text)
        await owner.click(self.last(), Btn("◀️ Back"))
        await owner.click(self.last(), Btn("Settings"))
        await owner.click(self.last(), Btn("Publish my Telegram: on"))
        await friend.send("/start")
        await friend.click(self.last(), Btn("Family"))
        self.assertNotIn("tg://user?id=100", self.last().text)

    async def test_owner_toggles_alerts_and_hands_over_on_logout(self):
        client = self.client(OWNER)
        await client.send("/start")
        await client.click(self.last(), Btn("Settings"))
        await client.click(self.last(), Btn("New member alerts: on"))
        self.assertFalse(await self.db.notify_requests(OWNER))
        self.assertIn("New member alerts: off", self.buttons())
        self.families.bot = AsyncMock()
        await client.click(self.last(), Btn("Log out"))
        self.assertIn("a random other family member", self.last().text)
        with patch("bot.family.random.choice", lambda items: items[0]):
            await client.click(self.last(), Btn("Yes, log out"))
        self.assertEqual((await self.db.account(FRIEND))["owner_telegram_id"], FRIEND)
        self.assertEqual(self.families.bot.send_message.await_args.args[0], FRIEND)
        self.assertIn("You are now the owner", self.families.bot.send_message.await_args.args[1])

    async def test_failed_steam_logout_keeps_account_and_confirmation_for_retry(self):
        client = self.client(FRIEND)
        await client.send("/start")
        await client.click(self.last(), Btn("Settings"))
        await client.click(self.last(), Btn("Log out"))
        self.revoke.side_effect = RuntimeError("Steam unavailable")
        await client.click(self.last(), Btn("Yes, log out"))
        self.assertEqual(self.db.refresh_token(await self.db.account(FRIEND)), "token")
        self.assertIn("Yes, log out", self.buttons())

    async def test_action_auth_refusal_drops_only_the_acting_session(self):
        client = self.client(OWNER)
        self.families.owner_token = AsyncMock(return_value="token")
        await client.send("/start")
        await client.click(self.last(), Btn("Family"))
        await client.click(self.last(), Btn("Manage family"))
        with patch("bot.dialogs.family.rename_family", AsyncMock(side_effect=TokenRejected("revoked"))):
            await client.click(self.last(), Btn("✏️ Rename family"))
            await client.send("New name")
        self.assertIsNone(self.db.refresh_token(await self.db.account(OWNER)))
        self.assertEqual(self.db.refresh_token(await self.db.account(FRIEND)), "token")

    async def test_family_overview_stats_and_member_card(self):
        await self.db.set_publication(
            OWNER,
            ALICE,
            FAMILY,
            "publish_presence",
            True,
            expected_version=(await self.db.account(OWNER))["publication_version"],
        )
        await self.db.set_publication(
            OWNER,
            ALICE,
            FAMILY,
            "publish_telegram",
            True,
            expected_version=(await self.db.account(OWNER))["publication_version"],
        )
        await self.db.set_publication(
            FRIEND,
            BOB,
            FAMILY,
            "publish_telegram",
            True,
            expected_version=(await self.db.account(FRIEND))["publication_version"],
        )
        with self.assertRaises(SteamAlreadyLinked):
            await self.families.register(NEWCOMER, "duplicate", ALICE)
        client = self.client(FRIEND)
        await client.send("/start")
        shown = []
        show = self.messages.show_message

        async def record(bot, new_message, old_message):
            shown.append(new_message)
            return await show(bot, new_message, old_message)

        self.messages.show_message = record
        await client.click(self.last(), Btn("Family"))
        del self.messages.show_message
        text = self.last().text
        self.assertIn(
            '<a href="https://steamcommunity.com/id/alice/">Alice</a> · <a href="https://t.me/alice_tg">'
            "@alice_tg</a> 👑 🟢",
            text,
        )  # Steam and Telegram
        self.assertIn(
            '<a href="https://steamcommunity.com/profiles/' + BOB + '/">Bob</a> · <a href="tg://user?id=200">'
            "User_200</a>",
            text,
        )  # Telegram name refreshed from the user's latest message
        self.assertNotIn("tg://user?id=300", text)  # no permission from the pending member
        self.assertIn("<b>Fam</b> · 3 of 6", text)
        self.assertNotIn("Free spots", text)
        self.assertTrue(shown[-1].link_preview_options.is_disabled)  # no profile picture previews
        self.assertNotIn("Manage family", self.buttons())  # owner only

        await client.click(self.last(), Btn("📊 Stats"))
        self.assertIn("Games: <b>20</b>", self.last().text)
        await client.click(self.last(), Btn("Games per member"))
        self.assertIsNotNone(self.last().photo)
        self.assertIn("How many family games each member owns", self.last().caption)
        await client.click(self.last(), Btn("◀️ Back"))
        await client.click(self.last(), Btn("Most played borrowed"))
        self.assertIsNotNone(self.last().photo)
        await client.click(self.last(), Btn("◀️ Back"))
        await client.click(self.last(), Btn("◀️ Back"))

        self.families.member_stats = AsyncMock(return_value={"level": 102, "games": 807, "hours": 5000})
        await client.click(self.last(), Btn("Members"))
        await client.click(self.last(), Btn("🟢 Alice"))
        card = self.last().text
        self.assertIn('<a href="https://t.me/alice_tg">@alice_tg</a>', card)
        self.assertIn("Level: 102", card)
        self.assertIn("Games brought: 20 (only theirs: 14)", card)
        self.assertNotIn("Remove from family", self.buttons())  # owner only
        await client.click(self.last(), Btn("Games we both have"))
        self.assertIn("Games: <b>6</b>", self.last().text)  # 3, 6, ..., 18
        self.assertIn("Everyone has it: Bob, Alice", self.last().text)

    async def test_owner_manages_the_steam_family(self):
        self.families.owner_token = AsyncMock(return_value="token")
        self.families.member_stats = AsyncMock(return_value={})
        client = self.client(OWNER)
        await client.send("/start")
        await client.click(self.last(), Btn("Family"))
        await client.click(self.last(), Btn("Manage family"))
        self.assertEqual(
            self.buttons()[:5],
            [
                "Bot members (1 pending)",
                "➕ Invite member",
                "Pending invites (1)",
                "✏️ Rename family",
                "🛒 Purchase requests",
            ],
        )
        with patch("bot.dialogs.family.rename_family", AsyncMock()) as rename:
            await client.click(self.last(), Btn("✏️ Rename family"))
            await client.send("x" * 65)
            self.assertIn("1 to 64 characters", self.last().text)
            await client.send("New name")
        rename.assert_awaited_once_with(None, "token", FAMILY, "New name")
        self.assertIn("✅ Done", self.last().text)

        with (
            patch("bot.dialogs.family.resolve_profile", AsyncMock(return_value="76561198000000042")),
            patch(
                "bot.dialogs.family.invite_member", AsyncMock(side_effect=SteamActionError("No free spots"))
            ) as invite,
        ):
            await client.click(self.last(), Btn("➕ Invite member"))
            await client.send("https://steamcommunity.com/id/someone")
            await client.click(self.last(), Btn("Child"))
        invite.assert_awaited_once_with(None, "token", FAMILY, "76561198000000042", 2)
        self.assertIn("❌ Steam refused: No free spots", self.last().text)

        with patch("bot.dialogs.family.remove_member", AsyncMock()) as remove:
            await client.click(self.last(), Btn("◀️ Back"))
            await client.click(self.last(), Btn("Members"))
            await client.click(self.last(), Btn("Bob"))
            await client.click(self.last(), Btn("Remove from family"))
            self.assertIn("cooldown", self.last().text)
            await client.click(self.last(), Btn("Yes, remove"))
        remove.assert_awaited_once_with(None, "token", FAMILY, BOB)


if __name__ == "__main__":
    unittest.main()
