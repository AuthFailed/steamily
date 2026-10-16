"""Render every stats view with real matplotlib, without Steam or Postgres."""

import asyncio
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from bot import charts
from bot.dialogs.family import STATS, render_stat
from bot.family import Families, Family


class ChartTest(unittest.TestCase):
    def assert_png(self, path):
        self.assertTrue(Path(path).is_file())
        with Image.open(path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreaterEqual(image.width, 600)
            self.assertGreaterEqual(image.height, 200)
            image.load()  # decode the full file, not just the PNG header
            for low, high in image.convert("RGB").getextrema():
                self.assertGreater(high - low, 100)  # visible plot/text against the dark background

    def test_all_stats_render_empty_and_large_libraries(self):
        now = datetime(2026, 10, 16, tzinfo=UTC).timestamp()
        empty = Family("empty")
        empty.snapshot = {"members": [], "apps": []}
        large = Family("large")
        members = [f"member-{i}" for i in range(6)]
        large.snapshot = {
            "members": members,
            "names": {member: f"Member {i} with a very long display name" for i, member in enumerate(members)},
            "apps": [
                {
                    "appid": i,
                    "name": f"A very long game title number {i:04d}",
                    "owners": [members[i % 6]] if i % 2 else members[:2],
                    "acquired": now - (i % 24) * 28 * 86400,
                }
                for i in range(1, 1201)
            ],
            "playtime": [{"steamid": members[i % 6], "appid": i, "seconds": i * 3600} for i in range(1, 1201)],
        }
        store = {
            "tag_names": {str(i): f"Genre {i}" for i in range(12)},
            "apps": {str(i): {"price": i * 100, "price_text": f"${i}.00", "tags": [i % 12]} for i in range(1, 1201)},
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(charts, "CHART_DIR", Path(directory)),
            patch("bot.library.time.time", return_value=now),
        ):
            for key in STATS:
                with self.subTest(stat=key):
                    empty_path = render_stat(key, empty, {}, "en", Path(directory))
                    large_path = render_stat(key, large, store, "en", Path(directory))
                    self.assert_png(empty_path)
                    self.assert_png(large_path)
                    self.assertNotEqual(Path(empty_path).read_bytes(), Path(large_path).read_bytes())

    def test_unchanged_chart_reuses_the_file_and_changed_data_gets_a_new_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(charts, "CHART_DIR", Path(directory)):
            first = charts.bars("Games", [("Alice", 10)], directory=Path(directory))
            self.assert_png(first)
            with patch("matplotlib.figure.Figure.savefig") as save:
                self.assertEqual(charts.bars("Games", [("Alice", 10)], directory=Path(directory)), first)
            save.assert_not_called()
            changed = charts.bars("Games", [("Alice", 11)], directory=Path(directory))
            self.assertNotEqual(changed, first)
            self.assert_png(changed)


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
        self.db.account.return_value = {
            "telegram_id": 1,
            "steam_id": "alice",
            "family_id": "one",
            "owner_telegram_id": None,
            "refresh_token": None,
            "token_renewed_at": 0.0,
            "token_generation": 1,
        }
        self.db.family.return_value = None  # Deleting the last account also removes its family row.
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


if __name__ == "__main__":
    unittest.main()
