import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        self.begin_data = {"client_id": 1, "request_id": b"r", "url": "https://s.team/q/1/a", "interval": 0}
        self.begin = AsyncMock(return_value=self.begin_data)
        self.clock = MagicMock(return_value=1000.0)
        self.pending = []
        self.patches = [
            patch("bot.login.begin_qr_login", self.begin),
            patch("bot.login.poll_qr_login", side_effect=self.wait_for_cancel),
            patch("bot.login.time", SimpleNamespace(monotonic=self.clock)),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        tasks = set(self.logins.tasks.values()) | set(self.pending)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    async def run_login(self, poll):
        with patch("bot.login.poll_qr_login", poll):
            data = await self.logins.start(1, self.manager)
            await self.logins.tasks[1]
        return data

    async def assert_rejected(self, telegram_id, reason, retry_after=None):
        from bot.login import LoginRejected

        with self.assertRaises(LoginRejected) as rejected:
            await self.logins.start(telegram_id, self.manager)
        self.assertEqual(rejected.exception.reason, reason)
        if retry_after is not None:
            self.assertEqual(rejected.exception.retry_after, retry_after)

    async def wait_for_cancel(self, *args):
        await asyncio.Event().wait()

    async def test_same_user_is_reserved_before_steam_begin_returns(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def begin(http):
            entered.set()
            await finish.wait()

        with patch("bot.login.begin_qr_login", side_effect=begin) as start:
            first = asyncio.create_task(self.logins.start(1, self.manager))
            await entered.wait()
            second = asyncio.create_task(self.logins.start(1, self.manager))
            try:
                await asyncio.sleep(0)
                self.assertTrue(second.done(), "duplicate start must reject while Steam begin is still pending")
                self.assertEqual(start.await_count, 1)
                with self.assertRaises(Exception) as rejected:
                    await second
                self.assertEqual(rejected.exception.reason, "login_cooldown")
                self.assertIs(self.logins.tasks[1], first)
            finally:
                first.cancel()
                second.cancel()
                await asyncio.gather(first, second, return_exceptions=True)

    async def test_rejected_retry_keeps_existing_qr_and_cooldown_expires(self):
        with patch("bot.login.poll_qr_login", side_effect=self.wait_for_cancel):
            data = await self.logins.start(1, self.manager)
            original = self.logins.tasks[1]
            self.clock.return_value = 1000.2
            await self.assert_rejected(1, "login_cooldown", retry_after=15)
            self.assertIs(self.logins.tasks[1], original)
            self.assertFalse(original.cancelling())
            self.assertTrue(Path(data["qr"]).exists())
            self.begin.assert_awaited_once()

            self.clock.return_value = 1015.0
            await self.assert_rejected(1, "login_active")
            self.logins.cancel(1)
            await asyncio.gather(original, return_exceptions=True)
            self.assertFalse(Path(data["qr"]).exists())
            replacement = await self.logins.start(1, self.manager)
            self.assertIsNot(self.logins.tasks[1], original)
            self.assertTrue(Path(replacement["qr"]).exists())
            self.assertEqual(self.begin.await_count, 2)

    async def test_global_limit_counts_pending_steam_requests(self):
        entered = asyncio.Event()
        calls = 0

        async def begin(http):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
            await asyncio.Event().wait()

        with patch("bot.login.MAX_ACTIVE_LOGINS", 2), patch("bot.login.begin_qr_login", side_effect=begin):
            self.pending = [asyncio.create_task(self.logins.start(user, self.manager)) for user in (1, 2)]
            await entered.wait()
            await self.assert_rejected(3, "login_busy", retry_after=15)
            self.assertEqual(calls, 2)
            self.logins.cancel(1)
            await self.assert_rejected(3, "login_busy", retry_after=15)
            await asyncio.gather(self.pending[0], return_exceptions=True)
            third = asyncio.create_task(self.logins.start(3, self.manager))
            self.pending.append(third)
            await asyncio.sleep(0)
            self.assertEqual(calls, 3, "a rejected start must remain eligible once capacity is released")

    async def test_cancel_during_begin_rejects_a_late_steam_response(self):
        entered = asyncio.Event()

        async def begin(http):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return self.begin_data

        with (
            patch("bot.login.begin_qr_login", side_effect=begin),
            patch("bot.login.qr_file") as qr,
            patch("bot.login.poll_qr_login") as poll,
        ):
            request = asyncio.create_task(self.logins.start(1, self.manager))
            self.pending.append(request)
            await entered.wait()
            self.logins.cancel(1)
            self.assertIs(self.logins.tasks[1], request, "a canceled request occupies its slot until it drains")
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertFalse(self.logins.tasks)
            qr.assert_not_called()
            poll.assert_not_called()
            self.families.register.assert_not_awaited()

    async def test_cancel_before_poller_first_step_cleans_qr_and_capacity(self):
        with patch("bot.login.poll_qr_login") as poll:
            data = await self.logins.start(1, self.manager)
            worker = self.logins.tasks[1]
            self.logins.cancel(1)
            await asyncio.gather(worker, return_exceptions=True)
            self.assertFalse(self.logins.tasks)
            self.assertFalse(Path(data["qr"]).exists())
            poll.assert_not_called()

    async def test_canceled_poll_cannot_register_a_late_token(self):
        entered = asyncio.Event()

        async def poll(http, login):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return TOKEN

        with patch("bot.login.poll_qr_login", side_effect=poll):
            data = await self.logins.start(1, self.manager)
            worker = self.logins.tasks[1]
            await entered.wait()
            self.logins.cancel(1)
            await asyncio.gather(worker, return_exceptions=True)
        self.families.register.assert_not_awaited()
        self.manager.start.assert_not_awaited()
        self.manager.update.assert_not_awaited()
        self.assertFalse(self.logins.tasks)
        self.assertFalse(Path(data["qr"]).exists())

    async def test_canceled_registration_cannot_open_the_menu(self):
        entered = asyncio.Event()

        async def register(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return MagicMock()

        self.families.register.side_effect = register
        with patch("bot.login.poll_qr_login", return_value=TOKEN):
            data = await self.logins.start(1, self.manager)
            worker = self.logins.tasks[1]
            await entered.wait()
            self.logins.cancel(1)
            await asyncio.gather(worker, return_exceptions=True)
        self.manager.start.assert_not_awaited()
        self.manager.update.assert_not_awaited()
        self.assertFalse(self.logins.tasks)
        self.assertFalse(Path(data["qr"]).exists())

    async def test_begin_failure_and_timeout_release_capacity(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.logins = Logins(self.families, bot=AsyncMock())
                begin = self.wait_for_cancel if timeout else AsyncMock(side_effect=RuntimeError("begin failed"))
                with (
                    patch("bot.login.begin_qr_login", side_effect=begin),
                    patch("bot.login.LOGIN_TIMEOUT", 0 if timeout else 300),
                ):
                    with self.assertRaises(TimeoutError if timeout else RuntimeError):
                        await self.logins.start(1, self.manager)
                self.assertFalse(self.logins.tasks)
                self.clock.return_value += 15
                await self.run_login(AsyncMock(return_value=TOKEN))
                self.assertFalse(self.logins.tasks)

    async def test_poll_failure_releases_capacity_and_removes_qr(self):
        with self.assertLogs("bot.login", level="ERROR"):
            data = await self.run_login(AsyncMock(side_effect=RuntimeError("poll failed")))
        self.assertFalse(self.logins.tasks)
        self.assertFalse(Path(data["qr"]).exists())
        self.manager.update.assert_awaited_with({"login": "failed"})

    async def test_timeout_cancels_an_in_flight_poll(self):
        entered = asyncio.Event()
        timeouts = []
        real_timeout = asyncio.timeout

        def timeout(seconds):
            timer = real_timeout(None)
            timeouts.append(timer)
            return timer

        async def poll(http, login):
            entered.set()
            await asyncio.Event().wait()

        with (
            patch("bot.login.asyncio.timeout", side_effect=timeout),
            patch("bot.login.poll_qr_login", side_effect=poll),
        ):
            data = await self.logins.start(1, self.manager)
            worker = self.logins.tasks[1]
            await entered.wait()
            timeouts[-1].reschedule(asyncio.get_running_loop().time())
            await worker
        self.manager.update.assert_awaited_with({"login": "timeout"})
        self.families.register.assert_not_awaited()
        self.assertFalse(self.logins.tasks)
        self.assertFalse(Path(data["qr"]).exists())

    async def test_shutdown_drains_begin_and_poll_and_rejects_new_attempts(self):
        with patch("bot.login.poll_qr_login", side_effect=self.wait_for_cancel):
            data = await self.logins.start(1, self.manager)
            worker = self.logins.tasks[1]
            entered = asyncio.Event()

            async def begin(http):
                entered.set()
                await asyncio.Event().wait()

            with patch("bot.login.begin_qr_login", side_effect=begin):
                request = asyncio.create_task(self.logins.start(2, self.manager))
                self.pending.append(request)
                await entered.wait()
                await self.logins.close()
            self.assertTrue(worker.done() and request.done())
            self.assertFalse(self.logins.tasks)
            self.assertFalse(Path(data["qr"]).exists())
            await self.assert_rejected(3, "login_stopping")

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
        self.assertFalse(self.logins.tasks)

    async def test_timeout_and_no_family_are_shown_in_place(self):
        with patch("bot.login.LOGIN_TIMEOUT", 0):
            await self.run_login(AsyncMock(return_value=None))
        self.manager.update.assert_awaited_with({"login": "timeout"})

        self.clock.return_value += 15
        self.families.register.side_effect = NoFamily("no family")
        await self.run_login(AsyncMock(return_value=TOKEN))
        self.manager.update.assert_awaited_with({"login": "no_family"})
        self.manager.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
