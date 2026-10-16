import asyncio
import unittest
from unittest.mock import AsyncMock, call, patch

from bot.steam_auth import SteamAuth, SteamError, TokenRejected

ALICE = "76561198000000001"
NOW = 2_000_000_000.0
DAY = 86_400
ACCESS = "x.eyJleHAiOiAyMDAwMDAzNjAwfQ.y"  # {"exp": NOW + 3600}


class SteamAuthTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.persist = AsyncMock()
        self.auth = SteamAuth(None, "refresh", ALICE, self.persist)
        self.auth.last_renewal = NOW - DAY
        self.call = AsyncMock(return_value={1: b"access"})
        call_patch = patch("bot.steam_auth.call", self.call)
        time_patch = patch("bot.steam_auth.time.time", return_value=NOW)
        call_patch.start()
        time_patch.start()
        self.addCleanup(call_patch.stop)
        self.addCleanup(time_patch.stop)

    async def test_renewal_is_requested_at_the_daily_boundary(self):
        self.assertEqual(await self.auth.get_access_token(), "access")
        self.call.assert_awaited_once_with(
            None,
            "GenerateAccessTokenForApp",
            [(1, "refresh"), (2, ("fixed64", int(ALICE))), (3, 1)],
        )
        self.assertEqual(self.auth.last_renewal, NOW)
        self.persist.assert_awaited_once_with("refresh", NOW)

    async def test_renewal_is_suppressed_before_the_daily_boundary(self):
        previous = NOW - DAY + 1
        self.auth.last_renewal = previous
        self.assertEqual(await self.auth.get_access_token(), "access")
        self.call.assert_awaited_once_with(
            None,
            "GenerateAccessTokenForApp",
            [(1, "refresh"), (2, ("fixed64", int(ALICE))), (3, 0)],
        )
        self.assertEqual(self.auth.last_renewal, previous)
        self.persist.assert_not_awaited()

    async def test_persisted_renewal_suppresses_renewal_after_restart(self):
        self.call.return_value = {1: b"access", 2: b"rotated"}
        await self.auth.get_access_token()
        self.persist.assert_awaited_once_with("rotated", NOW)
        token, renewed_at = self.persist.await_args.args

        restored = SteamAuth(None, token, ALICE, self.persist, last_renewal=renewed_at)
        self.call.reset_mock()
        self.persist.reset_mock()
        self.call.return_value = {1: b"access"}
        with patch("bot.steam_auth.time.time", return_value=NOW + 1):
            self.assertEqual(await restored.get_access_token(), "access")

        self.call.assert_awaited_once_with(
            None,
            "GenerateAccessTokenForApp",
            [(1, "rotated"), (2, ("fixed64", int(ALICE))), (3, 0)],
        )
        self.assertEqual(restored.last_renewal, NOW)
        self.persist.assert_not_awaited()

    async def test_failed_request_does_not_advance_or_persist_renewal(self):
        for error, expected in (
            (SteamError(503, "unavailable"), SteamError),
            (SteamError(401, "revoked"), TokenRejected),
            (OSError("network down"), OSError),
        ):
            with self.subTest(error=type(error).__name__, status=getattr(error, "status", None)):
                self.auth.last_renewal = NOW - DAY
                self.call.side_effect = error
                with self.assertRaises(expected):
                    await self.auth.get_access_token()
                self.assertEqual(self.auth.last_renewal, NOW - DAY)
                self.assertEqual(self.auth.refresh_token, "refresh")
                self.persist.assert_not_awaited()

    async def test_missing_access_token_does_not_advance_or_persist_renewal(self):
        self.call.return_value = {2: b"rotated"}
        with self.assertRaises(TokenRejected):
            await self.auth.get_access_token()
        self.assertEqual(self.auth.last_renewal, NOW - DAY)
        self.assertEqual(self.auth.refresh_token, "refresh")
        self.persist.assert_not_awaited()

    async def test_successful_renewal_persists_time_when_token_is_unchanged(self):
        self.call.return_value = {1: b"access", 2: b"refresh"}
        await self.auth.get_access_token()
        self.assertEqual(self.auth.last_renewal, NOW)
        self.persist.assert_awaited_once_with("refresh", NOW)

    async def test_replacement_token_and_renewal_time_are_persisted_together(self):
        self.call.return_value = {1: b"access", 2: b"rotated"}
        await self.auth.get_access_token()
        self.assertEqual(self.auth.refresh_token, "rotated")
        self.assertEqual(self.auth.last_renewal, NOW)
        self.persist.assert_awaited_once_with("rotated", NOW)

    async def test_failed_persistence_retries_the_same_pair_before_calling_steam(self):
        self.call.return_value = {1: ACCESS.encode(), 2: b"rotated"}
        self.persist.side_effect = [OSError("database unavailable"), None]
        with self.assertRaises(OSError):
            await self.auth.get_access_token()
        self.assertEqual(self.auth.refresh_token, "refresh")
        self.assertEqual(self.auth.last_renewal, NOW - DAY)
        self.assertIsNone(self.auth.access_token)

        with patch("bot.steam_auth.time.time", return_value=NOW + 1):
            self.assertEqual(await self.auth.get_access_token(), ACCESS)

        self.assertEqual(self.auth.refresh_token, "rotated")
        self.assertEqual(self.auth.last_renewal, NOW)
        self.assertEqual(self.auth.access_token, ACCESS)
        self.call.assert_awaited_once()
        self.assertEqual(self.persist.await_args_list, [call("rotated", NOW), call("rotated", NOW)])

    async def test_expired_pending_access_is_refreshed_after_persistence_recovers(self):
        self.call.return_value = {1: ACCESS.encode(), 2: b"rotated"}
        self.persist.side_effect = [OSError("database unavailable"), None]
        with self.assertRaises(OSError):
            await self.auth.get_access_token()

        async def generate(*args):
            self.assertEqual(self.persist.await_args_list, [call("rotated", NOW), call("rotated", NOW)])
            return {1: b"fresh access"}

        self.call.side_effect = generate
        with patch("bot.steam_auth.time.time", return_value=NOW + 3601):
            self.assertEqual(await self.auth.get_access_token(), "fresh access")

        self.assertEqual(self.auth.refresh_token, "rotated")
        self.assertEqual(self.auth.last_renewal, NOW)
        self.assertEqual(self.call.await_count, 2)
        self.call.assert_awaited_with(
            None,
            "GenerateAccessTokenForApp",
            [(1, "rotated"), (2, ("fixed64", int(ALICE))), (3, 0)],
        )

    async def test_registration_can_renew_without_a_persistence_callback(self):
        auth = SteamAuth(None, "refresh", ALICE)
        self.call.return_value = {1: b"access", 2: b"rotated"}
        self.assertEqual(await auth.get_access_token(), "access")
        self.assertEqual(auth.refresh_token, "rotated")
        self.assertEqual(auth.last_renewal, NOW)

    async def test_concurrent_requests_share_one_access_token_renewal(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def generate(*args):
            started.set()
            await release.wait()
            return {1: ACCESS.encode(), 2: b"rotated"}

        self.call.side_effect = generate
        first = asyncio.create_task(self.auth.get_access_token())
        await started.wait()
        second = asyncio.create_task(self.auth.get_access_token())
        await asyncio.sleep(0)  # let the second request reach the in-flight renewal
        release.set()

        self.assertEqual(await asyncio.gather(first, second), [ACCESS, ACCESS])
        self.call.assert_awaited_once()
        self.persist.assert_awaited_once_with("rotated", NOW)

    async def test_valid_cached_access_token_skips_steam(self):
        self.auth.access_token = ACCESS
        self.assertEqual(await self.auth.get_access_token(), ACCESS)
        self.assertEqual(self.auth.last_renewal, NOW - DAY)
        self.call.assert_not_awaited()
        self.persist.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
