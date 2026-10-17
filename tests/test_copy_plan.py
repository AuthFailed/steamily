import unittest

from bot.library import copy_plan


class CopyPlanTest(unittest.TestCase):
    def test_four_players_need_two_more_copies_without_double_counting_owners(self):
        self.assertEqual(
            copy_plan({"owners": ["a", "b"], "exclude_reason": 0}, ["a", "b", "c", "d"]),
            {"players": 4, "owned": 2, "borrowable": 0, "eligible": 2, "additional": 2},
        )

    def test_spare_family_copies_cover_nonowners_and_keep_surplus_capacity(self):
        self.assertEqual(
            copy_plan({"owners": ["a", "b", "c"], "exclude_reason": 0}, ["a", "d"]),
            {"players": 2, "owned": 1, "borrowable": 2, "eligible": 3, "additional": 0},
        )

    def test_excluded_copies_only_cover_selected_owners(self):
        for reason in (1, 4, 999):
            with self.subTest(reason=reason):
                app = {"owners": ["a", "b"], "exclude_reason": reason}
                self.assertEqual(
                    copy_plan(app, ["a", "c"]),
                    {"players": 2, "owned": 1, "borrowable": 0, "eligible": 1, "additional": 1},
                )
                self.assertEqual(
                    copy_plan(app, ["a", "b"]),
                    {"players": 2, "owned": 2, "borrowable": 0, "eligible": 2, "additional": 0},
                )

    def test_missing_eligibility_remains_unknown_unless_every_player_owns_a_copy(self):
        for app in ({"owners": ["a", "b"]}, {"owners": ["a", "b"], "exclude_reason": None}):
            with self.subTest(app=app):
                self.assertEqual(
                    copy_plan(app, ["a", "c"]),
                    {"players": 2, "owned": 1, "borrowable": None, "eligible": None, "additional": None},
                )
                self.assertEqual(
                    copy_plan(app, ["a", "b"]),
                    {"players": 2, "owned": 2, "borrowable": None, "eligible": 2, "additional": 0},
                )

    def test_duplicate_players_and_owners_cannot_add_demand_or_capacity(self):
        self.assertEqual(
            copy_plan({"owners": ["a", "a", "b", "b"], "exclude_reason": 0}, ["a", "a", "c", "c", "d"]),
            {"players": 3, "owned": 1, "borrowable": 1, "eligible": 2, "additional": 1},
        )

    def test_empty_selection_needs_no_copies_even_with_unknown_eligibility(self):
        self.assertEqual(
            copy_plan({"owners": ["a"]}, []),
            {"players": 0, "owned": 0, "borrowable": None, "eligible": 0, "additional": 0},
        )
