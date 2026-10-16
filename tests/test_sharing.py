import json
import unittest
from pathlib import Path

from bot.library import query
from bot.steam import parse_snapshot


class SharingTest(unittest.TestCase):
    def setUp(self):
        library = json.loads((Path(__file__).parent / "fixtures" / "shared_library_apps.json").read_text())
        family = {
            "response": {
                "family_groupid": "9",
                "family_group": {"members": [{"steamid": member} for member in ("a", "b", "c")]},
            }
        }
        self.apps = parse_snapshot(family, library, {})["apps"]

    def test_parser_preserves_exclusions_and_defaults_omitted_reason(self):
        self.assertEqual({app["appid"]: app["exclude_reason"] for app in self.apps}, {101: 0, 102: 1, 103: 0, 104: 999})

    def test_selected_players_need_shareable_copies_or_ownership(self):
        cases = [
            (["a"], [101, 102, 103, 104]),
            (["c"], [101, 103]),
            (["a", "b"], [101, 102, 103, 104]),
            (["a", "c"], [101, 103]),
            (["a", "b", "c"], []),
        ]
        for players, expected in cases:
            with self.subTest(players=players):
                self.assertEqual(sorted(app["appid"] for app in query(self.apps, {}, players=players)), expected)

    def test_catalog_and_owner_filter_keep_excluded_games(self):
        for owners, expected in [([], [101, 102, 103, 104]), (["a", "b"], [101, 102, 103, 104]), (["c"], [])]:
            with self.subTest(owners=owners):
                self.assertEqual(sorted(app["appid"] for app in query(self.apps, {}, owners=owners)), expected)

    def test_old_snapshot_without_reason_remains_shareable(self):
        app = dict(self.apps[2])
        app.pop("exclude_reason", None)
        self.assertEqual(query([app], {}, players=["a", "c"]), [app])
