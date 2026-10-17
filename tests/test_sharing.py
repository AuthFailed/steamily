import json
import unittest
from copy import deepcopy
from pathlib import Path

from bot.library import query, removal_impact
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


class RemovalImpactTest(unittest.TestCase):
    def test_only_owner_loses_title(self):
        apps = [{"appid": 1, "name": "Solo", "owners": ["a"], "exclude_reason": 0}]
        self.assertEqual(removal_impact(apps, "a"), [{"appid": 1, "name": "Solo", "before": 1, "after": 0}])

    def test_other_owners_keep_title_with_fewer_copies(self):
        apps = [{"appid": 1, "name": "Shared", "owners": ["a", "b", "c"], "exclude_reason": 0}]
        self.assertEqual(removal_impact(apps, "a"), [{"appid": 1, "name": "Shared", "before": 3, "after": 2}])

    def test_excluded_games_do_not_affect_shared_capacity(self):
        for reason in (1, 999):
            with self.subTest(reason=reason):
                apps = [{"appid": 1, "name": "Excluded", "owners": ["a"], "exclude_reason": reason}]
                self.assertEqual(removal_impact(apps, "a"), [])

    def test_old_snapshot_without_reason_remains_shareable(self):
        apps = [{"appid": 1, "name": "Old", "owners": ["a", "b"]}]
        self.assertEqual(removal_impact(apps, "a"), [{"appid": 1, "name": "Old", "before": 2, "after": 1}])

    def test_absent_member_and_empty_library_have_no_impact(self):
        self.assertEqual(removal_impact([{"appid": 1, "name": "Other", "owners": ["b"]}], "a"), [])
        self.assertEqual(removal_impact([], "a"), [])

    def test_lost_titles_sort_first_then_name_and_appid(self):
        apps = [
            {"appid": 1, "name": "A kept title", "owners": ["a", "b"]},
            {"appid": 3, "name": "alpha", "owners": ["a"]},
            {"appid": 4, "name": "Beta", "owners": ["a"]},
            {"appid": 2, "name": "ALPHA", "owners": ["a"]},
        ]
        self.assertEqual([app["appid"] for app in removal_impact(apps, "a")], [2, 3, 4, 1])

    def test_preview_does_not_change_snapshot(self):
        apps = [
            {"appid": 2, "name": "Solo", "owners": ["a"], "exclude_reason": 0, "acquired": 123},
            {"appid": 1, "name": "Shared", "owners": ["a", "b"], "exclude_reason": 0},
        ]
        before = deepcopy(apps)
        removal_impact(apps, "a")
        self.assertEqual(apps, before)
