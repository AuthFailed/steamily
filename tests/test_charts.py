import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot import charts
from bot.family import Families


class ChartCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "charts"
        patched = patch("bot.charts.CHART_DIR", self.directory)
        patched.start()
        self.addCleanup(patched.stop)
        self.db = AsyncMock()
        self.families = Families(self.db, http=None, bot=None)

    def family(self, family_id):
        family = self.families.get(family_id)
        self.addCleanup(family.cleanup_charts)
        return family

    async def test_last_logout_deletes_only_its_family_pngs(self):
        first, second = self.family("one"), self.family("two")
        first_chart = Path(charts.bars("Games", [("Alice", 20)], directory=first.chart_dir))
        second_chart = Path(charts.columns("Added", [("2026-10", 2)], directory=second.chart_dir))
        self.assertEqual(first_chart.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.db.account.return_value = {"steam_id": "alice", "family_id": "one", "owner_telegram_id": None}
        self.db.family_accounts.return_value = []

        await self.families.leave(1)

        self.assertFalse(first_chart.parent.exists())
        self.assertTrue(second_chart.is_file())
        with self.assertRaises(FileNotFoundError):
            _ = first.chart_dir

    async def test_delayed_save_cannot_restore_a_deleted_chart_directory(self):
        family = self.family("one")
        directory = family.chart_dir
        started, released = threading.Event(), threading.Event()
        save = charts._save

        def delayed_save(*args):
            started.set()
            if not released.wait(5):
                raise TimeoutError("chart cleanup did not finish")
            return save(*args)

        with patch("bot.charts._save", delayed_save):
            render = asyncio.create_task(asyncio.to_thread(charts.bars, "Games", [("Alice", 20)], directory=directory))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.families._forget_family("one")
            finally:
                released.set()
            with self.assertRaises(FileNotFoundError):
                await render
        self.assertFalse(directory.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_startup_reset_removes_legacy_pngs_and_crash_directories(self):
        family = self.family("old")
        chart = Path(charts.bars("Games", [("Alice", 20)], directory=family.chart_dir))
        legacy = self.directory / "legacy.png"
        legacy.write_bytes(chart.read_bytes())

        charts.reset_cache()

        self.assertTrue(self.directory.is_dir())
        self.assertEqual(list(self.directory.iterdir()), [])
