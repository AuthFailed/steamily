import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from bot.dialogs.menu import set_publication, settings_getter
from bot.i18n import LANGUAGES, t

STEAM, FAMILY, TELEGRAM = "76561198000000001", "9", 100
VERSION, NEW_VERSION = "d27386c5-6418-457c-8aa0-b94c46a42285", "91d846c7-7b1b-4b23-8bc7-55735a1dc148"


def account(**changes):
    return {
        "telegram_id": TELEGRAM,
        "steam_id": STEAM,
        "family_id": FAMILY,
        "owner_telegram_id": None,
        "status": "approved",
        "refresh_token": b"token",
        "publish_presence": False,
        "publish_telegram": False,
        "publication_version": VERSION,
        **changes,
    }


class PublicationSettingsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = SimpleNamespace(
            account=AsyncMock(return_value=account()),
            set_publication=AsyncMock(return_value=True),
            notify_requests=AsyncMock(return_value=True),
        )
        self.family = SimpleNamespace(lock=asyncio.Lock())
        self.families = SimpleNamespace(
            get=MagicMock(return_value=self.family),
            _fetch=AsyncMock(return_value={"family_id": FAMILY, "members": [STEAM]}),
        )
        self.manager = SimpleNamespace(
            middleware_data={"db": self.db, "families": self.families, "lang": "en"}, dialog_data={}
        )
        self.callback = SimpleNamespace(from_user=SimpleNamespace(id=TELEGRAM), answer=AsyncMock())

    async def click(self, item_id):
        await set_publication(self.callback, None, self.manager, item_id)

    async def test_settings_offer_independent_default_off_choices_in_both_languages(self):
        for lang in LANGUAGES:
            data = await settings_getter(self.manager, account(), self.db, lang=lang)
            self.assertTrue(data["can_publish"])
            self.assertEqual(
                data["publication_items"],
                [
                    (f"p:{VERSION}:1", t(lang, "btn_publish_presence_off")),
                    (f"t:{VERSION}:1", t(lang, "btn_publish_telegram_off")),
                ],
            )
            self.assertTrue(t(lang, "publication_desc"))
        data = await settings_getter(self.manager, account(publish_presence=True), self.db)
        self.assertEqual(data["publication_items"][0][0], f"p:{VERSION}:0")
        self.assertEqual(data["publication_items"][1][0], f"t:{VERSION}:1")
        self.assertLessEqual(len(f"12345678:publication:p:{VERSION}:1".encode()), 64)

    async def test_pending_blocked_logged_out_and_expired_accounts_have_no_controls(self):
        for row in (None, account(status="pending"), account(status="blocked"), account(refresh_token=None)):
            data = await settings_getter(self.manager, row, self.db)
            self.assertFalse(data["can_publish"])
            self.assertEqual(data["publication_items"], [])

    async def test_settings_keep_original_dialog_family_binding(self):
        await settings_getter(self.manager, account(), self.db)
        self.assertEqual(self.manager.dialog_data["family_id"], FAMILY)
        await settings_getter(self.manager, account(family_id="other-family"), self.db)
        self.assertEqual(self.manager.dialog_data["family_id"], FAMILY)

    async def test_enable_checks_fresh_membership_and_saves_only_sender_choice(self):
        for code, setting in (("p", "publish_presence"), ("t", "publish_telegram")):
            await self.click(f"{code}:{VERSION}:1")
            self.families._fetch.assert_awaited_with(FAMILY)
            self.db.account.assert_awaited_with(TELEGRAM)
            self.db.set_publication.assert_awaited_with(
                TELEGRAM, STEAM, FAMILY, setting, True, expected_version=VERSION
            )
        self.callback.answer.assert_not_awaited()

    async def test_enable_holds_family_lock_during_membership_fetch(self):
        async def fetch(family_id):
            self.assertEqual(family_id, FAMILY)
            self.assertTrue(self.family.lock.locked())
            return {"family_id": FAMILY, "members": [STEAM]}

        self.families._fetch.side_effect = fetch
        await self.click(f"p:{VERSION}:1")
        self.families.get.assert_called_once_with(FAMILY)
        self.assertFalse(self.family.lock.locked())
        self.callback.answer.assert_not_awaited()

    async def test_disabling_does_not_depend_on_steam_availability(self):
        self.families._fetch.side_effect = aiohttp.ClientError()
        await self.click(f"p:{VERSION}:0")
        self.families._fetch.assert_not_awaited()
        self.families.get.assert_not_called()
        self.db.set_publication.assert_awaited_once_with(
            TELEGRAM, STEAM, FAMILY, "publish_presence", False, expected_version=VERSION
        )

    async def test_malformed_and_old_binding_callbacks_cannot_write(self):
        for item_id in (
            "p",
            f"unknown:{VERSION}:1",
            f"p:{VERSION}:yes",
            f"p:{NEW_VERSION}:1",
            f"p:{STEAM}:{FAMILY}:1",
        ):
            with self.subTest(item_id=item_id):
                await self.click(item_id)
        self.db.set_publication.assert_not_awaited()
        self.families._fetch.assert_not_awaited()

    async def test_forged_sender_and_unapproved_accounts_cannot_write(self):
        for row in (
            None,
            account(telegram_id=200),
            account(status="pending"),
            account(status="blocked"),
            account(refresh_token=None),
        ):
            self.db.account.return_value = row
            await self.click(f"p:{VERSION}:1")
        self.db.set_publication.assert_not_awaited()
        self.families._fetch.assert_not_awaited()

    async def test_departure_family_change_and_failed_fresh_reads_fail_closed(self):
        for snapshot in (None, {"family_id": FAMILY, "members": []}, {"family_id": "other", "members": [STEAM]}):
            self.families._fetch.return_value = snapshot
            await self.click(f"p:{VERSION}:1")
        for error in (aiohttp.ClientError(), TimeoutError(), RuntimeError()):
            self.families._fetch.side_effect = error
            await self.click(f"p:{VERSION}:1")
        self.db.set_publication.assert_not_awaited()
        self.assertEqual(self.callback.answer.await_count, 6)

    async def test_old_opt_in_cannot_enable_after_same_account_reset(self):
        self.db.account.return_value = account(publication_version=NEW_VERSION)
        await self.click(f"p:{VERSION}:1")
        self.db.set_publication.assert_not_awaited()
        self.families._fetch.assert_not_awaited()
        self.callback.answer.assert_awaited_once()

    async def test_db_guards_original_version_during_inflight_reset_relink_or_delete(self):
        for current in (
            None,
            account(steam_id="relinked", publication_version=NEW_VERSION),
            account(family_id="other-family", publication_version=NEW_VERSION),
            account(publication_version=NEW_VERSION),
        ):
            with self.subTest(current=current):
                self.db.account.side_effect = [account(), current]
                self.db.set_publication.reset_mock()
                self.db.set_publication.return_value = False
                self.callback.answer.reset_mock()
                await self.click(f"p:{VERSION}:1")
                self.db.set_publication.assert_awaited_once_with(
                    TELEGRAM, STEAM, FAMILY, "publish_presence", True, expected_version=VERSION
                )
                self.callback.answer.assert_awaited_once()
                self.assertEqual(self.manager.middleware_data["account"], current)
