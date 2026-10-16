import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bot.login import Logins
from bot.steam import NoFamily

ALICE = "76561198000000001"
TOKEN = "x." + "eyJzdWIiOiAiNzY1NjExOTgwMDAwMDAwMDEifQ" + ".y"  # {"sub": ALICE}
COOKIE = ALICE + "%7C%7C" + TOKEN


class LoginsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.families = MagicMock(http=None)
        self.families.register = AsyncMock()
        self.families.db.account = AsyncMock(return_value={"status": "approved"})
        self.logins = Logins(self.families, bot=AsyncMock())
        self.manager = AsyncMock()
        begin = {"client_id": 1, "request_id": b"r", "url": "https://s.team/q/1/a", "interval": 0}

        async def finalize(http, token, steam_id, *, on_refresh):
            await on_refresh(COOKIE)
            return COOKIE

        self.revoke = AsyncMock()
        self.patches = [
            patch("bot.login.begin_qr_login", AsyncMock(return_value=begin)),
            patch("bot.login.finalize_login", side_effect=finalize, create=True),
            patch("bot.login.revoke_session", self.revoke, create=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    async def run_login(self, poll):
        with patch("bot.login.poll_qr_login", poll):
            data = await self.logins.start(1, self.manager)
            await self.logins.tasks[1]
        return data

    async def test_rotation_edits_the_qr_and_success_opens_menu(self):
        async def poll(http, login):
            poll.calls += 1
            if poll.calls == 1:
                return None  # same code: nothing is redrawn
            if poll.calls == 2:
                login["url"] = "https://s.team/q/1/b"
                return None
            return TOKEN

        poll.calls = 0

        data = await self.run_login(poll)
        self.assertEqual((data["login"], data["url"]), ("waiting", "https://s.team/q/1/a"))
        updates = [c.args[0] for c in self.manager.update.await_args_list]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["url"], "https://s.team/q/1/b")
        self.assertNotEqual(updates[0]["qr"], data["qr"])
        args, kwargs = self.families.register.await_args
        self.assertEqual(args, (1, COOKIE, ALICE))
        self.assertTrue(callable(kwargs["on_refresh"]))
        self.revoke.assert_not_awaited()
        self.manager.start.assert_awaited_once()
        self.assertFalse(Path(data["qr"]).exists() or Path(updates[0]["qr"]).exists())  # temp files cleaned

    async def test_timeout_and_no_family_are_shown_in_place(self):
        with patch("bot.login.LOGIN_TIMEOUT", 0):
            await self.run_login(AsyncMock(return_value=None))
        self.manager.update.assert_awaited_with({"login": "timeout"})

        self.families.register.side_effect = NoFamily("no family")
        await self.run_login(AsyncMock(return_value=TOKEN))
        self.manager.update.assert_awaited_with({"login": "no_family"})
        self.manager.start.assert_not_called()
        self.assertEqual(self.revoke.await_args.args[1:], (COOKIE, ALICE))

    async def test_failed_registration_revokes_latest_staged_cookie_and_closes_private_session(self):
        latest = ALICE + "%7C%7Clatest"

        async def fail(*args, on_refresh, on_registered):
            await on_refresh(latest)
            raise RuntimeError("registration unavailable")

        self.families.register.side_effect = fail
        await self.run_login(AsyncMock(return_value=TOKEN))
        http, revoked, identity = self.revoke.await_args.args
        self.assertEqual((revoked, identity), (latest, ALICE))
        self.assertTrue(http.closed)
        self.assertIsNot(http, self.families.http)
        self.manager.update.assert_awaited_with({"login": "failed"})

    async def test_failed_finalization_revokes_the_approved_raw_token(self):
        with patch("bot.login.finalize_login", AsyncMock(side_effect=RuntimeError("unavailable"))):
            await self.run_login(AsyncMock(return_value=TOKEN))
        http, revoked, identity = self.revoke.await_args.args
        self.assertEqual((revoked, identity), (TOKEN, ALICE))
        self.assertTrue(http.closed)

    async def test_immediate_cancel_closes_private_session_and_removes_qr(self):
        begin = {"client_id": 1, "request_id": b"r", "url": "https://s.team/q/cancel", "interval": 5}
        with patch("bot.login.begin_qr_login", AsyncMock(return_value=begin)) as start:
            data = await self.logins.start(1, self.manager)
        http = start.await_args.args[0]
        self.addAsyncCleanup(http.close)
        task = self.logins.tasks[1]
        self.logins.cancel(1)
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertTrue(http.closed)
        self.assertFalse(Path(data["qr"]).exists())

    async def test_cancel_after_registration_commit_keeps_saved_session(self):
        committed = asyncio.Event()

        async def saved(*args, on_refresh, on_registered):
            on_registered()
            committed.set()
            await asyncio.Future()

        self.families.register.side_effect = saved
        with patch("bot.login.poll_qr_login", AsyncMock(return_value=TOKEN)):
            await self.logins.start(1, self.manager)
            task = self.logins.tasks[1]
            await asyncio.wait_for(committed.wait(), 1)
            self.logins.cancel(1)
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.revoke.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
