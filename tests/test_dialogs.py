"""Clicks through the real dialogs with aiogram-dialog's test client, backed by a test Postgres (no Steam)."""

import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    InlineQuery,
    Update,
    User,
)
from aiogram_dialog.test_tools import BotClient, MockMessageManager
from aiogram_dialog.test_tools.keyboard import InlineButtonTextLocator as Btn
from aiogram_dialog.test_tools.memory_storage import JsonMemoryStorage

from bot.__main__ import create_dispatcher
from bot.db import SteamAlreadyLinked
from bot.family import Families
from bot.login import qr_png
from bot.steam import SteamActionError
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


@needs_pg
class DialogFlowTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = MockMessageManager()
        cls.logins = StubLogins()
        cls.dp = create_dispatcher(None, None, cls.logins, storage=JsonMemoryStorage(), message_manager=cls.messages)

    async def asyncSetUp(self):
        self.db = await fresh_db()
        self.families = Families(self.db, http=None, bot=None)
        self.families.refresh = AsyncMock()  # never call Steam from tests
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
                    "multiplayer": i % 3 == 0,
                }
                for i in range(1, 21)
            },
        }
        self.messages.reset_history()

    async def asyncTearDown(self):
        await self.db.pool.close()

    def last(self):
        return self.messages.last_message()

    def buttons(self):
        return [b.text for row in self.last().reply_markup.inline_keyboard for b in row]

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
        self.assertIn("«Через мобильное приложение»", self.last().caption)
        self.assertIn("город, где стоит сервер бота", self.last().caption)  # LOGIN_LOCATION not set

    async def test_newcomer_logs_in_from_qr_screen(self):
        client = self.client(NEWCOMER)
        await client.send("/start")
        self.assertIn("Log in with your Steam account", self.last().text)
        self.assertEqual(self.buttons(), ["Log in with Steam", "Settings"])
        with patch("bot.dialogs.menu.LOGIN_LOCATION", "Vienna, Wien, AT"):
            await client.click(self.last(), Btn("Log in with Steam"))
        self.assertEqual(self.logins.started[-1], NEWCOMER)
        login = self.last()
        self.assertIsNotNone(login.photo)  # the QR is part of the dialog message
        self.assertIn("mobile app", login.caption)
        self.assertIn("Vienna, Wien, AT", login.caption)
        self.assertEqual(login.reply_markup.inline_keyboard[0][0].url, "https://s.team/q/1/42")
        await client.click(login, Btn("Cancel"))
        self.assertEqual(self.logins.cancelled[-1], NEWCOMER)
        self.assertIn("Log in with Steam", self.buttons())

    async def test_pending_member_waits(self):
        await self.client(PENDING).send("/start")
        self.assertIn("Waiting for the family owner", self.last().text)
        self.assertEqual(self.buttons(), ["Settings"])

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
        self.assertEqual(self.buttons(), ["Log in with Steam", "Settings"])

    async def test_revoked_owner_cannot_submit_old_family_action(self):
        self.families.owner_token = AsyncMock(return_value="token")
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn("✏️ Rename family"))
        await self.fetch_members([BOB, CAROL])

        with patch("bot.dialogs.family.rename_family", AsyncMock()) as rename:
            await client.send("Unauthorized name")
        rename.assert_not_awaited()
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

    async def test_library_pagination_search_filters_and_game(self):
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
        self.assertNotIn("Admin", self.buttons())  # merged into family management
        await client.click(self.last(), Btn("Family"))
        await client.click(self.last(), Btn("Manage family"))

    async def test_owner_approves_and_blocks_from_family_management(self):
        self.families.member_stats = AsyncMock(return_value={})
        client = self.client(OWNER)
        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(1 pending\)"))
        self.assertEqual(self.buttons()[:2], ["✅ Bob", "⏳ Carol"])  # the owner is not listed

        await client.click(self.last(), Btn("⏳ Carol"))
        await client.click(self.last(), Btn("Approve"))
        self.assertEqual((await self.db.account(PENDING))["status"], "approved")
        await self.client(PENDING).send("/start")
        self.assertIn("Family library", self.buttons())

        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(0 pending\)"))
        await client.click(self.last(), Btn("✅ Bob"))
        await client.click(self.last(), Btn("Block"))
        self.assertEqual((await self.db.account(FRIEND))["status"], "blocked")
        await self.client(FRIEND).send("/start")
        self.assertIn("blocked", self.last().text)

        await self.open_bot_members(client)
        await client.click(self.last(), Btn(r"Bot members \(0 pending\)"))
        await client.click(self.last(), Btn("◀️ Back"))
        self.assertIn("Manage family", self.last().text)  # back where it was opened

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
                await self.families.send_alert(await self.db.family(FAMILY), "Before removal")
                self.families.bot.send_message.assert_awaited_once_with(-800, "Before removal", parse_mode=None)
                self.families.bot.reset_mock()

                await self.membership(FRIEND, -800, "member", status)

                row = await self.db.family(FAMILY)
                self.assertIsNone(row["notify_chat"])
                await self.families.send_alert(row, "After removal")
                self.families.bot.send_message.assert_not_awaited()

    async def test_removal_from_old_group_keeps_the_new_group_linked(self):
        self.families.bot = AsyncMock()
        await self.membership(OWNER, -800, "left", "member")
        await self.membership(OWNER, -900, "left", "member")
        self.families.bot.reset_mock()

        await self.membership(FRIEND, -800, "member", "kicked")

        row = await self.db.family(FAMILY)
        self.assertEqual(row["notify_chat"], -900)
        await self.families.send_alert(row, "New group alert")
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
        self.assertEqual(answer.await_args.kwargs, {"cache_time": 60, "is_personal": True})

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
        client = self.client(FRIEND)
        await client.send("/start")
        self.assertEqual(self.buttons(), ["Family library", "Family", "Settings"])
        self.assertEqual(self.button("Settings").icon_custom_emoji_id, "5341715473882955310")
        await client.click(self.last(), Btn("Settings"))
        self.assertEqual(self.buttons(), ["Language", "Log out", "◀️ Back"])  # alerts toggle is for owners
        await client.click(self.last(), Btn("Language"))
        await client.click(self.last(), Btn("🇷🇺 Русский"))
        self.assertIn("Настройки", self.last().text)  # back in settings, now in Russian
        await client.click(self.last(), Btn("Выйти"))
        self.assertNotIn("👑", self.last().text)
        await client.click(self.last(), Btn("Да, выйти"))
        self.assertIsNone(await self.db.account(FRIEND))
        self.assertIsNone(await self.db.language(FRIEND))  # settings removed
        self.assertIn("Choose the bot language", self.last().text)  # starts over

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

    async def test_family_overview_stats_and_member_card(self):
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
        self.assertIn('<a href="tg://user?id=300">Telegram</a> (child)', text)  # no Telegram name saved yet
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
