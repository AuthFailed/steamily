import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.dialogs.games import game_getter
from bot.family import Family
from bot.i18n import LANGUAGES, t
from bot.inline import card_text


class InlineSharingCardTest(unittest.TestCase):
    def test_family_cards_distinguish_owned_shareable_and_available_copies(self):
        for reason, shareable in ((0, 2), (1, 0), (999, 0)):
            with self.subTest(reason=reason):
                text = card_text("en", "Game", "", None, ["Alice", "Bob"], "https://store/", reason)
                self.assertIn("Owned copies: 2", text)
                self.assertIn(f"Shareable copies: {shareable}", text)
                self.assertIn("Available now: unknown", text)
                self.assertNotIn("Your access:", text)

    def test_store_only_card_does_not_imply_family_copy_availability(self):
        text = card_text("en", "Game", "", None, [], "https://store/", 0)
        self.assertIn("Nobody in the family has it", text)
        self.assertNotIn("Owned copies:", text)
        self.assertNotIn("Shareable copies:", text)
        self.assertNotIn("Available now:", text)


class SharingCardTest(unittest.IsolatedAsyncioTestCase):
    async def test_opinion_rendering_uses_current_consent_membership_and_response_revision(self):
        family = Family("9")
        family.snapshot = {
            "members": ["alice", "bob"],
            "names": {"alice": "Alice", "bob": "Bob <name>"},
            "apps": [{"appid": 620, "name": "Portal 2", "owners": ["alice"]}],
        }
        viewer = {
            "telegram_id": 1,
            "steam_id": "alice",
            "family_id": "9",
            "country": "US",
            "status": "approved",
            "refresh_token": b"token",
            "game_response_version": "viewer",
        }
        voter = {
            **viewer,
            "telegram_id": 2,
            "steam_id": "bob",
            "game_response_version": "vote",
            "publish_telegram": True,
            "tg_username": "bob_tg",
            "tg_name": "Bob",
        }
        manager = SimpleNamespace(dialog_data={"appid": 620}, middleware_data={"account": viewer, "family": family})
        row = {
            "telegram_id": 2,
            "steam_id": "bob",
            "appid": 620,
            "response": "recommend",
            "named": True,
            "game_response_version": "vote",
        }
        for change in (
            "none",
            "optout",
            "revision",
            "blocked",
            "deleted",
            "departed",
            "viewer_deleted",
            "viewer_blocked",
        ):
            with self.subTest(change=change):
                family.snapshot["members"] = ["alice", "bob"]

                async def accounts(_, change=change):
                    current = dict(voter)
                    if change == "optout":
                        current["publish_telegram"] = False
                    elif change == "revision":
                        current["game_response_version"] = "cleared"
                    elif change == "blocked":
                        current["status"] = "blocked"
                    elif change == "departed":
                        family.snapshot["members"] = ["alice"]
                    reader = dict(viewer)
                    if change == "viewer_deleted":
                        return [current]
                    if change == "viewer_blocked":
                        reader["status"] = "blocked"
                    return [reader] if change == "deleted" else [reader, current]

                db = SimpleNamespace(
                    game_responses=AsyncMock(return_value=[row]),
                    game_marks=AsyncMock(return_value={}),
                    family_accounts=AsyncMock(side_effect=accounts),
                )
                data = await game_getter(manager, family, db, "en")
                if change.startswith("viewer_"):
                    self.assertEqual(data["details"], t("en", "response_unavailable"))
                    self.assertEqual(data["response_items"], [])
                    continue
                visible = change in ("none", "optout")
                self.assertIn(f"Recommend: {int(visible)}", data["details"])
                self.assertEqual("Bob &lt;name&gt;" in data["details"], visible)
                self.assertEqual("bob_tg" in data["details"], change == "none")

    async def test_opinion_buttons_fit_telegram_limit_in_both_languages(self):
        family = Family("9")
        family.snapshot = {
            "members": ["alice"],
            "names": {"alice": "Alice"},
            "apps": [{"appid": 4294967295, "name": "Game", "owners": ["alice"]}],
        }
        viewer = {
            "telegram_id": 1,
            "steam_id": "alice",
            "family_id": "9",
            "country": "US",
            "status": "approved",
            "refresh_token": b"token",
            "game_response_version": "d27386c5-6418-457c-8aa0-b94c46a42285",
        }
        manager = SimpleNamespace(
            dialog_data={"appid": 4294967295}, middleware_data={"account": viewer, "family": family}
        )
        db = SimpleNamespace(
            game_responses=AsyncMock(return_value=[]),
            game_marks=AsyncMock(return_value={}),
            family_accounts=AsyncMock(return_value=[viewer]),
        )
        for lang in LANGUAGES:
            data = await game_getter(manager, family, db, lang)
            self.assertIn(t(lang, "response_privacy"), data["details"])
            self.assertEqual(len(data["response_items"]), 5)
            for item, _ in data["response_items"]:
                self.assertLessEqual(len(f"12345678:vote:{item}".encode()), 64)

    async def card(self, reason, viewer="alice", lang="en", info=None):
        family = Family("9")
        if info is not None:
            family.stores["US"] = {"apps": {"620": info}}
        family.snapshot = {
            "members": ["alice", "bob", "carol"],
            "apps": [{"appid": 620, "name": "Portal 2", "owners": ["alice", "bob"], "exclude_reason": reason}],
            "names": {"alice": "Alice", "bob": "Bob"},
        }
        manager = SimpleNamespace(
            dialog_data={"appid": 620},
            middleware_data={
                "family": family,
                "account": {
                    "country": "US",
                    "steam_id": viewer,
                    "telegram_id": 1,
                    "family_id": "9",
                    "game_response_version": "version",
                    "status": "approved",
                    "refresh_token": b"token",
                },
            },
        )
        db = SimpleNamespace(
            family_accounts=AsyncMock(return_value=[manager.middleware_data["account"]]),
            game_responses=AsyncMock(return_value=[]),
            game_marks=AsyncMock(return_value={}),
        )
        return (await game_getter(manager, family, db, lang))["details"]

    async def test_card_distinguishes_known_modes_from_missing_and_legacy_details(self):
        for info, mode in (
            (None, "❔ Mode unavailable"),
            ({}, "❔ Mode unavailable"),
            ({"multiplayer": False}, "❔ Mode unavailable"),
            ({"play_mode": None}, "❔ Mode unavailable"),
            ({"play_mode": "singleplayer"}, "👤 Single-player"),
            ({"play_mode": "multiplayer"}, "👥 Multiplayer"),
        ):
            with self.subTest(info=info):
                details = await self.card(0, info=info)
                self.assertIn(mode, details.splitlines())
                for other in {"❔ Mode unavailable", "👤 Single-player", "👥 Multiplayer"} - {mode}:
                    self.assertNotIn(other, details)

    async def test_russian_card_shows_unknown_mode(self):
        details = await self.card(0, lang="ru")
        self.assertIn("❔ Режим игры неизвестен", details)
        self.assertNotIn("👤 Одиночная", details)

    async def test_card_explains_specific_modes_and_copy_assumptions(self):
        info = {
            "play_mode": "multiplayer",
            "categories": {"supported_player_categoryids": [1, 38, 36, 39], "feature_categoryids": [44]},
        }
        details = await self.card(0, info=info)
        for label in ("Online co-op", "Online PvP", "Local co-op/split-screen", "Remote Play Together"):
            self.assertIn(f"{label}: supported", details)
        self.assertIn("one owned or shareable copy per player", details)
        self.assertIn("everyone plays on the host's device", details)
        self.assertIn("guests join the host's stream", details)
        self.assertIn("player limit and input requirements", details)

    async def test_card_distinguishes_unlisted_modes_from_missing_collections(self):
        details = await self.card(0, info={"categories": {"supported_player_categoryids": []}})
        self.assertIn("Online co-op: not listed", details)
        self.assertIn("Remote Play Together: unknown", details)
        self.assertNotIn("one owned or shareable copy per player", details)

    async def test_russian_card_explains_remote_hosting(self):
        details = await self.card(0, lang="ru", info={"categories": {"feature_categoryids": [44]}})
        self.assertIn("Remote Play Together: поддерживается", details)
        self.assertIn("трансляции ведущего", details)

    async def test_excluded_card_keeps_ownership_separate_from_sharing_and_availability(self):
        details = await self.card(1)
        self.assertIn("Owned copies: 2", details)
        self.assertIn("Shareable copies: 0", details)
        self.assertIn("Your access: own copy", details)
        self.assertIn("Not shareable: excluded by the publisher", details)
        self.assertIn("Available now: unknown", details)
        self.assertIn("Alice", details)
        self.assertIn("Bob", details)

    async def test_nonowner_access_depends_on_sharing_eligibility(self):
        included = await self.card(0, viewer="carol")
        self.assertIn("Shareable copies: 2", included)
        self.assertIn("Your access: family sharing", included)
        excluded = await self.card(1, viewer="carol")
        self.assertIn("Your access: unavailable through family sharing", excluded)

    async def test_future_exclusion_is_safe_and_shows_its_code(self):
        details = await self.card(999, viewer="carol")
        self.assertIn("Shareable copies: 0", details)
        self.assertIn("Steam restriction (code 999)", details)
        self.assertIn("unavailable through family sharing", details)

    async def test_russian_card_explains_exclusion_and_owned_access(self):
        details = await self.card(4, lang="ru")
        self.assertIn("Собственных копий: 2", details)
        self.assertIn("Копий для семейного доступа: 0", details)
        self.assertIn("Ваша копия", details)
        self.assertIn("приватная лицензия", details)
        self.assertIn("Свободно сейчас: неизвестно", details)
