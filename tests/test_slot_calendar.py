import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.dialogs import family as screens
from bot.family import Family
from bot.i18n import t


class SlotCalendarTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.family = Family("42")
        self.family.snapshot = {
            "name": "Calendar family",
            "members": ["alice", "bob"],
            "names": {"alice": "Alice <adult>", "bob": "Bob"},
            "joined": {"alice": 1_700_000_000, "bob": 1_700_000_100},
            "pending_invites": [{"steamid": "invitee", "role": 2}],
            "free_spots": 1,
            "calendar": {
                "checked_at": 1_700_000_000,
                "free_spots": 1,
                "slot_cooldown_remaining_seconds": 86_400,
                "member_cooldowns": {"alice": 0, "bob": 172_800},
            },
        }

    async def render(self, lang="en"):
        data = await screens.slots_getter(self.family, lang)
        return data["slots_body"]

    async def test_dates_use_explicit_distinct_timers_and_response_time(self):
        text = await self.render()
        self.assertIn("2023-11-14 22:13:20 UTC", text)
        self.assertIn("2023-11-15 22:13:20 UTC", text)
        self.assertIn("Bob: until 2023-11-16 22:13:20 UTC", text)
        self.assertIn("Alice &lt;adult&gt;", text)
        self.assertIn("invitee", text)
        self.assertIn("Occupied: 2", text)
        self.assertIn("Total slots: 4", text)
        self.assertIn("Invited / reserved: 1", text)
        self.assertIn("Available for new members without an override: 0", text)
        self.assertNotIn("2024-11", text)

    async def test_free_places_are_available_only_with_an_explicit_clear_timer(self):
        calendar = self.family.snapshot["calendar"]
        calendar["slot_cooldown_remaining_seconds"] = 0
        self.assertIn("Available for new members without an override: 1", await self.render())
        calendar.pop("slot_cooldown_remaining_seconds")
        self.assertIn("Available for new members without an override: unknown", await self.render())
        calendar.update(slot_cooldown_remaining_seconds=86_400, slot_cooldown_overrides=2)
        text = await self.render()
        self.assertIn("Available for new members without an override: 0", text)
        self.assertIn("Cooldown overrides: 2", text)

    async def test_missing_dates_stay_unknown_including_legacy_snapshots(self):
        for calendar in ({}, {"checked_at": 1_700_000_000, "member_cooldowns": {"alice": 86_400}}):
            with self.subTest(calendar=calendar):
                self.family.snapshot["calendar"] = calendar
                text = await self.render()
                self.assertIn("Bob: unknown", text)
                self.assertNotIn("2024-11", text)
        self.family.snapshot.pop("calendar")
        self.assertIn("unknown", await self.render())

    async def test_full_family_has_occupied_places_and_no_inferred_capacity(self):
        self.family.snapshot["members"] = [str(index) for index in range(6)]
        self.family.snapshot["pending_invites"] = []
        self.family.snapshot["calendar"].update(free_spots=0, slot_cooldown_remaining_seconds=0)
        text = await self.render()
        self.assertIn("Occupied: 6", text)
        self.assertIn("Total slots: 6", text)
        self.assertIn("Available for new members without an override: 0", text)
        self.assertIn("no active cooldown reported", text)

    async def test_invalid_optional_values_cannot_become_verified_dates(self):
        for value in (-1, True, "86400", 10**30):
            with self.subTest(value=value):
                self.family.snapshot["calendar"].update(
                    slot_cooldown_remaining_seconds=value, member_cooldowns={"bob": value}
                )
                self.assertIn("Bob: unknown", await self.render())

    async def test_russian_view_keeps_timezone_and_member_restrictions(self):
        text = await self.render("ru")
        self.assertIn("2023-11-15 22:13:20 UTC", text)
        self.assertIn("Ограничения участников", text)
        self.assertIn("Bob: до 2023-11-16 22:13:20 UTC", text)

    async def test_overview_does_not_treat_cooling_places_as_removed_slots(self):
        db = SimpleNamespace(owner_steam_id=AsyncMock(return_value="alice"), family_accounts=AsyncMock(return_value=[]))
        account = {"steam_id": "alice", "status": "approved", "telegram_id": 1, "owner_telegram_id": 1}
        data = await screens.info_getter(None, self.family, account, db, "en")
        self.assertNotIn("of 3", t("en", "family_info", **data))
