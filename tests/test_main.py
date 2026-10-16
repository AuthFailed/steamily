import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot import __main__ as app


class BackgroundTasksTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_background_task_logs_its_name_and_traceback(self):
        async def fail():
            raise RuntimeError("background failure")

        task = asyncio.create_task(fail(), name="family-refresh")
        await asyncio.gather(task, return_exceptions=True)
        with self.assertLogs("bot.__main__", level="ERROR") as logs:
            app.background_finished(task)
        self.assertIn("family-refresh", logs.output[0])
        self.assertIn("background failure", logs.output[0])
        self.assertIs(logs.records[0].exc_info[1], task.exception())

    async def test_unexpected_normal_completion_is_logged(self):
        async def finish():
            return None

        task = asyncio.create_task(finish(), name="family-presence")
        await task
        with self.assertLogs("bot.__main__", level="ERROR") as logs:
            app.background_finished(task)
        self.assertIn("family-presence", logs.output[0])
        self.assertIn("unexpectedly", logs.output[0])

    async def test_cancelled_background_task_is_not_logged_as_failure(self):
        task = asyncio.create_task(asyncio.sleep(60), name="family-refresh")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.assertNoLogs("bot.__main__", level="ERROR"):
            app.background_finished(task)
        self.assertTrue(task.cancelled())

    async def test_main_waits_for_background_cleanup_before_closing_resources(self):
        started, cleaned, tasks = set(), set(), []
        ready = asyncio.Event()

        async def background(name):
            tasks.append(asyncio.current_task())
            started.add(name)
            if len(started) == 3:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.add(name)

        families = MagicMock()
        families.refresh_loop.side_effect = lambda _: background("refresh")
        families.presence_loop.side_effect = lambda: background("presence")
        families.delivery_loop.side_effect = lambda: background("delivery")
        db = MagicMock()

        async def close():
            self.assertEqual(cleaned, {"refresh", "presence", "delivery"})

        db.pool.close = AsyncMock(side_effect=close)
        logins = MagicMock(close=AsyncMock(side_effect=close))

        async def poll(*args, **kwargs):
            await ready.wait()
            raise asyncio.CancelledError

        async def exit_http(*args):
            await close()

        dispatcher = MagicMock(start_polling=AsyncMock(side_effect=poll))
        bot = AsyncMock(me=AsyncMock(return_value=SimpleNamespace(username="bot")))
        http = MagicMock()
        http.__aenter__ = AsyncMock()
        http.__aexit__ = AsyncMock(side_effect=exit_http)
        try:
            with (
                patch.object(app, "required", return_value="configured"),
                patch.object(app, "poll_seconds", return_value=60),
                patch.object(app, "Bot", return_value=bot),
                patch.object(app.charts, "reset_cache"),
                patch.object(app.Db, "connect", AsyncMock(return_value=db)),
                patch.object(app.aiohttp, "ClientSession", return_value=http),
                patch.object(app, "Families", return_value=families),
                patch.object(app, "Logins", return_value=logins),
                patch.object(app, "create_dispatcher", return_value=dispatcher),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(app.main(), timeout=1)
            self.assertTrue(all(task.cancelled() for task in tasks))
            logins.close.assert_awaited_once()
            db.pool.close.assert_awaited_once()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
