import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bot.login import Logins
from bot.steam import NoFamily

ALICE = "76561198000000001"
TOKEN = "x." + "eyJzdWIiOiAiNzY1NjExOTgwMDAwMDAwMDEifQ" + ".y"  # {"sub": ALICE}


class LoginsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.families = MagicMock(http=None)
        self.families.register = AsyncMock()
        self.families.db.account = AsyncMock(return_value={"status": "approved"})
        self.logins = Logins(self.families, bot=AsyncMock())
        self.manager = AsyncMock()
        begin = {"client_id": 1, "request_id": b"r", "url": "https://s.team/q/1/a", "interval": 0}
        self.patches = [patch("bot.login.begin_qr_login", AsyncMock(return_value=begin))]
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
        self.families.register.assert_awaited_once_with(1, TOKEN, ALICE)
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


if __name__ == "__main__":
    unittest.main()
