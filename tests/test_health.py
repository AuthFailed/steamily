import asyncio
import json
import tempfile
import unittest
from pathlib import Path
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

    async def test_enumeration_failure_stays_failed_until_worker_restarts_after_db_recovers(self):
        db = AsyncMock()
        db.families_with_accounts.side_effect = RuntimeError("postgresql://private:password@host/db")
        families = Families(db, None, AsyncMock(), health=self.health)
        old = self.tasks["refresh"]
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
        self.tasks["refresh"] = asyncio.create_task(families.refresh_loop(60))
        await asyncio.gather(self.tasks["refresh"], return_exceptions=True)
        report = self.health.snapshot(self.tasks)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["workers"]["refresh"]["status"], "failed")
        self.assertNotIn("password", json.dumps(report))
        db.families_with_accounts.side_effect = None
        db.families_with_accounts.return_value = []
        self.assertFalse(self.health.snapshot(self.tasks)["healthy"])
        self.tasks["refresh"] = asyncio.create_task(families.refresh_loop(60))
        await asyncio.sleep(0)
        self.assertTrue(self.health.snapshot(self.tasks)["healthy"])

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
