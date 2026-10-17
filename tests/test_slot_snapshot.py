import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.steam import fetch_family_snapshot, parse_snapshot

FIXTURES = Path(__file__).parent / "fixtures/steam"
MEMBERS = [f"7656119800000000{i}" for i in range(1, 6)]


def recorded_family():
    return json.loads((FIXTURES / "family_calendar_recorded.json").read_text())


class SlotSnapshotTest(unittest.TestCase):
    def test_recorded_family_preserves_observed_calendar_values(self):
        snapshot = parse_snapshot(recorded_family(), {"response": {"apps": []}}, {})

        self.assertIn("calendar", snapshot)
        self.assertEqual(
            snapshot["calendar"],
            {
                "checked_at": None,
                "member_cooldowns": dict.fromkeys(MEMBERS, 0),
                "free_spots": 1,
                "slot_cooldown_remaining_seconds": 0,
                "slot_cooldown_overrides": 0,
            },
        )

    def test_missing_optional_calendar_values_stay_missing(self):
        family = {"response": {"family_groupid": "42", "family_group": {"members": [{"steamid": MEMBERS[0]}]}}}

        snapshot = parse_snapshot(family, {"response": {"apps": []}}, {})

        self.assertIn("calendar", snapshot)
        self.assertEqual(snapshot["calendar"], {"checked_at": None, "member_cooldowns": {}})

    def test_synthetic_nonzero_cooldowns_remain_raw_seconds(self):
        family = recorded_family()
        group = family["response"]["family_group"]
        group["members"][0]["cooldown_seconds_remaining"] = 86_401
        del group["members"][1]["cooldown_seconds_remaining"]
        group["slot_cooldown_remaining_seconds"] = 172_801
        group["slot_cooldown_overrides"] = 2

        snapshot = parse_snapshot(family, {"response": {"apps": []}}, {})

        self.assertIn("calendar", snapshot)
        self.assertEqual(snapshot["calendar"]["member_cooldowns"][MEMBERS[0]], 86_401)
        self.assertNotIn(MEMBERS[1], snapshot["calendar"]["member_cooldowns"])
        self.assertEqual(snapshot["calendar"]["slot_cooldown_remaining_seconds"], 172_801)
        self.assertEqual(snapshot["calendar"]["slot_cooldown_overrides"], 2)


class SlotSnapshotFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_checked_at_uses_family_response_time_before_slower_requests(self):
        now = 0

        async def get_response(http, method, params):
            nonlocal now
            if method == "IFamilyGroupsService/GetFamilyGroupForUser/v1":
                now = 2_000_000_000.0
                return recorded_family()
            now += 60
            if method == "IFamilyGroupsService/GetSharedLibraryApps/v1":
                return {"response": {"apps": []}}
            return {"response": {}}

        with (
            patch("bot.steam.get_json", side_effect=get_response),
            patch("bot.steam.post_json", return_value={"response": {}}),
            patch("bot.steam.time.time", side_effect=lambda: now),
        ):
            snapshot = await fetch_family_snapshot(None, "fixture-access")

        self.assertIn("calendar", snapshot)
        self.assertEqual(snapshot["calendar"]["checked_at"], 2_000_000_000.0)
