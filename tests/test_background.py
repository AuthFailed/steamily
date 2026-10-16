import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.family import Families

LOOPS = (("refresh_loop", "refresh"), ("presence_loop", "check_presence"))


class BackgroundLoopsTest(unittest.IsolatedAsyncioTestCase):
    async def test_database_failure_is_logged_and_next_cycle_processes_families(self):
        for loop_name, operation in LOOPS:
            with self.subTest(loop=loop_name):
                events = []

                async def enumerate_families(events=events):
                    events.append("enumerate")
                    if events.count("enumerate") == 1:
                        raise OSError("database unavailable")
                    return [{"family_id": "healthy"}]

                async def process(family_id, events=events):
                    events.append(family_id)
                    raise asyncio.CancelledError

                async def sleep(interval, events=events):
                    events.append(interval)

                families = Families(SimpleNamespace(families_with_accounts=enumerate_families), None, None)
                setattr(families, operation, process)
                with (
                    patch("bot.family.asyncio", SimpleNamespace(sleep=sleep)),
                    self.assertLogs("bot.family", level="ERROR") as logs,
                    self.assertRaises(asyncio.CancelledError),
                ):
                    await getattr(families, loop_name)(7)

                expected = ["enumerate", 7, "enumerate", "healthy"]
                self.assertEqual(events, [7, *expected] if loop_name == "presence_loop" else expected)
                self.assertIsInstance(logs.records[0].exc_info[1], OSError)

    async def test_family_failure_keeps_other_families_and_later_cycles_running(self):
        for loop_name, operation in LOOPS:
            with self.subTest(loop=loop_name):
                processed = []
                db = SimpleNamespace(
                    families_with_accounts=AsyncMock(return_value=[{"family_id": "broken"}, {"family_id": "healthy"}])
                )

                async def process(family_id, processed=processed):
                    processed.append(family_id)
                    if family_id == "broken":
                        raise OSError("family unavailable")
                    if processed.count("healthy") == 2:
                        raise asyncio.CancelledError

                families = Families(db, None, None)
                setattr(families, operation, process)
                with (
                    patch("bot.family.asyncio", SimpleNamespace(sleep=AsyncMock())),
                    self.assertLogs("bot.family", level="ERROR"),
                    self.assertRaises(asyncio.CancelledError),
                ):
                    await getattr(families, loop_name)(7)
                self.assertEqual(processed, ["broken", "healthy", "broken", "healthy"])

    async def test_cancellation_propagates_while_sleeping_enumerating_or_processing(self):
        for loop_name, operation in LOOPS:
            for stage in ("sleep", "enumerate", "process"):
                with self.subTest(loop=loop_name, stage=stage):
                    entered = asyncio.Event()

                    async def wait(*args, entered=entered):
                        entered.set()
                        await asyncio.Event().wait()

                    async def sleep(interval, stage=stage):
                        if stage == "sleep":
                            await wait()

                    async def enumerate_families(stage=stage):
                        if stage == "enumerate":
                            await wait()
                        return [{"family_id": "healthy"}]

                    families = Families(SimpleNamespace(families_with_accounts=enumerate_families), None, None)
                    setattr(families, operation, AsyncMock(side_effect=wait))
                    with (
                        patch("bot.family.asyncio", SimpleNamespace(sleep=sleep)),
                        self.assertNoLogs("bot.family", level="ERROR"),
                    ):
                        task = asyncio.create_task(getattr(families, loop_name)(7))
                        try:
                            await asyncio.wait_for(entered.wait(), 1)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(task, 1)
                        finally:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
