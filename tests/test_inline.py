import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from bot.family import Family
from bot.inline import search

ALICE = "76561198000000001"


def response(data, status=200):
    request = MagicMock()
    request.__aenter__.return_value.status = status
    request.__aenter__.return_value.json.return_value = data
    return request


class InlineSearchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.query = SimpleNamespace(
            from_user=SimpleNamespace(id=42, language_code="en"), query="portal", answer=AsyncMock()
        )
        account = {
            "telegram_id": 42,
            "steam_id": ALICE,
            "owner_telegram_id": 42,
            "status": "approved",
            "family_id": "9",
            "country": "KZ",
            "refresh_token": b"token",
            "created_at": 1,
            "publish_telegram": False,
            "publish_presence": False,
        }
        self.db = SimpleNamespace(
            language=AsyncMock(return_value="en"),
            account=AsyncMock(return_value=account),
            family_accounts=AsyncMock(return_value=[account]),
        )
        family = Family("9")
        family.snapshot = {
            "members": [ALICE],
            "apps": [{"appid": 620, "name": "Portal 2", "owners": [ALICE]}],
            "names": {ALICE: "Alice"},
        }
        self.http = MagicMock()
        self.families = SimpleNamespace(http=self.http, items={"9": family})
        self.cards = {
            "response": {
                "store_items": [
                    {
                        "appid": 620,
                        "name": "Portal 2",
                        "basic_info": {"short_description": "Co-op puzzles"},
                        "best_purchase_option": {"formatted_final_price": "300₸"},
                    }
                ]
            }
        }

    async def answer(self):
        await search(self.query, self.db, self.families)
        self.query.answer.assert_awaited_once()
        self.assertEqual(self.query.answer.await_args.kwargs, {"cache_time": 0, "is_personal": True})
        results = self.query.answer.await_args.args[0]
        self.assertEqual([result.id for result in results], ["620"])
        self.assertEqual(results[0].title, "Portal 2")
        text = results[0].input_message_content.message_text
        self.assertIn(f'<a href="https://steamcommunity.com/profiles/{ALICE}/">Alice</a>', text)
        self.assertIn("https://store.steampowered.com/app/620/", text)
        return text

    async def test_store_search_failures_still_return_enriched_family_game(self):
        for failure in ("connection", "timeout", "invalid_json", "invalid_shape", "non_200"):
            with self.subTest(failure=failure):
                store = response({"items": []})
                if failure == "connection":
                    store.__aenter__.side_effect = aiohttp.ClientConnectionError("network down")
                elif failure == "timeout":
                    store.__aenter__.side_effect = TimeoutError()
                elif failure == "invalid_json":
                    store.__aenter__.return_value.json.side_effect = json.JSONDecodeError("invalid", "<html>", 0)
                elif failure == "invalid_shape":
                    store.__aenter__.return_value.json.return_value = None
                else:
                    store.__aenter__.return_value.status = 503
                self.http.get.side_effect = [store, response(self.cards)]
                self.query.answer.reset_mock()

                if failure == "non_200":
                    text = await self.answer()
                else:
                    with self.assertLogs("bot.inline", level="WARNING"):
                        text = await self.answer()

                self.assertIn("300₸", text)
                self.assertIn("Co-op puzzles", text)

    async def test_search_and_details_failures_keep_known_family_data_and_safe_logs(self):
        self.http.get.side_effect = aiohttp.ClientConnectionError("https://steam/?access_token=secret-token")

        with self.assertLogs("bot.inline", level="WARNING") as logs:
            text = await self.answer()

        self.assertIn("Price unknown", text)
        self.assertIn("Store search", "\n".join(logs.output))
        self.assertIn("Store details", "\n".join(logs.output))
        self.assertIn("ClientConnectionError", "\n".join(logs.output))
        self.assertNotIn("secret-token", "\n".join(logs.output))

    async def test_hanging_search_and_details_are_bounded_and_cancelled(self):
        finished = []

        async def hang(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(True)

        with (
            patch("bot.inline.STORE_TIMEOUT", 0.01),
            patch("bot.inline.search_store", hang),
            patch("bot.inline.store_cards", hang),
            self.assertLogs("bot.inline", level="WARNING"),
        ):
            async with asyncio.timeout(0.5):
                await self.answer()

        self.assertEqual(len(finished), 2)

    async def test_cancellation_propagates(self):
        self.http.get.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await search(self.query, self.db, self.families)
        self.query.answer.assert_not_awaited()
