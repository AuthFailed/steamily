import unittest

from bot import library


class PlayModesTest(unittest.TestCase):
    def test_modes_use_exact_store_categories(self):
        cases = (
            ("supported_player_categoryids", 38, "online_coop"),
            ("supported_player_categoryids", 36, "online_pvp"),
            ("supported_player_categoryids", 24, "local_coop"),
            ("supported_player_categoryids", 37, "local_coop"),
            ("supported_player_categoryids", 39, "local_coop"),
            ("feature_categoryids", 44, "remote_play"),
        )
        for field, category, mode in cases:
            with self.subTest(category=category):
                categories = {"supported_player_categoryids": [], "feature_categoryids": [], field: [category]}
                expected = {"online_coop": False, "online_pvp": False, "local_coop": False, "remote_play": False}
                expected[mode] = True
                self.assertEqual(library.play_modes({"categories": categories}), expected)

    def test_generic_multiplayer_coop_and_controllers_do_not_imply_specific_modes(self):
        info = {"categories": {"supported_player_categoryids": [1, 9, 27], "feature_categoryids": [18, 28]}}
        self.assertEqual(
            library.play_modes(info),
            {"online_coop": False, "online_pvp": False, "local_coop": False, "remote_play": False},
        )

    def test_missing_metadata_is_unknown_and_explicit_empty_is_unsupported(self):
        unknown = {"online_coop": None, "online_pvp": None, "local_coop": None, "remote_play": None}
        for info in ({}, {"categories": None}, {"categories": {}}):
            with self.subTest(info=info):
                self.assertEqual(library.play_modes(info), unknown)
        self.assertEqual(
            library.play_modes({"categories": {"supported_player_categoryids": [], "feature_categoryids": []}}),
            {"online_coop": False, "online_pvp": False, "local_coop": False, "remote_play": False},
        )

    def test_missing_category_collection_only_leaves_its_modes_unknown(self):
        self.assertEqual(
            library.play_modes({"categories": {"feature_categoryids": [44]}}),
            {"online_coop": None, "online_pvp": None, "local_coop": None, "remote_play": True},
        )
        self.assertEqual(
            library.play_modes({"categories": {"supported_player_categoryids": [38]}}),
            {"online_coop": True, "online_pvp": False, "local_coop": False, "remote_play": None},
        )

    def test_modes_can_overlap_without_category_fields_being_interchangeable(self):
        self.assertEqual(
            library.play_modes(
                {"categories": {"supported_player_categoryids": [38, 36, 39], "feature_categoryids": [44]}}
            ),
            {"online_coop": True, "online_pvp": True, "local_coop": True, "remote_play": True},
        )
        self.assertEqual(
            library.play_modes({"categories": {"supported_player_categoryids": [44], "feature_categoryids": [38]}}),
            {"online_coop": False, "online_pvp": False, "local_coop": False, "remote_play": False},
        )


class ModeQueryTest(unittest.TestCase):
    def setUp(self):
        self.game = {"appid": 1, "name": "Mixed Co-op", "owners": ["owner"], "exclude_reason": 0}
        self.store = {
            "apps": {
                "1": {
                    "categories": {"supported_player_categoryids": [38, 36, 39], "feature_categoryids": [44]},
                    "play_mode": "multiplayer",
                    "price": 999,
                    "tags": [1685],
                }
            }
        }

    def test_selected_mode_requires_positive_metadata(self):
        for info in ({}, {"categories": None}, {"categories": {}}, {"categories": {"feature_categoryids": []}}):
            with self.subTest(info=info):
                self.assertEqual(library.query([self.game], {"apps": {"1": info}}, mode="remote_play"), [])
        self.assertEqual(library.query([self.game], self.store, mode="remote_play"), [self.game])

    def test_unknown_modes_fail_closed(self):
        self.assertEqual(library.query([self.game], self.store, mode="unsupported"), [])

    def test_online_and_default_modes_still_need_a_copy_per_selected_player(self):
        for mode in (None, "online_coop", "online_pvp"):
            with self.subTest(mode=mode):
                self.assertEqual(library.query([self.game], self.store, mode=mode, players=["a", "b"]), [])
                self.assertEqual(library.query([self.game], self.store, mode=mode, players=["guest"]), [self.game])

    def test_local_and_remote_modes_need_only_one_shared_host_copy(self):
        for mode in ("local_coop", "remote_play"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    library.query([self.game], self.store, mode=mode, players=["guest", "other"]), [self.game]
                )

    def test_excluded_host_can_invite_guests_but_guests_cannot_host_it(self):
        self.game["exclude_reason"] = 3
        for mode in ("local_coop", "remote_play"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    library.query([self.game], self.store, mode=mode, players=["owner", "guest"]), [self.game]
                )
                self.assertEqual(library.query([self.game], self.store, mode=mode, players=["guest", "other"]), [])
        for mode in (None, "online_coop", "online_pvp"):
            with self.subTest(mode=mode):
                self.assertEqual(library.query([self.game], self.store, mode=mode, players=["guest"]), [])

    def test_hosted_selection_needs_an_owned_copy_but_no_players_keeps_the_catalog(self):
        self.game["owners"] = []
        for mode in ("local_coop", "remote_play"):
            with self.subTest(mode=mode):
                self.assertEqual(library.query([self.game], self.store, mode=mode, players=["guest"]), [])
                self.assertEqual(library.query([self.game], self.store, mode=mode), [self.game])
        self.game.update(owners=["owner"], exclude_reason=3)
        self.assertEqual(library.query([self.game], self.store, mode="local_coop"), [self.game])
        self.assertEqual(library.query([self.game], self.store, mode="local_coop", owners=["guest"]), [])

    def test_hosted_modes_keep_search_tag_price_and_owner_filters(self):
        filters = {"mode": "remote_play", "players": ["guest", "other"]}
        self.assertEqual(
            library.query(
                [self.game], self.store, **filters, search="mixed CO", tag="1685", price=999, owners=["owner"]
            ),
            [self.game],
        )
        for extra in ({"search": "absent"}, {"tag": "19"}, {"price": 998}, {"owners": ["guest"]}):
            with self.subTest(extra=extra):
                self.assertEqual(library.query([self.game], self.store, **filters, **extra), [])
        self.store["apps"]["1"]["price"] = None
        self.assertEqual(library.query([self.game], self.store, **filters, price=999), [])

    def test_legacy_multiplayer_flag_remains_supported(self):
        self.assertEqual(library.query([self.game], self.store, multiplayer=True), [self.game])
        self.store["apps"]["1"]["play_mode"] = None
        self.assertEqual(library.query([self.game], self.store, multiplayer=True), [])


if __name__ == "__main__":
    unittest.main()
