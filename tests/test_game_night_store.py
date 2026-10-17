import asyncio
import unittest
from datetime import UTC, datetime

from cryptography.fernet import Fernet

from bot.db import Db
from bot.game_night_store import GameNightStore
from bot.migrations import MIGRATIONS, migrate
from tests.pg import TEST_DATABASE_URL, fresh_db, needs_pg

START = datetime(2099, 1, 2, 18, tzinfo=UTC)
SNAPSHOT = {
    "members": ["1", "2", "3", "4"],
    "names": {"1": "Alice", "2": "Bob"},
    "apps": [{"appid": 10, "name": "One", "owners": ["1"]}, {"appid": 20, "name": "Two", "owners": ["2"]}],
}


@needs_pg
class GameNightStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        self.store = GameNightStore(self.db)
        for family_id, chat_id in (("9", -100), ("8", -200)):
            await self.db.ensure_family(family_id, "Family")
            await self.db.update_family(family_id, notify_chat=chat_id, state={"observed": SNAPSHOT})
        for telegram_id, steam_id, family_id, status in (
            (100, "1", "9", "approved"),
            (200, "2", "9", "approved"),
            (300, "3", "9", "pending"),
            (400, "4", "8", "approved"),
        ):
            await self.db.save_account(telegram_id, steam_id, family_id, "token")
            await self.db.set_status(telegram_id, status)

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def create(self, **changes):
        return await self.store.create(
            **{
                "family_id": "9",
                "telegram_id": 100,
                "steam_id": "1",
                "chat_id": -100,
                "starts_at": START,
                "timezone": "Europe/Moscow",
                "appids": [10, 20],
                **changes,
            }
        )

    async def event(self):
        event = await self.create()
        await self.store.attach(event["id"], 77)
        return event["id"]

    async def act(self, event_id, action, value=None, **changes):
        return await self.store.act(
            **{
                "event_id": event_id,
                "telegram_id": 200,
                "steam_id": "2",
                "chat_id": -100,
                "message_id": 77,
                "action": action,
                "value": value,
                **changes,
            }
        )

    async def test_creation_validates_member_chat_shortlist_and_time(self):
        for changes in (
            {"family_id": "missing"},
            {"telegram_id": 999},
            {"telegram_id": 300, "steam_id": "3"},
            {"telegram_id": 400, "steam_id": "4"},
            {"steam_id": "2"},
            {"chat_id": -200},
            {"appids": []},
            {"appids": [10]},
            {"appids": [10, 20, 30, 40, 50, 60]},
            {"appids": [10, 10]},
            {"appids": [10, 99]},
            {"appids": [10, True]},
            {"appids": [10, -20]},
            {"appids": [10, 2**63]},
            {"starts_at": START.replace(tzinfo=None)},
            {"starts_at": datetime(2000, 1, 1, tzinfo=UTC)},
            {"timezone": "Invalid/Timezone"},
            {"timezone": "../Europe/Moscow"},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(await self.create(**changes))
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_nights"), 0)
        event = await self.create()
        self.assertEqual((event["starts_at"], event["timezone"], event["appids"]), (START, "Europe/Moscow", [10, 20]))
        self.assertEqual(event["snapshot"], SNAPSHOT)
        self.assertEqual(event["participants"], [])
        self.assertIsNone(event["chosen_appid"])

    async def test_historical_membership_does_not_authorize_creation(self):
        await self.db.update_family("9", state={"members": ["1"], "observed": {**SNAPSHOT, "members": ["2"]}})
        self.assertIsNone(await self.create())
        await self.db.update_family("9", state={"members": ["1"], "apps": SNAPSHOT["apps"]})
        self.assertIsNone(await self.create())
        await self.db.update_family("9", state=None)
        self.assertIsNone(await self.create())

    async def test_repeated_and_concurrent_votes_and_rsvps_have_one_row_per_member(self):
        event_id = await self.event()
        results = await asyncio.gather(
            *(self.act(event_id, "vote", 20) for _ in range(8)),
            *(self.act(event_id, "rsvp", "going") for _ in range(8)),
        )
        self.assertTrue(all(results))
        result = await self.store.get(event_id, -100)
        self.assertEqual(result["participants"], [{"telegram_id": 200, "steam_id": "2", "vote": 20, "rsvp": "going"}])
        self.assertEqual(result["chosen_appid"], 20)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_night_participants"), 1)
        changed = await self.act(event_id, "vote", 10)
        self.assertEqual(changed["participants"][0]["rsvp"], "going")
        self.assertEqual(changed["chosen_appid"], 10)
        changed = await self.act(event_id, "rsvp", "cannot")
        self.assertEqual(changed["participants"][0]["vote"], 10)

    async def test_tie_uses_shortlist_order_and_rsvp_does_not_cast_a_vote(self):
        event_id = await self.event()
        result = await self.act(event_id, "rsvp", "maybe")
        self.assertIsNone(result["chosen_appid"])
        await self.act(event_id, "vote", 20)
        result = await self.act(event_id, "vote", 10, telegram_id=100, steam_id="1")
        self.assertEqual(result["chosen_appid"], 10)

    async def test_actions_reject_invalid_values_other_groups_and_other_messages(self):
        event_id = await self.event()
        for action, value, changes in (
            ("invalid", None, {}),
            ("rsvp", "yes", {}),
            ("vote", True, {}),
            ("vote", 99, {}),
            ("vote", "10", {}),
            ("vote", 10, {"event_id": event_id + 1}),
            ("vote", 10, {"chat_id": -200}),
            ("vote", 10, {"message_id": 78}),
            ("vote", 10, {"telegram_id": 400, "steam_id": "4"}),
            ("vote", 10, {"telegram_id": 300, "steam_id": "3"}),
            ("vote", 10, {"telegram_id": 999}),
            ("vote", 10, {"steam_id": "1"}),
            ("close", None, {}),
            ("cancel", None, {}),
        ):
            with self.subTest(action=action, value=value, changes=changes):
                supplied_id = changes.pop("event_id", event_id)
                self.assertIsNone(await self.act(supplied_id, action, value, **changes))
        self.assertEqual((await self.store.get(event_id, -100))["participants"], [])
        self.assertIsNone(await self.store.get(event_id, -200))
        self.assertIsNone(await self.store.get(event_id + 1, -100))

    async def test_only_creator_can_close_or_cancel_and_final_events_are_immutable(self):
        for final_action, state in (("close", "closed"), ("cancel", "cancelled")):
            with self.subTest(action=final_action):
                event_id = await self.event()
                await self.act(event_id, "vote", 10)
                result = await self.act(event_id, final_action, telegram_id=100, steam_id="1")
                self.assertEqual(result["state"], state)
                for action, value in (("vote", 10), ("rsvp", "going"), ("close", None), ("cancel", None)):
                    stale = await self.act(event_id, action, value, telegram_id=100, steam_id="1")
                    self.assertEqual(stale, result)
                self.assertIsNone(await self.act(event_id, "vote", 20, message_id=78))
                self.assertIsNone(await self.act(event_id, "vote", 20, chat_id=-200))
                self.assertIsNone(await self.act(event_id, "vote", 20, steam_id="1"))
                self.assertIsNone(await self.act(event_id, "vote", 20, telegram_id=300, steam_id="3"))
                self.assertEqual((await self.store.get(event_id, -100))["state"], state)

    async def test_blocked_logged_out_and_departed_members_lose_access_and_tally(self):
        event_id = await self.event()
        await self.act(event_id, "vote", 20)
        for status in ("blocked", "pending"):
            await self.db.set_status(200, status)
            self.assertIsNone(await self.act(event_id, "rsvp", "going"))
            self.assertIsNone((await self.store.get(event_id, -100))["chosen_appid"])
        await self.db.set_status(200, "approved")
        await self.db.set_refresh_token(200, None)
        self.assertIsNone(await self.act(event_id, "rsvp", "going"))
        self.assertEqual((await self.store.get(event_id, -100))["participants"], [])
        await self.db.save_account(200, "2", "9", "new-token")
        await self.db.update_family("9", state={"members": ["1", "2"], "observed": {**SNAPSHOT, "members": ["1"]}})
        self.assertIsNone(await self.act(event_id, "vote", 10))
        self.assertEqual((await self.store.get(event_id, -100))["participants"], [])

    async def test_replaced_account_and_moved_family_cannot_keep_participation(self):
        event_id = await self.event()
        await self.act(event_id, "vote", 20)
        await self.act(event_id, "rsvp", "going")
        await self.db.save_account(200, "5", "9", "new-account")
        await self.db.set_status(200, "approved")
        await self.db.update_family("9", state={"observed": {**SNAPSHOT, "members": ["1", "5"]}})
        self.assertEqual((await self.store.get(event_id, -100))["participants"], [])
        self.assertIsNone(await self.act(event_id, "vote", 10))
        result = await self.act(event_id, "rsvp", "maybe", steam_id="5")
        self.assertEqual(result["participants"], [{"telegram_id": 200, "steam_id": "5", "vote": None, "rsvp": "maybe"}])
        await self.db.save_account(200, "5", "8", "moved")
        await self.db.set_status(200, "approved")
        self.assertIsNone(await self.act(event_id, "vote", 10, steam_id="5"))
        self.assertEqual((await self.store.get(event_id, -100))["participants"], [])

    async def test_changed_creator_steam_identity_cannot_close_old_event(self):
        event_id = await self.event()
        await self.db.save_account(100, "5", "9", "new-account")
        await self.db.set_status(100, "approved")
        await self.db.update_family("9", state={"observed": {**SNAPSHOT, "members": ["5", "2"]}})
        self.assertIsNone(await self.act(event_id, "close", telegram_id=100, steam_id="5"))

    async def test_unlink_relink_never_reactivates_old_callbacks(self):
        event_id = await self.event()
        await self.db.unlink_chat(-100)
        self.assertIsNone(await self.store.get(event_id, -100))
        self.assertIsNone(await self.act(event_id, "vote", 10))
        await self.db.update_family("9", notify_chat=-100)
        self.assertIsNone(await self.store.get(event_id, -100))
        self.assertIsNone(await self.act(event_id, "close", telegram_id=100, steam_id="1"))
        self.assertIsNotNone(await self.create())

    async def test_live_chat_change_rejects_the_previous_event(self):
        event_id = await self.event()
        await self.db.update_family("9", notify_chat=-300)
        self.assertIsNone(await self.store.get(event_id, -100))
        self.assertIsNone(await self.act(event_id, "vote", 10))

    async def test_deleted_accounts_and_families_cascade_personal_data(self):
        event_id = await self.event()
        await self.act(event_id, "vote", 20)
        await self.db.delete_account(200)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_night_participants"), 0)
        self.assertIsNotNone(await self.store.get(event_id, -100))
        await self.db.delete_account(100)
        self.assertIsNone(await self.store.get(event_id, -100))
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_nights"), 0)
        other = await self.create(family_id="8", telegram_id=400, steam_id="4", chat_id=-200)
        await self.db.pool.execute("DELETE FROM families WHERE family_id = '8'")
        self.assertIsNone(await self.store.get(other["id"], -200))

    async def test_attach_only_binds_once_and_events_survive_pool_restart(self):
        event_id = await self.event()
        await self.act(event_id, "vote", 20)
        await self.act(event_id, "rsvp", "going")
        await self.store.attach(event_id, 99)
        await self.db.pool.close()
        self.db = await Db.connect(TEST_DATABASE_URL, Fernet.generate_key().decode())
        self.store = GameNightStore(self.db)
        event = await self.store.get(event_id, -100)
        self.assertEqual(event["message_id"], 77)
        self.assertEqual(event["chosen_appid"], 20)
        self.assertEqual(event["participants"][0]["rsvp"], "going")

    async def test_close_requires_a_current_voted_game_and_keeps_its_final_choice(self):
        event_id = await self.event()
        self.assertIsNone(await self.act(event_id, "close", telegram_id=100, steam_id="1"))
        await self.act(event_id, "vote", 20)
        await self.db.update_family("9", state={"observed": {**SNAPSHOT, "apps": SNAPSHOT["apps"][:1]}})
        self.assertIsNone(await self.act(event_id, "vote", 20))
        self.assertIsNone(await self.act(event_id, "close", telegram_id=100, steam_id="1"))
        await self.db.update_family("9", state={"observed": SNAPSHOT})
        result = await self.act(event_id, "close", telegram_id=100, steam_id="1")
        self.assertEqual(result["chosen_appid"], 20)
        await self.db.set_status(200, "blocked")
        result = await self.store.get(event_id, -100)
        self.assertEqual(result["participants"], [])
        self.assertEqual(result["chosen_appid"], 20)

    async def test_schema_migration_replay_preserves_events(self):
        event_id = await self.event()
        async with self.db.pool.acquire() as connection:
            await connection.execute(MIGRATIONS[8].read_text())
            await migrate(connection)
        self.assertIsNotNone(await self.store.get(event_id, -100))

    async def test_upgrade_from_released_backlog_schema_preserves_private_data(self):
        async with self.db.pool.acquire() as connection:
            await connection.execute("DROP TABLE game_night_participants, game_nights")
            await connection.execute("DELETE FROM schema_migrations WHERE version >= 9")
            history = await connection.fetch("SELECT * FROM schema_migrations ORDER BY version")
        self.assertEqual(len(history), 8)
        self.assertEqual(history[-1]["name"], "008_game_statuses.sql")
        self.assertTrue(await self.db.set_game_status(100, "1", "9", 10, "One", "playing"))
        marks = await self.db.game_marks(100)
        account = await self.db.account(100)
        family = await self.db.family("9")

        async with self.db.pool.acquire() as connection:
            await migrate(connection)
            upgraded = await connection.fetch("SELECT * FROM schema_migrations ORDER BY version")
        self.assertEqual(upgraded[:8], history)
        self.assertEqual(
            [(row["version"], row["name"]) for row in upgraded[8:]],
            [(9, "009_game_nights.sql"), (10, "010_game_responses.sql")],
        )
        self.assertEqual(await self.db.account(100), account)
        self.assertEqual(await self.db.family("9"), family)
        self.assertEqual(await self.db.game_marks(100), marks)
        event_id = await self.event()
        result = await self.act(event_id, "vote", 20)
        self.assertEqual(result["chosen_appid"], 20)
        self.assertEqual(await self.db.game_marks(100), marks)

    async def test_status_changes_cannot_pass_the_access_check_while_the_action_is_waiting(self):
        event_id = await self.event()
        async with self.db.pool.acquire() as connection, connection.transaction():
            await connection.fetchval("SELECT family_id FROM families WHERE family_id = '9' FOR UPDATE")
            pending = asyncio.create_task(self.act(event_id, "vote", 10))
            await asyncio.sleep(0.03)
            self.assertFalse(pending.done())
            await self.db.set_status(200, "blocked")
        self.assertIsNone(await pending)
        self.assertEqual((await self.store.get(event_id, -100))["participants"], [])
