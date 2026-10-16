import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.family import Families, person_link
from bot.inline import search


class PublicationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.accounts = {
            "a": {
                "telegram_id": 1,
                "steam_id": "a",
                "family_id": "9",
                "status": "approved",
                "refresh_token": b"token",
                "created_at": 1,
                "publication_version": "alice-original",
                "publish_presence": True,
                "publish_telegram": False,
                "tg_username": "secret_handle",
                "tg_name": "Secret name",
            },
            "b": {
                "telegram_id": 2,
                "steam_id": "b",
                "family_id": "9",
                "status": "approved",
                "refresh_token": b"token",
                "created_at": 2,
                "publication_version": "bob-original",
                "publish_presence": False,
                "publish_telegram": True,
                "tg_username": None,
                "tg_name": "Bob Telegram",
            },
        }
        self.row = {
            "family_id": "9",
            "notify_chat": -100,
            "language": "en",
            "alerts": {"online": True, "playing": True},
        }
        self.db = AsyncMock()
        self.db.family.return_value = self.row
        self.db.family_accounts.side_effect = lambda _: [dict(a) for a in self.accounts.values()]
        self.db.account.side_effect = lambda tid: next(
            (dict(a) for a in self.accounts.values() if a["telegram_id"] == tid), None
        )
        self.bot = AsyncMock()
        self.families = Families(self.db, None, self.bot)
        self.family = self.families.get("9")
        self.family.snapshot = {
            "family_id": "9",
            "members": ["a", "b", "unregistered"],
            "names": {"a": "Alice", "b": "Bob", "unregistered": "Relative"},
            "apps": [],
        }
        self.families._fetch = AsyncMock(side_effect=lambda _: self.family.snapshot)
        self.families.any_token = AsyncMock(return_value="access")
        self.families.presence["9"] = {m: {"online": False} for m in self.family.members}

    def test_hidden_identity_never_uses_username_name_or_numeric_fallback(self):
        for username in ("secret_handle", None):
            with self.subTest(username=username):
                self.accounts["a"]["tg_username"] = username
                link = person_link(self.family, "a", self.accounts)
                self.assertIn("Alice</a>", link)
                self.assertNotIn("Telegram", link)
                self.assertNotIn("secret_handle", link)
                self.assertNotIn("Secret name", link)
                self.assertNotIn("tg://", link)

    def test_only_current_active_members_may_publish_identity(self):
        self.assertIn("Bob Telegram", person_link(self.family, "b", self.accounts))
        for fields in ({"status": "pending"}, {"refresh_token": None}, {"family_id": "other"}):
            with self.subTest(fields=fields):
                accounts = {"b": {**self.accounts["b"], **fields}}
                self.assertNotIn("Bob Telegram", person_link(self.family, "b", accounts))
        self.family.snapshot["members"].remove("b")
        self.assertNotIn("Bob Telegram", person_link(self.family, "b", self.accounts))

    async def test_presence_only_announces_opted_in_member(self):
        presence = {m: {"online": True, "game": 620, "game_name": "Portal"} for m in self.family.members}
        with patch("bot.family.fetch_presence", AsyncMock(return_value=presence)):
            await self.families.check_presence("9")
        messages = [call.args[1] for call in self.bot.send_message.await_args_list]
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("Alice" in text for text in messages))
        self.assertTrue(all("Bob" not in text and "Relative" not in text for text in messages))

    async def test_queued_presence_rechecks_optout_departure_deletion_and_replacement(self):
        for change in ("optout", "departure", "delete", "replacement", "reconsent", "unlink_group", "disabled_alert"):
            with self.subTest(change=change):
                self.setUp()

                async def changed_presence(*args, change=change):
                    if change == "optout":
                        self.accounts["a"]["publish_presence"] = False
                    elif change == "departure":
                        self.family.snapshot["members"].remove("a")
                    elif change == "delete":
                        del self.accounts["a"]
                    elif change == "replacement":
                        self.accounts["a"]["created_at"] = 3
                        self.accounts["a"]["publication_version"] = "replacement"
                    elif change == "reconsent":
                        self.accounts["a"]["publication_version"] = "new-consent"
                    elif change == "unlink_group":
                        self.row["notify_chat"] = None
                    else:
                        self.row["alerts"]["online"] = False
                    return {"a": {"online": True}}

                with patch("bot.family.fetch_presence", changed_presence):
                    await self.families.check_presence("9")
                self.assertEqual(self.bot.send_message.await_count, 0)

    async def test_delivery_rechecks_consent_after_fresh_membership_request(self):
        async def fresh(*args):
            self.accounts["a"]["publish_presence"] = False
            return self.family.snapshot

        self.families._fetch.side_effect = fresh
        with patch("bot.family.fetch_presence", AsyncMock(return_value={"a": {"online": True}})):
            await self.families.check_presence("9")
        self.assertEqual(self.bot.send_message.await_count, 0)

    async def test_delivery_fails_closed_when_membership_cannot_be_verified(self):
        self.families._fetch.return_value = None
        self.families._fetch.side_effect = None
        with patch("bot.family.fetch_presence", AsyncMock(return_value={"a": {"online": True}})):
            await self.families.check_presence("9")
        self.assertEqual(self.bot.send_message.await_count, 0)

    async def test_inline_cards_use_latest_identity_consent_without_telegram_cache(self):
        self.db.language.return_value = "en"
        self.accounts["a"].update(country="US", owner_telegram_id=1, publish_telegram=True)
        self.family.snapshot["apps"] = [{"appid": 620, "name": "Portal", "owners": ["a", "b"]}]
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), query="Portal", answer=AsyncMock())

        async def details(*args):
            self.accounts["a"]["publish_telegram"] = False
            return {}  # fallback cards must obey the same preference

        with patch("bot.inline.search_store", AsyncMock(return_value=[])), patch("bot.inline.store_cards", details):
            await search(query, self.db, self.families)
        answer = query.answer.await_args
        text = answer.args[0][0].input_message_content.message_text
        self.assertNotIn("secret_handle", text)
        self.assertIn("Bob Telegram", text)
        self.assertEqual(answer.kwargs["cache_time"], 0)
        self.assertTrue(answer.kwargs["is_personal"])

    async def test_inline_search_rechecks_sender_deleted_during_store_request(self):
        self.db.language.return_value = "en"
        self.accounts["a"].update(country="US", owner_telegram_id=1)
        self.family.snapshot["apps"] = [{"appid": 620, "name": "Portal", "owners": ["a"]}]
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), query="Portal", answer=AsyncMock())

        async def details(*args):
            del self.accounts["a"]
            return {}

        with patch("bot.inline.search_store", AsyncMock(return_value=[])), patch("bot.inline.store_cards", details):
            await search(query, self.db, self.families)
        self.assertEqual(query.answer.await_args.args[0], [])

    async def test_inline_search_rechecks_sender_in_final_account_collection(self):
        self.db.language.return_value = "en"
        self.accounts["a"].update(country="US", owner_telegram_id=1)
        self.family.snapshot["apps"] = [{"appid": 620, "name": "Portal", "owners": ["b"]}]
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), query="Portal", answer=AsyncMock())

        async def accounts(*args):
            del self.accounts["a"]
            return list(self.accounts.values())

        self.db.family_accounts.side_effect = accounts
        with (
            patch("bot.inline.search_store", AsyncMock(return_value=[])),
            patch("bot.inline.store_cards", AsyncMock(return_value={})),
        ):
            await search(query, self.db, self.families)
        self.assertEqual(query.answer.await_args.args[0], [])
