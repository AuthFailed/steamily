import asyncio
import unittest
from unittest.mock import AsyncMock, call, patch

from bot.family import Families


class FamilyLoopsTest(unittest.IsolatedAsyncioTestCase):
    async def test_loops_wait_repeat_isolate_family_errors_and_cancel(self):
        for loop_name, operation in (("presence_loop", "check_presence"), ("refresh_loop", "refresh")):
            for empty in (False, True):
                with self.subTest(loop=loop_name, empty=empty):
                    rows = [] if empty else [{"family_id": "bad"}, {"family_id": "healthy"}]
                    db = AsyncMock()
                    db.families_with_accounts.side_effect = [rows, rows, AssertionError("loop did not wait")]
                    families = Families(db, http=None, bot=AsyncMock())

                    async def check(family_id):
                        if family_id == "bad":
                            raise OSError("family unavailable")

                    action = AsyncMock(side_effect=check)
                    setattr(families, operation, action)
                    waits = asyncio.Queue()

                    async def sleep(interval, waits=waits):
                        resumed = asyncio.get_running_loop().create_future()
                        waits.put_nowait(resumed)
                        await resumed

                    with (
                        patch("bot.family.asyncio.sleep", side_effect=sleep) as sleep_mock,
                        patch("bot.family.log.error") as log,
                    ):
                        task = asyncio.create_task(getattr(families, loop_name)(interval=17))
                        try:
                            first_pass = int(loop_name == "refresh_loop")
                            for passes in range(first_pass, 3):
                                resumed = await asyncio.wait_for(waits.get(), timeout=1)
                                self.assertFalse(task.done())
                                self.assertTrue(waits.empty())
                                self.assertEqual(db.families_with_accounts.await_count, passes)
                                self.assertEqual(
                                    action.await_args_list,
                                    [] if empty else [call("bad"), call("healthy")] * passes,
                                )
                                self.assertEqual(sleep_mock.await_args_list, [call(17)] * (passes + 1 - first_pass))
                                if passes < 2:
                                    resumed.set_result(None)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                            self.assertEqual(log.call_count, 0 if empty else 2)
                            if not empty:
                                self.assertTrue(all(args.args[1] == "bad" for args in log.call_args_list))
                        finally:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

    async def test_cancelling_during_family_work_stops_the_loop(self):
        for loop_name, operation in (("presence_loop", "check_presence"), ("refresh_loop", "refresh")):
            with self.subTest(loop=loop_name):
                db = AsyncMock()
                db.families_with_accounts.return_value = [{"family_id": "first"}, {"family_id": "later"}]
                families = Families(db, http=None, bot=AsyncMock())
                started = asyncio.Event()

                async def check(family_id, started=started):
                    started.set()
                    await asyncio.get_running_loop().create_future()

                action = AsyncMock(side_effect=check)
                setattr(families, operation, action)
                with (
                    patch("bot.family.asyncio.sleep", new_callable=AsyncMock),
                    patch("bot.family.log.error") as log,
                ):
                    task = asyncio.create_task(getattr(families, loop_name)(interval=17))
                    try:
                        await asyncio.wait_for(started.wait(), timeout=1)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        action.assert_awaited_once_with("first")
                        db.families_with_accounts.assert_awaited_once()
                        log.assert_not_called()
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
