"""Game-night input and group interactions, with real storage and no external API calls."""

import unittest
from datetime import UTC, datetime
from itertools import count
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from aiogram_dialog.test_tools import BotClient, MockMessageManager
from aiogram_dialog.test_tools.bot_client import FakeBot
from aiogram_dialog.test_tools.memory_storage import JsonMemoryStorage

from bot.__main__ import create_dispatcher, router
from bot.family import Families
from bot.game_night import parse_proposal, render_event
from bot.game_night_store import GameNightStore
from tests.pg import fresh_db, needs_pg

ALICE, BOB, CAROL = "76561198000000001", "76561198000000002", "76561198000000003"
OWNER, FRIEND, PENDING, OUTSIDER = 100, 200, 300, 400
FAMILY, GROUP = "9", -500


class Telegram(FakeBot):
    """Keep Telegram message payloads in memory; no HTTP session exists."""

    def __init__(self):
        self.messages = {}
        self.sent = []
        self.answers = []
        self.ids = count(1000)
        self.fail_edit = None

    async def __call__(self, method, request_timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method)
            return True
        if isinstance(method, SendMessage):
            message_id = next(self.ids)
        elif isinstance(method, EditMessageText):
            if self.fail_edit:
                error, self.fail_edit = self.fail_edit, None
                raise error(method=method, message="Temporary delivery failure")
            message_id = method.message_id
            old = self.messages[(method.chat_id, message_id)]
            if old.text == method.text and old.reply_markup == method.reply_markup:
                raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        else:
            raise AssertionError(f"Unexpected Telegram request: {method}")
        message = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=method.chat_id, type="group"),
            from_user=User(id=self.id, is_bot=True, first_name="Bot"),
            text=method.text,
            reply_markup=method.reply_markup,
        ).as_(self)
        self.messages[(method.chat_id, message_id)] = message
        self.sent.append(message)
        return message


