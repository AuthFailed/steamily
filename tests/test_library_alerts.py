import html
import re
import unittest

from bot import library


class LibraryAlertTest(unittest.TestCase):
    def test_new_games_are_grouped_per_owner_with_linked_numbered_titles(self):
        events = [
            {"appid": 1944430, "name": "Amnesia: The Bunker", "owner": "alice"},
            {"appid": 239200, "name": "Amnesia: A Machine for Pigs", "owner": "alice"},
            {"appid": 620, "name": "Portal 2", "owner": "bob"},
        ]

        messages = library.format_notifications(events, {"alice": "Alice", "bob": "Bob"}, "en")

        self.assertEqual(len(messages), 2)
        self.assertIn("Alice", messages[0])
        self.assertIn('1. <a href="https://store.steampowered.com/app/1944430/">Amnesia: The Bunker</a>', messages[0])
        self.assertIn(
            '2. <a href="https://store.steampowered.com/app/239200/">Amnesia: A Machine for Pigs</a>', messages[0]
        )
        self.assertIn("Bob", messages[1])
        self.assertIn('1. <a href="https://store.steampowered.com/app/620/">Portal 2</a>', messages[1])
        self.assertNotIn("\nhttps://", "\n".join(messages))

    def test_titles_and_owner_names_are_escaped_and_missing_names_fall_back(self):
        event = {"appid": 620, "name": '<Portal & "Friends">', "owner": "alice"}
        for lang, added in (("en", "added"), ("ru", "добавил(а)")):
            with self.subTest(lang=lang):
                message = library.format_notifications([event], {"alice": "<Alice & Bob>"}, lang)[0]
                self.assertIn("&lt;Alice &amp; Bob&gt;", message)
                self.assertIn("&lt;Portal &amp; &quot;Friends&quot;&gt;", message)
                self.assertIn(added, message)
        message = library.format_notifications([event], {}, "en")[0]
        self.assertIn("Steam user alice", message)
        self.assertEqual(library.format_notifications([], {}, "en"), [])

    def test_large_groups_split_without_losing_links_or_continuous_numbering(self):
        events = [{"appid": i, "name": f"Game {i}: " + "🎮 &<>" * 15, "owner": "alice"} for i in range(1, 121)]

        messages = library.format_notifications(events, {"alice": "<Alice>"}, "ru")

        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 4096)
            self.assertIn("&lt;Alice&gt;", message)
            self.assertEqual(message.count("<a "), message.count("</a>"))
        all_rows = re.findall(
            r'(?m)^(\d+)\. <a href="https://store.steampowered.com/app/(\d+)/">(.*?)</a>', "\n".join(messages)
        )
        self.assertEqual([(int(number), int(appid)) for number, appid, _ in all_rows], [(i, i) for i in range(1, 121)])
        self.assertEqual([html.unescape(title) for _, _, title in all_rows], [event["name"] for event in events])

    def test_removed_game_keeps_former_owners_and_old_snapshot_names(self):
        old = {
            "members": ["alice", "bob"],
            "names": {"alice": "<Alice>"},
            "apps": [
                {"appid": 1203620, "name": "Enshrouded & friends", "owners": ["alice", "bob"]},
            ],
        }
        new = {"members": ["alice", "bob"], "names": {"alice": "New name"}, "apps": []}

        events = library.family_events(old, new)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["owners"], ["alice", "bob"])
        self.assertEqual(events[0]["owner_names"], {"alice": "<Alice>"})
        for lang, former in (("en", "Previous owners (2)"), ("ru", "Раньше владели (2)")):
            with self.subTest(lang=lang):
                message = library.format_event(events[0], {}, [], lang)
                self.assertIn('href="https://store.steampowered.com/app/1203620/"', message)
                self.assertIn("Enshrouded &amp; friends", message)
                self.assertIn("&lt;Alice&gt;", message)
                self.assertIn("bob", message)
                self.assertIn(former, message)

    def test_pathological_labels_are_bounded_without_cutting_links_or_html_entities(self):
        event = {"appid": 620, "name": "<🎮&>" * 2000, "owner": "alice"}
        message = library.format_notifications([event], {"alice": "<Owner &>" * 1000}, "ru")[0]
        self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 4096)
        self.assertIn('1. <a href="https://store.steampowered.com/app/620/">', message)
        self.assertTrue(message.endswith("…</a>"))
        removed = {"type": "removed", "appid": 620, "game": event["name"], "owners": list("abcdef")}
        message = library.format_event(removed, dict.fromkeys("abcdef", "<Owner &>" * 1000), [], "en")
        self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 4096)
        self.assertIn("Previous owners (6)", message)

    def test_removed_single_owner_falls_back_to_id_and_partial_copy_loss_stays_silent(self):
        old = {
            "members": ["alice", "bob"],
            "apps": [
                {"appid": 620, "name": "Portal 2", "owners": ["alice", "bob"]},
            ],
        }
        new = {
            "members": ["alice", "bob"],
            "apps": [
                {"appid": 620, "name": "Portal 2", "owners": ["alice"]},
            ],
        }
        self.assertEqual(library.family_events(old, new), [])
        event = {"type": "removed", "appid": 620, "game": "Portal 2", "owners": ["alice"]}
        self.assertIn("Previous owner: Steam user alice", library.format_event(event, {}, [], "en"))

    def test_removed_legacy_event_and_other_event_rendering_remain_supported(self):
        event = {"type": "removed", "appid": 620, "game": "Portal 2"}
        self.assertIn("Previous owners unknown", library.format_event(event, {}, [], "en"))
        started = {"type": "started", "steamid": "alice", "game": "<Portal & Friends>"}
        self.assertEqual(
            library.format_event(started, {"alice": "<Alice>"}, [], "en"),
            "🎮 <Alice> started playing <Portal & Friends>",
        )
