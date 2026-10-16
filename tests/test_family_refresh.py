import asyncio
import unittest
from copy import deepcopy
from itertools import count
from unittest.mock import AsyncMock, patch

from bot.db import Db
from bot.family import Families

ALICE = "76561198000000001"
PORTAL = {"appid": 620, "name": "Portal 2", "owners": [ALICE], "acquired": 1}
CS = {"appid": 730, "name": "CS2", "owners": [ALICE], "acquired": 2}


def snapshot(apps=(), *, family_id="9", name="Fam"):
    return {
        "family_id": family_id,
        "name": name,
        "members": [ALICE],
        "names": {ALICE: "Alice"},
        "apps": list(apps),
    }


class FamilyRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rows = {
            family_id: {
                "family_id": family_id,
                "name": "Fam",
                "state": {"members": [ALICE], "ownership": {}},
                "notify_chat": -100,
                "language": "en",
                "alerts": {},
            }
            for family_id in ("9", "10")
        }

        async def family(family_id):
            return deepcopy(self.rows[family_id])

        async def update_family(family_id, **values):
            self.rows[family_id].update(deepcopy(values))

        pending, alert_ids = {}, count(1)

        async def observe_family(family_id, state, name, alerts):
            await update_family(family_id, state=state, name=name)
            for alert in alerts:
                alert_id = next(alert_ids)
                pending[alert_id] = {
                    **deepcopy(alert),
                    "id": alert_id,
                    "family_id": family_id,
                    "chat_id": self.rows[family_id]["notify_chat"],
                    "link_version": 0,
                }

        db = AsyncMock(spec=Db)
        db.family.side_effect = family
        db.update_family.side_effect = update_family
        db.observe_family.side_effect = observe_family
        db.pending_alerts.side_effect = lambda family_id, now: [
            alert for alert in pending.values() if alert["family_id"] == family_id
        ]
        db.delete_alert.side_effect = pending.pop
        db.family_accounts.return_value = []
        db.store_cache.return_value = {}
        self.bot = AsyncMock()
        self.families = Families(db, http=None, bot=self.bot)
        for family_id in self.rows:
            self.families.get(family_id).snapshot = snapshot(family_id=family_id)
        self.fetch = self.enterContext(
            patch.object(self.families, "_fetch", AsyncMock(return_value=snapshot([PORTAL])))
        )
        self.enterContext(patch("bot.family.update_store_cache", AsyncMock(return_value=False)))
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def start_refresh(self, family_id="9"):
        started = asyncio.Event()

        async def run():
            started.set()
            await self.families.refresh(family_id)

        task = asyncio.create_task(run())
        self.tasks.append(task)
        # The refresh runs through synchronous fake I/O until it reaches a real barrier or lock wait.
        await asyncio.wait_for(started.wait(), timeout=2)
        return task

    async def finish(self, *tasks):
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

    async def test_overlap_during_notification_announces_acquisition_once(self):
        sending, release = asyncio.Event(), asyncio.Event()

        async def send_message(*args, **kwargs):
            sending.set()
            await release.wait()

        self.bot.send_message.side_effect = send_message
        first = await self.start_refresh()
        await asyncio.wait_for(sending.wait(), timeout=2)
        second = await self.start_refresh()
        release.set()
        await self.finish(first, second)

        self.bot.send_message.assert_awaited_once_with(
            -100,
            "🎮 New Steam Family game\nPortal 2\nAdded by: Alice\nhttps://store.steampowered.com/app/620/",
            parse_mode=None,
        )
        self.assertEqual(
            self.rows["9"]["state"],
            {
                "members": [ALICE],
                "ownership": {"620": [ALICE]},
                "observed": {"members": [ALICE], "apps": [{"appid": 620, "name": "Portal 2"}]},
            },
        )

    async def test_older_fetch_cannot_overwrite_newer_snapshot_or_saved_name(self):
        fetching, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def fetch(family_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                fetching.set()
                await release.wait()
                return snapshot(name="Old")
            return snapshot([PORTAL], name="New")

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching.wait(), timeout=2)
        second = await self.start_refresh()
        self.assertFalse(second.done(), "a newer fetch must wait for the older refresh to finish")
        release.set()
        await self.finish(first, second)

        self.assertEqual(self.families.get("9").snapshot, snapshot([PORTAL], name="New"))
        self.assertEqual(self.rows["9"]["name"], "New")
        self.assertEqual(self.rows["9"]["state"]["ownership"], {"620": [ALICE]})

    async def test_burst_during_refresh_coalesces_into_one_trailing_fetch(self):
        fetching, release = asyncio.Event(), asyncio.Event()

        async def fetch(family_id):
            if self.fetch.await_count == 1:
                fetching.set()
                await release.wait()
            return snapshot([PORTAL])

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching.wait(), timeout=2)
        second, third = await self.start_refresh(), await self.start_refresh()
        self.assertEqual(self.fetch.await_count, 1)
        release.set()
        await self.finish(first, second, third)
        self.assertEqual(self.fetch.await_count, 2)
        self.bot.send_message.assert_awaited_once()

    async def test_different_family_completes_while_first_family_is_fetching(self):
        fetching, release = asyncio.Event(), asyncio.Event()

        async def fetch(family_id):
            if family_id == "9":
                fetching.set()
                await release.wait()
            return snapshot([PORTAL], family_id=family_id)

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching.wait(), timeout=2)
        other = await self.start_refresh("10")
        await self.finish(other)
        self.assertFalse(first.done())
        self.assertEqual(self.rows["10"]["state"]["ownership"], {"620": [ALICE]})
        release.set()
        await self.finish(first)

    async def test_failure_releases_lock_and_queued_refresh_retries(self):
        fetching, release = asyncio.Event(), asyncio.Event()

        async def fetch(family_id):
            if self.fetch.await_count == 1:
                fetching.set()
                await release.wait()
                raise OSError("Steam unavailable")
            return snapshot([PORTAL])

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching.wait(), timeout=2)
        second = await self.start_refresh()
        self.assertFalse(second.done())
        release.set()
        with self.assertRaisesRegex(OSError, "Steam unavailable"):
            await asyncio.wait_for(first, timeout=2)
        await self.finish(second)
        self.assertEqual(self.rows["9"]["state"]["ownership"], {"620": [ALICE]})

    async def test_active_cancellation_releases_lock_and_queued_refresh_retries(self):
        fetching, release = asyncio.Event(), asyncio.Event()

        async def fetch(family_id):
            if self.fetch.await_count == 1:
                fetching.set()
                await release.wait()
            return snapshot([PORTAL])

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching.wait(), timeout=2)
        second = await self.start_refresh()
        self.assertFalse(second.done())
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await self.finish(second)
        self.assertEqual(self.rows["9"]["state"]["ownership"], {"620": [ALICE]})

    async def test_canceling_waiter_does_not_block_later_request(self):
        fetching, release = asyncio.Event(), asyncio.Event()

        async def fetch(family_id):
            if self.fetch.await_count == 1:
                fetching.set()
                await release.wait()
            return snapshot([PORTAL])

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching.wait(), timeout=2)
        canceled = await self.start_refresh()
        canceled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await canceled
        later = await self.start_refresh()
        self.assertEqual(self.fetch.await_count, 1)
        release.set()
        await self.finish(first, later)
        self.assertEqual(self.fetch.await_count, 2)
        self.assertEqual(self.rows["9"]["state"]["ownership"], {"620": [ALICE]})

    async def test_request_during_trailing_fetch_gets_another_fresh_pass(self):
        fetching = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]

        async def fetch(family_id):
            call = self.fetch.await_count
            if call <= 2:
                fetching[call - 1].set()
                await release[call - 1].wait()
            return snapshot([PORTAL] if call < 3 else [PORTAL, CS])

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching[0].wait(), timeout=2)
        second = await self.start_refresh()
        release[0].set()
        await asyncio.wait_for(fetching[1].wait(), timeout=2)
        third = await self.start_refresh()
        self.assertEqual(self.fetch.await_count, 2)
        release[1].set()
        await self.finish(first, second, third)
        self.assertEqual(self.fetch.await_count, 3)
        self.assertEqual(self.families.get("9").snapshot["apps"], [PORTAL, CS])
        self.assertEqual(self.rows["9"]["state"]["ownership"], {"620": [ALICE], "730": [ALICE]})

    async def test_failed_trailing_pass_does_not_coalesce_away_queued_retry(self):
        fetching, release = asyncio.Event(), asyncio.Event()

        async def fetch(family_id):
            if self.fetch.await_count == 1:
                fetching.set()
                await release.wait()
                return snapshot([PORTAL])
            if self.fetch.await_count == 2:
                raise OSError("Trailing fetch failed")
            return snapshot([PORTAL, CS])

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching.wait(), timeout=2)
        second, third = await self.start_refresh(), await self.start_refresh()
        release.set()
        await self.finish(first)
        with self.assertRaisesRegex(OSError, "Trailing fetch failed"):
            await asyncio.wait_for(second, timeout=2)
        await self.finish(third)
        self.assertEqual(self.fetch.await_count, 3)
        self.assertEqual(self.families.get("9").snapshot["apps"], [PORTAL, CS])
        self.assertEqual(self.rows["9"]["state"]["ownership"], {"620": [ALICE], "730": [ALICE]})

    async def test_no_snapshot_burst_coalesces_without_changing_cache_or_state(self):
        fetching, release = asyncio.Event(), asyncio.Event()
        before = deepcopy(self.rows["9"])

        async def fetch(family_id):
            if self.fetch.await_count == 1:
                fetching.set()
                await release.wait()
            return None

        self.fetch.side_effect = fetch
        first = await self.start_refresh()
        await asyncio.wait_for(fetching.wait(), timeout=2)
        second, third = await self.start_refresh(), await self.start_refresh()
        release.set()
        await self.finish(first, second, third)
        self.assertEqual(self.fetch.await_count, 2)
        self.assertEqual(self.families.get("9").snapshot, snapshot())
        self.assertEqual(self.rows["9"], before)
        self.bot.send_message.assert_not_awaited()
