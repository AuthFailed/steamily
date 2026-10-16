import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest

from bot.family import Families
from bot.health import Health, check, write_report


class HealthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = patch("bot.health.monotonic", return_value=100)
        self.clock.start()
        self.health = Health(60)
        self.tasks = {name: asyncio.create_task(asyncio.Event().wait()) for name in ("refresh", "presence")}
        for name in self.tasks:
            self.health.beat(name, failed=False)

    async def asyncTearDown(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.clock.stop()

    async def test_empty_install_is_healthy_but_stalled_worker_is_not(self):
        self.assertTrue(self.health.snapshot(self.tasks)["healthy"])
        with patch("bot.health.monotonic", return_value=281):
            report = self.health.snapshot(self.tasks)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["workers"]["refresh"]["status"], "stalled")

    async def test_missing_and_stale_steam_reads_make_live_workers_unhealthy(self):
        self.health.expect_families(["9", "10"])
        with patch("bot.health.monotonic", return_value=281):
            for name in self.tasks:
                self.health.beat(name)
            self.assertFalse(self.health.snapshot(self.tasks)["healthy"])
            self.health.steam_read("9")
            self.assertFalse(self.health.snapshot(self.tasks)["healthy"])
            self.health.steam_read("10")
            report = self.health.snapshot(self.tasks)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["steam"]["last_success_age_seconds"], 0)
        with patch("bot.health.monotonic", return_value=462):
            for name in self.tasks:
                self.health.beat(name)
            self.assertFalse(self.health.snapshot(self.tasks)["healthy"])
        self.health.expect_families([])
        self.assertEqual(self.health.snapshot(self.tasks)["steam"]["families"], [])

    async def test_enumeration_failure_is_unhealthy_then_recovers_automatically_for_each_worker(self):
        for name, loop_name, operation in (
            ("refresh", "refresh_loop", "refresh"),
            ("presence", "presence_loop", "check_presence"),
        ):
            with self.subTest(worker=name):
                failed, retry, recovered = asyncio.Event(), asyncio.Event(), asyncio.Event()
                sleep_calls = 0

                async def sleep(interval, name=name, failed=failed, retry=retry, recovered=recovered):
                    nonlocal sleep_calls
                    sleep_calls += 1
                    if name == "presence" and sleep_calls == 1:
                        return
                    if not failed.is_set():
                        failed.set()
                        await retry.wait()
                    else:
                        recovered.set()
                        await asyncio.Event().wait()

                db = AsyncMock()
                db.families_with_accounts.side_effect = [
                    RuntimeError("postgresql://private:password@host/db"),
                    [{"family_id": "9"}],
                ]
                families = Families(db, None, AsyncMock(), health=self.health)
                process = AsyncMock(side_effect=lambda _: self.health.steam_read("9"))
                setattr(families, operation, process)
                old = self.tasks[name]
                old.cancel()
                await asyncio.gather(old, return_exceptions=True)
                with (
                    patch("bot.family.asyncio", SimpleNamespace(sleep=sleep)),
                    self.assertLogs("bot.family", level="ERROR"),
                ):
                    task = self.tasks[name] = asyncio.create_task(getattr(families, loop_name)(60))
                    try:
                        await asyncio.wait_for(failed.wait(), timeout=1)
                        report = self.health.snapshot(self.tasks)
                        self.assertFalse(report["healthy"])
                        self.assertEqual(report["workers"][name]["status"], "failed")
                        self.assertNotIn("password", json.dumps(report))
                        self.assertFalse(task.done())
                        retry.set()
                        await asyncio.wait_for(recovered.wait(), timeout=1)
                        self.assertFalse(task.done())
                        report = self.health.snapshot(self.tasks)
                        self.assertTrue(report["healthy"])
                        self.assertEqual(report["workers"][name]["status"], "ok")
                        self.assertEqual(report["steam"]["last_success_age_seconds"], 0)
                        self.assertNotIn("password", json.dumps(report))
                        self.assertEqual(db.families_with_accounts.await_count, 2)
                        process.assert_awaited_once_with("9")
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        self.tasks[name] = asyncio.create_task(asyncio.Event().wait())

    async def test_failed_delivery_is_visible_without_credentials_or_fake_retry_queue(self):
        bot = AsyncMock()
        bot.send_message.side_effect = TelegramBadRequest(method="sendMessage", message="private-token")
        families = Families(AsyncMock(), None, bot, health=self.health)
        with self.assertLogs("bot.family", level="WARNING"):
            await families.send_alert({"notify_chat": -100, "family_id": "9"}, "hello")
        report = self.health.snapshot(self.tasks)
        self.assertEqual(report["notifications"]["failed_total"], 1)
        self.assertEqual(report["notifications"]["in_flight"], 0)
        self.assertIsNone(report["notifications"]["pending_retry"])
        self.assertNotIn("private-token", json.dumps(report))

    async def test_health_command_rejects_missing_corrupt_and_stale_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            self.assertFalse(check(path)["healthy"])
            path.write_text("not json")
            self.assertFalse(check(path)["healthy"])
            write_report(self.health.snapshot(self.tasks), path)
            self.assertTrue(check(path)["healthy"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch("bot.health.time.time", return_value=path.stat().st_mtime + 31):
                self.assertFalse(check(path)["healthy"])


if __name__ == "__main__":
    unittest.main()
