"""Render every stats view with real matplotlib, without Steam or Postgres."""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from bot import charts
from bot.dialogs.family import STATS, render_stat
from bot.family import Family


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
                    empty_path = render_stat(key, empty, {}, "en")
                    large_path = render_stat(key, large, store, "en")
                    self.assert_png(empty_path)
                    self.assert_png(large_path)
                    self.assertNotEqual(Path(empty_path).read_bytes(), Path(large_path).read_bytes())

    def test_unchanged_chart_reuses_the_file_and_changed_data_gets_a_new_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(charts, "CHART_DIR", Path(directory)):
            first = charts.bars("Games", [("Alice", 10)])
            self.assert_png(first)
            with patch("matplotlib.figure.Figure.savefig") as save:
                self.assertEqual(charts.bars("Games", [("Alice", 10)]), first)
            save.assert_not_called()
            changed = charts.bars("Games", [("Alice", 11)])
            self.assertNotEqual(changed, first)
            self.assert_png(changed)


if __name__ == "__main__":
    unittest.main()