class GameNightInputTest(unittest.TestCase):
    def test_local_time_is_stored_as_an_aware_instant(self):
        starts_at, timezone, appids = parse_proposal(
            "2099-01-02 19:30 Asia/Ho_Chi_Minh 570 730", now=datetime(2030, 1, 1, tzinfo=UTC)
        )
        self.assertEqual(starts_at.astimezone(UTC), datetime(2099, 1, 2, 12, 30, tzinfo=UTC))
        self.assertEqual(timezone, "Asia/Ho_Chi_Minh")
        self.assertEqual(appids, [570, 730])

    def test_rejects_invalid_dates_zones_and_shortlists(self):
        for arguments in (
            "",
            "2099-02-30 19:30 UTC 570 730",
            "2099-01-02 25:00 UTC 570 730",
            "2099-01-02 19:30 Not/A_Zone 570 730",
            "2099-01-02 19:30 UTC 570",
            "2099-01-02 19:30 UTC 1 2 3 4 5 6",
            "2099-01-02 19:30 UTC 570 570",
            "2099-01-02 19:30 UTC 0 730",
            "2099-01-02 19:30 UTC -570 730",
            "2099-01-02 19:30 UTC nope 730",
            "2029-01-02 19:30 UTC 570 730",
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                parse_proposal(arguments, now=datetime(2030, 1, 1, tzinfo=UTC))

    def test_rejects_nonexistent_and_ambiguous_dst_times(self):
        for local_time in ("2030-03-31 02:30", "2030-10-27 02:30"):
            with self.subTest(local_time=local_time), self.assertRaises(ValueError):
                parse_proposal(f"{local_time} Europe/Berlin 570 730", now=datetime(2030, 1, 1, tzinfo=UTC))


class GameNightRenderTest(unittest.TestCase):
    def event(self):
        return {
            "id": 42,
            "family_id": "9",
            "creator_telegram_id": 100,
            "creator_steam_id": ALICE,
            "chat_id": -500,
            "message_id": 50,
            "starts_at": datetime(2099, 1, 2, 12, 30, tzinfo=UTC),
            "timezone": "Asia/Ho_Chi_Minh",
            "appids": [570, 730],
            "state": "open",
            "chosen_appid": None,
            "snapshot": {
                "members": [ALICE, BOB, CAROL],
                "names": {ALICE: "Alice <host>", BOB: "Bob & Co", CAROL: "Carol"},
                "apps": [
                    {"appid": 570, "name": "Dota <2>", "owners": [ALICE], "exclude_reason": 0},
                    {"appid": 730, "name": "Counter & Strike", "owners": [ALICE, BOB], "exclude_reason": 0},
                ],
            },
            "participants": [],
        }

    def test_card_escapes_names_and_offers_shortlist_votes_and_rsvps(self):
        text, keyboard = render_event(self.event(), "en")
        self.assertIn("2099-01-02", text)
        self.assertIn("19:30", text)
        self.assertIn("Asia/Ho_Chi_Minh", text)
        self.assertIn("Dota &lt;2&gt;", text)
        self.assertIn("Counter &amp; Strike", text)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(
            callbacks,
            [
                "gn:42:vote:570",
                "gn:42:vote:730",
                "gn:42:rsvp:going",
                "gn:42:rsvp:maybe",
                "gn:42:rsvp:cannot",
                "gn:42:close",
                "gn:42:cancel",
            ],
        )
        self.assertNotIn("tg://", text)
        self.assertNotIn("https://t.me/", text)
        self.assertNotIn("Chosen game:", text)

    def test_going_count_controls_required_copies_and_shortage(self):
        event = self.event()
        event["chosen_appid"] = 570
        event["participants"] = [
            {"telegram_id": 100, "steam_id": ALICE, "vote": 570, "rsvp": "going"},
            {"telegram_id": 200, "steam_id": BOB, "vote": 570, "rsvp": "going"},
            {"telegram_id": 300, "steam_id": CAROL, "vote": 730, "rsvp": "maybe"},
        ]
        text, _ = render_event(event, "en")
        self.assertIn("Required copies: 2", text)
        self.assertIn("Eligible copies: 1", text)
        self.assertIn("not enough", text.lower())
        self.assertIn("Alice &lt;host&gt;", text)
        self.assertIn("Bob &amp; Co", text)

    def test_departed_owners_are_omitted_and_excluded_sharing_is_flagged(self):
        event = self.event()
        event["chosen_appid"] = 730
        event["snapshot"]["members"] = [ALICE, CAROL]
        event["participants"] = [{"telegram_id": 100, "steam_id": ALICE, "vote": 730, "rsvp": "going"}]
        text, _ = render_event(event, "en")
        self.assertIn("Eligible copies: 1", text)
        event["snapshot"]["apps"][1]["exclude_reason"] = 1
        text, _ = render_event(event, "en")
        self.assertIn("Eligible copies: 1", text)
        self.assertIn("not shareable", text.lower())

    def test_unknown_sharing_and_availability_are_not_promised(self):
        event = self.event()
        event["chosen_appid"] = 570
        event["participants"] = [{"telegram_id": 100, "steam_id": ALICE, "vote": 570, "rsvp": "going"}]
        del event["snapshot"]["apps"][0]["exclude_reason"]
        text, _ = render_event(event, "en")
        self.assertIn("unknown", text.lower())
        self.assertIn("availability", text.lower())
        self.assertIn("sharing", text.lower())

    def test_closed_and_cancelled_cards_have_no_action_buttons(self):
        for state in ("closed", "cancelled"):
            with self.subTest(state=state):
                event = self.event()
                event["state"] = state
                event["chosen_appid"] = 730 if state == "closed" else None
                text, keyboard = render_event(event, "en")
                self.assertIsNone(keyboard)
                self.assertIn(state, text.lower())
        russian, _ = render_event(self.event(), "ru")
        self.assertIn("2099-01-02", russian)
        self.assertNotIn("Required copies:", russian)


@needs_pg
class GameNightGroupTest(unittest.IsolatedAsyncioTestCase):
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
        self.store = GameNightStore(self.db)
        self.telegram = Telegram()
        self.families = Families(self.db, http=None, bot=self.telegram)
        self.families.refresh = AsyncMock(side_effect=AssertionError("Steam must not be called"))
        self.dp.workflow_data.update(db=self.db, families=self.families)
        self.updates = count(1)
        await self.db.ensure_family(FAMILY, "Fam")
        for telegram_id, steam_id, status in (
            (OWNER, ALICE, "approved"),
            (FRIEND, BOB, "approved"),
            (PENDING, CAROL, "pending"),
        ):
            await self.db.save_account(telegram_id, steam_id, FAMILY, "token")
            await self.db.set_status(telegram_id, status)
            await self.db.set_language(telegram_id, "en")
        self.snapshot = {
            "family_id": FAMILY,
            "name": "Fam",
            "members": [ALICE, BOB, CAROL],
            "names": {ALICE: "Alice", BOB: "Bob", CAROL: "Carol"},
            "apps": [
                {"appid": 570, "name": "Dota 2", "owners": [ALICE], "exclude_reason": 0},
                {"appid": 730, "name": "Counter-Strike", "owners": [ALICE, BOB], "exclude_reason": 0},
            ],
        }
        self.families.get(FAMILY).snapshot = self.snapshot
        await self.db.update_family(
            FAMILY, notify_chat=GROUP, language="en", owner_telegram_id=OWNER, state={"observed": self.snapshot}
        )

    async def asyncTearDown(self):
        await self.db.pool.close()

    def client(self, telegram_id=OWNER, chat_id=GROUP, chat_type="group"):
        return BotClient(self.dp, user_id=telegram_id, chat_id=chat_id, chat_type=chat_type, bot=self.telegram)

    async def propose(self, telegram_id=OWNER):
        await self.client(telegram_id).send("/gamenight 2099-01-02 19:30 Asia/Ho_Chi_Minh 570 730")
        message = self.telegram.sent[-1]
        self.assertIsNotNone(message.reply_markup, message.text)
        event_id = int(message.reply_markup.inline_keyboard[0][0].callback_data.split(":")[1])
        event = await self.store.get(event_id, GROUP)
        self.assertEqual(event["message_id"], message.message_id)
        return event_id, message

    async def click(self, telegram_id, message, event_id, action, value=None, *, raw_data=None):
        data = (
            raw_data if raw_data is not None else f"gn:{event_id}:{action}" + (f":{value}" if value is not None else "")
        )
        number = next(self.updates)
        await self.dp.feed_update(
            self.telegram,
            Update(
                update_id=number,
                callback_query=CallbackQuery(
                    id=f"callback-{number}",
                    from_user=User(id=telegram_id, is_bot=False, first_name=f"User {telegram_id}"),
                    chat_instance="group",
                    message=message,
                    data=data,
                ),
            ),
        )

    async def test_help_and_invalid_input_do_not_create_events(self):
        await self.client().send("/gamenight")
        self.assertIn("/gamenight", self.telegram.sent[-1].text)
        for arguments in ("nonsense", "2099-01-02 19:30 UTC 570 999", "2020-01-02 19:30 UTC 570 730"):
            with self.subTest(arguments=arguments):
                await self.client().send(f"/gamenight {arguments}")
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_nights"), 0)

    async def test_only_current_members_can_propose_in_the_linked_group(self):
        for telegram_id, chat_id, chat_type in (
            (PENDING, GROUP, "group"),
            (OUTSIDER, GROUP, "group"),
            (OWNER, -900, "group"),
            (OWNER, OWNER, "private"),
        ):
            with self.subTest(telegram_id=telegram_id, chat_id=chat_id):
                await self.client(telegram_id, chat_id, chat_type).send("/gamenight 2099-01-02 19:30 UTC 570 730")
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_nights"), 0)
        event_id, _ = await self.propose(FRIEND)
        self.assertEqual((await self.store.get(event_id, GROUP))["creator_telegram_id"], FRIEND)

    async def test_vote_and_rsvp_replacement_are_idempotent_and_close_uses_shortlist_tie_order(self):
        event_id, message = await self.propose()
        await self.click(OWNER, message, event_id, "vote", 570)
        await self.click(OWNER, message, event_id, "vote", 570)
        await self.click(OWNER, message, event_id, "rsvp", "maybe")
        await self.click(OWNER, message, event_id, "rsvp", "going")
        await self.click(FRIEND, message, event_id, "vote", 730)
        await self.click(OWNER, message, event_id, "vote", 730)
        self.assertEqual((await self.store.get(event_id, GROUP))["chosen_appid"], 730)
        await self.click(OWNER, message, event_id, "vote", 570)
        self.assertIn("Required copies: 1", self.telegram.messages[(GROUP, message.message_id)].text)
        await self.click(OWNER, message, event_id, "rsvp", "cannot")
        self.assertIn("Required copies: 0", self.telegram.messages[(GROUP, message.message_id)].text)
        await self.click(OWNER, message, event_id, "rsvp", "going")
        event = await self.store.get(event_id, GROUP)
        self.assertEqual(len(event["participants"]), 2)
        alice = next(participant for participant in event["participants"] if participant["telegram_id"] == OWNER)
        self.assertEqual((alice["vote"], alice["rsvp"]), (570, "going"))
        self.assertEqual(event["chosen_appid"], 570)
        await self.click(FRIEND, message, event_id, "close")
        self.assertEqual((await self.store.get(event_id, GROUP))["state"], "open")
        await self.click(OWNER, message, event_id, "close")
        self.assertEqual((await self.store.get(event_id, GROUP))["state"], "closed")
        self.assertIsNone(self.telegram.messages[(GROUP, message.message_id)].reply_markup)
        await self.click(FRIEND, message, event_id, "vote", 570)
        self.assertEqual((await self.store.get(event_id, GROUP))["chosen_appid"], 570)
        self.assertEqual(len(self.telegram.answers), 12)

    async def test_closing_without_votes_keeps_the_poll_open(self):
        event_id, message = await self.propose()
        await self.click(OWNER, message, event_id, "close")
        event = await self.store.get(event_id, GROUP)
        self.assertEqual(event["state"], "open")
        self.assertIsNone(event["chosen_appid"])
        self.assertIsNotNone(self.telegram.messages[(GROUP, message.message_id)].reply_markup)

    async def test_unchanged_telegram_card_still_acknowledges_repeated_click(self):
        event_id, message = await self.propose()
        await self.click(OWNER, message, event_id, "vote", 570)
        await self.click(OWNER, message, event_id, "vote", 570)
        event = await self.store.get(event_id, GROUP)
        self.assertEqual(len(event["participants"]), 1)
        self.assertEqual(event["participants"][0]["vote"], 570)
        self.assertEqual(len(self.telegram.answers), 2)

    async def test_malformed_callbacks_are_acknowledged_without_changing_the_poll(self):
        event_id, message = await self.propose()
        for data in ("gn:", "gn:notint:vote:570", f"gn:{event_id}:close:extra"):
            with self.subTest(data=data):
                await self.click(OWNER, message, event_id, "close", raw_data=data)
        event = await self.store.get(event_id, GROUP)
        self.assertEqual(event["state"], "open")
        self.assertEqual(event["participants"], [])
        self.assertEqual(len(self.telegram.answers), 3)

    async def test_creator_alone_can_cancel_and_cancellation_is_final(self):
        event_id, message = await self.propose(FRIEND)
        await self.click(OWNER, message, event_id, "cancel")
        self.assertEqual((await self.store.get(event_id, GROUP))["state"], "open")
        await self.click(FRIEND, message, event_id, "cancel")
        self.assertEqual((await self.store.get(event_id, GROUP))["state"], "cancelled")
        self.assertIsNone(self.telegram.messages[(GROUP, message.message_id)].reply_markup)
        await self.click(FRIEND, message, event_id, "rsvp", "going")
        self.assertEqual((await self.store.get(event_id, GROUP))["participants"], [])

    async def test_finalized_card_can_be_repaired_after_a_telegram_edit_failure(self):
        for action, state, error in (
            ("close", "closed", TelegramNetworkError),
            ("cancel", "cancelled", TelegramBadRequest),
        ):
            with self.subTest(action=action):
                event_id, message = await self.propose()
                await self.click(OWNER, message, event_id, "vote", 570)
                await self.click(FRIEND, message, event_id, "rsvp", "going")
                participants = (await self.store.get(event_id, GROUP))["participants"]
                self.telegram.fail_edit = error
                with self.assertRaises(error):
                    await self.click(OWNER, message, event_id, action)
                finalized = await self.store.get(event_id, GROUP)
                self.assertEqual(finalized["state"], state)
                self.assertEqual(finalized["participants"], participants)
                self.assertIsNotNone(self.telegram.messages[(GROUP, message.message_id)].reply_markup)

                await self.click(OWNER, message, event_id, action)
                self.assertIsNone(self.telegram.messages[(GROUP, message.message_id)].reply_markup)
                self.assertIn(state, self.telegram.messages[(GROUP, message.message_id)].text.lower())
                await self.click(FRIEND, message, event_id, "vote", 730)
                retried = await self.store.get(event_id, GROUP)
                self.assertEqual(retried["state"], state)
                self.assertEqual(retried["participants"], participants)
                self.assertEqual(retried["chosen_appid"], finalized["chosen_appid"])

    async def test_pending_unknown_users_and_forged_message_buttons_cannot_vote(self):
        event_id, message = await self.propose()
        for telegram_id in (PENDING, OUTSIDER):
            await self.click(telegram_id, message, event_id, "vote", 570)
        wrong_chat = message.model_copy(update={"chat": Chat(id=-900, type="group")})
        wrong_message = message.model_copy(update={"message_id": message.message_id + 1})
        for forged in (wrong_chat, wrong_message):
            await self.click(FRIEND, forged, event_id, "rsvp", "going")
        await self.click(FRIEND, message, event_id, "vote", 999)
        self.assertEqual((await self.store.get(event_id, GROUP))["participants"], [])

    async def test_revoked_approval_and_latest_steam_membership_remove_existing_participation(self):
        event_id, message = await self.propose()
        await self.click(FRIEND, message, event_id, "vote", 730)
        await self.click(FRIEND, message, event_id, "rsvp", "going")
        await self.db.set_status(FRIEND, "blocked")
        await self.click(FRIEND, message, event_id, "vote", 570)
        self.assertEqual((await self.store.get(event_id, GROUP))["participants"], [])
        await self.db.set_status(FRIEND, "approved")
        self.snapshot["members"].remove(BOB)
        await self.db.update_family(FAMILY, state={"observed": self.snapshot})
        await self.click(FRIEND, message, event_id, "rsvp", "maybe")
        self.assertEqual((await self.store.get(event_id, GROUP))["participants"], [])

    async def test_unlink_and_relink_do_not_reactivate_old_event_buttons(self):
        event_id, message = await self.propose()
        await self.db.unlink_chat(GROUP)
        await self.click(FRIEND, message, event_id, "vote", 570)
        self.assertIsNone(await self.store.get(event_id, GROUP))
        await self.db.update_family(FAMILY, notify_chat=GROUP)
        await self.click(FRIEND, message, event_id, "rsvp", "going")
        self.assertIsNone(await self.store.get(event_id, GROUP))
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_night_participants"), 0)
