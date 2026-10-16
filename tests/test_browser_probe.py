import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scripts.check_browser_auth import check_family_endpoints, load_credentials, save_credentials


class ProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_checks_reads_and_reports_production_snapshot_without_private_data(self):
        responses = {
            "IFamilyGroupsService/GetFamilyGroupForUser/v1": {
                "response": {"family_groupid": "9", "family_group": {"members": [{"steamid": "1"}]}}
            },
            "IFamilyGroupsService/GetSharedLibraryApps/v1": {"response": {"apps": []}},
            "IPlayerService/GetPlayerLinkDetails/v1": {"response": {"accounts": [{"public_data": {"steamid": "1"}}]}},
            "IFamilyGroupsService/GetPlaytimeSummary/v1": {"response": {}},
            "IFamilyGroupsService/GetPurchaseRequests/v1": {"response": {}},
            "IUserAccountService/GetUserCountry/v1": {"response": {"country": "US"}},
            "IPlayerService/GetSteamLevel/v1": {"response": {"player_level": 4}},
            "IPlayerService/GetOwnedGames/v1": {"response": {"game_count": 0}},
        }

        async def read(http, method, verb, params):
            self.assertIn(method, responses)
            self.assertEqual(params["access_token"], "private-token")
            return responses[method]

        with patch("scripts.check_browser_auth.request_json", side_effect=read) as request:
            report = await check_family_endpoints(None, "private-token", "1")
        self.assertEqual(request.await_count, 8)
        self.assertTrue(all(value["status"] == "pass" for value in report.values()))
        self.assertEqual(report["production_snapshot"]["games"], 0)
        self.assertNotIn("private-token", json.dumps(report))
        self.assertNotIn("steamid", json.dumps(report))

    async def test_optional_endpoint_failure_is_not_hidden_by_snapshot_fallback(self):
        family = {"response": {"family_groupid": "9", "family_group": {"members": [{"steamid": "1"}]}}}

        async def read(http, method, verb, params):
            if "GetFamilyGroupForUser" in method:
                return family
            if "GetSharedLibraryApps" in method:
                return {"response": {"apps": []}}
            raise RuntimeError("secret-token-in-network-error")

        with patch("scripts.check_browser_auth.request_json", side_effect=read):
            report = await check_family_endpoints(None, "token", "1")
        self.assertEqual(report["IFamilyGroupsService/GetPlaytimeSummary/v1"]["status"], "failed")
        self.assertNotIn("secret-token", json.dumps(report))

    async def test_credentials_are_encrypted_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_credentials(root, "secret-cookie", "1")
            self.assertEqual(load_credentials(root), ("secret-cookie", "1"))
            self.assertEqual((root / "credentials.enc").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "key").stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b"secret-cookie", (root / "credentials.enc").read_bytes())
            save_credentials(root, "replacement", "1")
            self.assertEqual(load_credentials(root), ("replacement", "1"))
            (root / "credentials.enc").unlink()
            (root / "credentials.enc").symlink_to(root / "key")
            with self.assertRaises(OSError):
                load_credentials(root)

    async def test_empty_player_response_cannot_prove_profile_compatibility(self):
        for players in ({"response": {}}, {"response": {"accounts": []}}):
            with self.subTest(players=players):

                async def read(http, method, verb, params, players=players):
                    if "GetFamilyGroupForUser" in method:
                        return {"response": {"family_groupid": "9", "family_group": {"members": [{"steamid": "1"}]}}}
                    if "GetPlayerLinkDetails" in method:
                        return players
                    return {"response": {"apps": [], "country": "US", "game_count": 0, "player_level": 4}}

                with patch("scripts.check_browser_auth.request_json", side_effect=read):
                    report = await check_family_endpoints(None, "token", "1")
                self.assertEqual(report["IPlayerService/GetPlayerLinkDetails/v1"]["status"], "failed")


class AuthHeadersTest(unittest.IsolatedAsyncioTestCase):
    async def test_browser_headers_do_not_send_mobile_identity_or_follow_redirects(self):
        from bot.steam_auth import call

        response = SimpleNamespace(status=200, headers={}, read=AsyncMock(return_value=b""))
        context = AsyncMock()
        context.__aenter__.return_value = response
        http = SimpleNamespace(post=lambda *args, **kwargs: context)
        headers = {"user-agent": "Steamily"}
        with patch.object(http, "post", return_value=context) as post:
            await call(http, "BeginAuthSessionViaQR", [], headers=headers)
        self.assertEqual(post.call_args.kwargs["headers"], headers)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
