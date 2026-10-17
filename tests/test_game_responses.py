import asyncio
import unittest
from datetime import timedelta
from unittest.mock import patch

import asyncpg

from bot.migrations import MIGRATIONS, migrate
from tests.pg import fresh_db, needs_pg


@needs_pg
class GameResponsesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await fresh_db()
        await self.db.ensure_family("9", "Family")
        await self.db.ensure_family("8", "Other")
        await self.db.update_family("9", state={"members": ["1", "2"], "ownership": {}})
        self.account = await self.approved(10, "1")
        self.peer = await self.approved(20, "2")

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def approved(self, telegram_id, steam_id, family_id="9"):
        await self.db.save_account(telegram_id, steam_id, family_id, "token")
        await self.db.set_status(telegram_id, "approved")
        return dict(await self.db.account(telegram_id))

    async def test_stale_named_response_cannot_restore_disclosure_after_change_or_clear(self):
        original = dict(self.account)
        self.assertTrue(await self.db.set_game_response(original, 10, "recommend", named=True))
        current = dict(await self.db.account(10))
        self.assertEqual(current["publication_version"], original["publication_version"])
        self.assertNotEqual(current.get("game_response_version"), original.get("game_response_version"))
        self.assertTrue(await self.db.set_game_response(current, 10, "recommend"))
        self.assertFalse(await self.db.set_game_response(original, 10, "recommend", named=True))
        current = dict(await self.db.account(10))
        self.assertFalse((await self.db.game_responses(current))[0]["named"])
        self.assertTrue(await self.db.set_game_response(current, 10, None))
        self.assertFalse(await self.db.set_game_response(original, 10, "recommend", named=True))
        self.assertEqual(await self.db.game_responses(dict(await self.db.account(10))), [])

    async def test_concurrent_responses_accept_only_one_revision(self):
        results = await asyncio.gather(
            self.db.set_game_response(self.account, 10, "recommend", named=True),
            self.db.set_game_response(self.account, 10, "recommend"),
        )
        self.assertCountEqual(results, [True, False])
        current = dict(await self.db.account(10))
        self.assertEqual(len(await self.db.game_responses(current)), 1)
        self.assertFalse(await self.db.set_game_response(self.account, 10, "recommend", named=True))

    async def test_token_reset_without_votes_invalidates_old_buttons(self):
        await self.db.set_refresh_token(10, None)
        current = await self.approved(10, "1")
        self.assertNotEqual(current["game_response_version"], self.account["game_response_version"])
        self.assertFalse(await self.db.set_game_response(self.account, 10, "recommend", named=True))
        self.assertTrue(await self.db.set_game_response(current, 10, "recommend"))

    async def test_publication_changes_preserve_responses_and_their_revision(self):
        await self.respond(self.account, 10, "recommend")
        revision = self.account["game_response_version"]
        self.assertTrue(
            await self.db.set_publication(
                10, "1", "9", "publish_presence", True, expected_version=self.account["publication_version"]
            )
        )
        current = dict(await self.db.account(10))
        self.assertEqual(current["game_response_version"], revision)
        self.assertEqual((await self.db.game_responses(current))[0]["response"], "recommend")

    async def test_backlog_status_and_family_response_change_independently(self):
        generation = self.account["token_generation"]
        self.assertTrue(await self.respond(self.account, 10, "recommend", named=True))
        for status in ("backlog", "completed"):
            self.assertTrue(
                await self.db.set_game_status(10, "1", "9", 10, "Game", status, expected_generation=generation)
            )
            self.assertEqual(await self.db.game_marks(10), {10: {"name": "Game", "status": status}})
            self.assertEqual((await self.db.game_responses(self.account))[0]["response"], "recommend")
        self.assertTrue(await self.respond(self.account, 10, None))
        self.assertEqual(await self.db.game_marks(10), {10: {"name": "Game", "status": "completed"}})
        self.assertTrue(await self.respond(self.account, 10, "together"))
        self.assertTrue(await self.db.set_game_status(10, "1", "9", 10, "Game", None, expected_generation=generation))
        self.assertEqual((await self.db.game_responses(self.account))[0]["response"], "together")
        self.assertEqual(await self.db.game_marks(10), {})

    async def test_version_ten_upgrade_preserves_statuses_events_transfers_and_login(self):
        async with self.db.pool.acquire() as connection:
            await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            with patch("bot.migrations.MIGRATIONS", MIGRATIONS[:10]):
                await migrate(connection)
            await connection.execute("INSERT INTO families (family_id, name) VALUES ('9', 'Existing')")
            await connection.execute(
                "INSERT INTO accounts (telegram_id, steam_id, family_id, status, refresh_token) "
                "VALUES (10, '1', '9', 'approved', $1)",
                self.db.fernet.encrypt(b"existing-token"),
            )
            await connection.execute(
                "INSERT INTO game_statuses (telegram_id, appid, name, status) "
                "VALUES (10, 10, 'Existing game', 'playing')"
            )
            await connection.execute(
                "INSERT INTO accounts (telegram_id, steam_id, family_id, status, refresh_token) "
                "VALUES (20, '2', '9', 'approved', $1)",
                self.db.fernet.encrypt(b"target-token"),
            )
            await connection.execute("UPDATE families SET owner_telegram_id = 10 WHERE family_id = '9'")
            transfer = await connection.fetchrow(
                "INSERT INTO owner_transfers (family_id, owner_id, target_id, owner_steam_id, target_steam_id, "
                "owner_generation, target_generation, owner_publication_version, target_publication_version) "
                "SELECT '9', 10, 20, '1', '2', a.token_generation, b.token_generation, "
                "a.publication_version, b.publication_version FROM accounts a JOIN accounts b ON b.telegram_id = 20 "
                "WHERE a.telegram_id = 10 RETURNING *"
            )
            event_id = await connection.fetchval(
                "INSERT INTO game_nights (family_id, creator_telegram_id, creator_steam_id, chat_id, link_version, "
                "starts_at, timezone, appids) VALUES ('9', 10, '1', -100, 0, '2099-01-02T18:00:00Z', 'UTC', '{10,20}') "
                "RETURNING id"
            )
            await connection.execute(
                "INSERT INTO game_night_participants (event_id, telegram_id, steam_id, vote, rsvp) "
                "VALUES ($1, 10, '1', 10, 'going')",
                event_id,
            )
            event = await connection.fetchrow("SELECT * FROM game_nights")
            participant = await connection.fetchrow("SELECT * FROM game_night_participants")
            before = dict(await connection.fetchrow("SELECT * FROM accounts WHERE telegram_id = 10"))
            await migrate(connection)
            after = dict(await connection.fetchrow("SELECT * FROM accounts WHERE telegram_id = 10"))
            self.assertTrue(after.pop("game_response_version"))
            self.assertEqual(after, before)
            self.assertEqual(await connection.fetchrow("SELECT * FROM game_nights"), event)
            self.assertEqual(await connection.fetchrow("SELECT * FROM game_night_participants"), participant)
            self.assertEqual(await connection.fetchrow("SELECT * FROM owner_transfers"), transfer)
            self.assertEqual(
                [
                    tuple(row)
                    for row in await connection.fetch(
                        "SELECT version, name FROM schema_migrations WHERE version >= 8 ORDER BY version"
                    )
                ],
                [
                    (8, "008_game_statuses.sql"),
                    (9, "009_game_nights.sql"),
                    (10, "010_owner_transfers.sql"),
                    (11, "011_game_responses.sql"),
                ],
            )
        self.account = dict(await self.db.account(10))
        self.assertEqual(self.db.refresh_token(self.account), "existing-token")
        self.assertTrue(await self.respond(self.account, 10, "recommend"))
        self.assertEqual(await self.db.owner_transfer("9"), transfer)
        self.assertEqual(await self.db.game_marks(10), {10: {"name": "Existing game", "status": "playing"}})

    async def respond(self, account, appid, response, *, named=False):
        result = await self.db.set_game_response(account, appid, response, named=named)
        if result:
            account.update(dict(await self.db.account(account["telegram_id"])))
        return result

    async def test_response_upsert_change_clear_and_named_privacy(self):
        for _ in range(2):
            self.assertTrue(await self.respond(self.account, 10, "recommend"))
        self.assertEqual(
            await self.db.game_responses(self.account),
            [
                {
                    "telegram_id": 10,
                    "steam_id": "1",
                    "appid": 10,
                    "response": "recommend",
                    "named": False,
                    "game_response_version": self.account["game_response_version"],
                }
            ],
        )
        self.assertTrue(await self.respond(self.account, 10, "recommend", named=True))
        self.assertTrue((await self.db.game_responses(self.account))[0]["named"])
        self.assertTrue(await self.respond(self.account, 10, "together", named=True))
        self.assertEqual((await self.db.game_responses(self.account))[0]["response"], "together")
        self.assertFalse((await self.db.game_responses(self.account))[0]["named"])
        self.assertTrue(await self.respond(self.account, 10, "not_interested", named=True))
        response = (await self.db.game_responses(self.account))[0]
        self.assertEqual((response["response"], response["named"]), ("not_interested", False))
        for _ in range(2):
            self.assertTrue(await self.respond(self.account, 10, None))
        self.assertEqual(await self.db.game_responses(self.account), [])

    async def test_family_scoped_reads_keep_not_interested_private(self):
        other = await self.approved(30, "3", "8")
        await self.respond(self.account, 10, "recommend", named=True)
        await self.respond(self.peer, 20, "not_interested")
        await self.respond(other, 30, "together")
        responses = await self.db.game_responses(self.account)
        self.assertEqual([(row["telegram_id"], row["response"]) for row in responses], [(10, "recommend")])
        self.assertEqual((await self.db.game_responses(self.peer))[-1]["response"], "not_interested")
        self.assertEqual([row["telegram_id"] for row in await self.db.game_responses(other)], [30])
        await self.db.set_status(20, "blocked")
        self.assertEqual([row["telegram_id"] for row in await self.db.game_responses(self.account)], [10])
        self.assertEqual(await self.db.game_responses(self.peer), [])

    async def test_invalid_values_and_stale_registration_cannot_write_or_read(self):
        await self.respond(self.account, 10, "recommend")
        for changed in (
            {"family_id": "8"},
            {"steam_id": "other"},
            {"telegram_id": 99},
            {"created_at": self.account["created_at"] + timedelta(seconds=1)},
            {"publication_version": "old"},
            {"game_response_version": "old"},
        ):
            with self.subTest(changed=changed):
                stale = dict(self.account, **changed)
                self.assertFalse(await self.respond(stale, 10, "together"))
                self.assertEqual(await self.db.game_responses(stale), [])
        for appid, response in ((0, "recommend"), (-1, "recommend"), (2**32, "recommend"), (10, "unknown")):
            with self.subTest(appid=appid, response=response):
                self.assertFalse(await self.respond(self.account, appid, response))
        self.assertEqual((await self.db.game_responses(self.account))[0]["response"], "recommend")

    async def test_reset_paths_remove_votes_and_reapproval_cannot_restore_them(self):
        for index, reset in enumerate(
            (
                lambda account: self.db.set_status(account["telegram_id"], "blocked"),
                lambda account: self.db.set_status(account["telegram_id"], "pending"),
                lambda account: self.db.set_refresh_token(account["telegram_id"], None),
                lambda account: self.db.revoke_account(account["telegram_id"], account["steam_id"], "9"),
                lambda account: self.db.revoke_membership(account["telegram_id"], "9"),
            ),
            start=1,
        ):
            with self.subTest(reset=index):
                account = await self.approved(100 + index, str(100 + index))
                self.assertTrue(await self.respond(account, 10, "recommend"))
                await reset(account)
                self.assertFalse(await self.respond(account, 10, "together"))
                self.assertEqual(await self.db.game_responses(account), [])
                current = await self.approved(account["telegram_id"], account["steam_id"])
                self.assertFalse(await self.respond(account, 10, "together"))
                self.assertEqual(await self.db.game_responses(current), [])
                self.assertEqual(
                    await self.db.pool.fetchval(
                        "SELECT count(*) FROM game_responses WHERE telegram_id = $1", account["telegram_id"]
                    ),
                    0,
                )

    async def test_same_login_preserves_votes_and_relink_or_family_move_removes_them(self):
        await self.respond(self.account, 10, "recommend")
        await self.db.save_account(10, "1", "9", "renewed")
        self.assertEqual(len(await self.db.game_responses(self.account)), 1)
        for steam_id, family_id in (("new", "9"), ("new", "8")):
            current = dict(await self.db.account(10))
            await self.respond(current, 10, "recommend")
            await self.db.save_account(10, steam_id, family_id, "relinked")
            self.assertEqual(
                await self.db.pool.fetchval("SELECT count(*) FROM game_responses WHERE telegram_id = 10"), 0
            )
            self.assertFalse(await self.respond(current, 10, "recommend"))
            await self.db.set_status(10, "approved")
            self.assertEqual(await self.db.game_responses(dict(await self.db.account(10))), [])

    async def test_membership_departure_and_account_or_family_deletion_remove_votes(self):
        await self.respond(self.account, 10, "recommend")
        await self.respond(self.peer, 20, "together")
        await self.db.reconcile_members("9", ["2"])
        self.assertEqual([row["telegram_id"] for row in await self.db.game_responses(self.peer)], [20])
        current = await self.approved(10, "1")
        self.assertEqual([row["telegram_id"] for row in await self.db.game_responses(current)], [20])
        await self.respond(current, 10, "recommend")
        await self.db.delete_user(10)
        replacement = await self.approved(10, "1")
        self.assertFalse(await self.respond(current, 10, "recommend"))
        self.assertEqual([row["telegram_id"] for row in await self.db.game_responses(replacement)], [20])
        await self.db.pool.execute("DELETE FROM families WHERE family_id = '9'")
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM game_responses"), 0)

    async def test_migration_is_repeatable_preserves_accounts_and_enforces_valid_responses(self):
        await self.respond(self.account, 10, "recommend")
        async with self.db.pool.acquire() as connection:
            await migrate(connection)
            await connection.execute("DELETE FROM schema_migrations WHERE version = 11")
            await migrate(connection)
        self.assertEqual((await self.db.account(10))["status"], "approved")
        self.assertEqual(len(await self.db.game_responses(self.account)), 1)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.db.pool.execute("UPDATE game_responses SET response = 'invalid'")
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.db.pool.execute("UPDATE game_responses SET response = 'not_interested', named = TRUE")
