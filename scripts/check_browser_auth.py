"""Opt-in browser-session experiment; never registers accounts or changes the bot's login flow.

Run from the repository: python -m scripts.check_browser_auth --state-dir /private/tmp/steamily-browser-check
Approve the displayed QR in Steam Guard, then run again with --resume to verify renewal after a process restart.
Only read operations are checked. Credentials stay encrypted in the private state directory; reports contain no tokens.
"""

import argparse
import asyncio
import json
import os
import stat
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from cryptography.fernet import Fernet

from bot.login import qr_png
from bot.steam import API, parse_snapshot
from bot.steam_auth import jwt_payload


def write_private(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        name = Path(temporary.name)
        try:
            os.chmod(name, 0o600)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


def read_private(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise OSError("State file must be private and regular")
        return source.read()


def save_credentials(root: Path, refresh_cookie: str, steam_id: str) -> None:
    key_path = root / "key"
    if not key_path.exists():
        write_private(key_path, Fernet.generate_key())
    data = json.dumps({"refresh_cookie": refresh_cookie, "steam_id": steam_id}).encode()
    write_private(root / "credentials.enc", Fernet(read_private(key_path)).encrypt(data))


def load_credentials(root: Path) -> tuple[str, str]:
    data = json.loads(Fernet(read_private(root / "key")).decrypt(read_private(root / "credentials.enc")))
    return data["refresh_cookie"], data["steam_id"]


async def request_json(http, method: str, verb: str, params: dict) -> dict:
    arguments = {"params" if verb == "GET" else "data": params}
    async with http.request(verb, f"{API}/{method}/", allow_redirects=False, **arguments) as response:
        if response.status != 200 or response.headers.get("x-eresult", "1") != "1":
            raise RuntimeError("Steam refused the read request")
        data = await response.json(content_type=None)
        if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
            raise RuntimeError("Steam returned an incomplete response")
        return data


async def check_family_endpoints(http, access_token: str, steam_id: str) -> dict:
    report, results = {}, {}

    async def read(method: str, params=None, *, verb="GET", list_field=None, required_field=None):
        try:
            data = await request_json(http, method, verb, {"access_token": access_token, **(params or {})})
            body = data["response"]
            if list_field and not isinstance(body.get(list_field, []), list):
                raise ValueError("Invalid list response")
            if required_field and required_field not in body:
                raise ValueError("Missing required field")
            results[method] = data
            report[method] = {"status": "pass"}
            if list_field:
                report[method]["rows"] = len(body.get(list_field, []))
            return body
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, KeyError, TypeError):
            report[method] = {"status": "failed"}
            return None

    family_method = "IFamilyGroupsService/GetFamilyGroupForUser/v1"
    family = await read(family_method, {"include_family_group_response": "true"}, required_field="family_groupid")
    group = (family or {}).get("family_group", {})
    members = group.get("members", []) if isinstance(group, dict) else []
    if not isinstance(members, list) or steam_id not in {str(m.get("steamid")) for m in members if isinstance(m, dict)}:
        report[family_method] = {"status": "failed"}
        return report
    group_id = family["family_groupid"]
    library_method = "IFamilyGroupsService/GetSharedLibraryApps/v1"
    await read(
        library_method,
        {
            "family_groupid": group_id,
            "include_own": "true",
            "include_excluded": "true",
            "include_free": "false",
            "include_non_games": "false",
            "language": "english",
        },
        list_field="apps",
        required_field="apps",
    )
    players_method = "IPlayerService/GetPlayerLinkDetails/v1"
    players = await read(
        players_method, {f"steamids[{i}]": m["steamid"] for i, m in enumerate(members)}, list_field="accounts"
    )
    if not players or not any(
        isinstance(account, dict)
        and isinstance(account.get("public_data"), dict)
        and str(account["public_data"].get("steamid")) == steam_id
        for account in players.get("accounts", [])
    ):
        report[players_method] = {"status": "failed"}
    await read(
        "IFamilyGroupsService/GetPlaytimeSummary/v1", {"family_groupid": group_id}, verb="POST", list_field="entries"
    )
    await read("IFamilyGroupsService/GetPurchaseRequests/v1", {"family_groupid": group_id}, list_field="requests")
    await read("IUserAccountService/GetUserCountry/v1", {"steamid": steam_id}, verb="POST", required_field="country")
    await read("IPlayerService/GetSteamLevel/v1", {"steamid": steam_id}, required_field="player_level")
    await read(
        "IPlayerService/GetOwnedGames/v1",
        {"steamid": steam_id, "include_appinfo": "false"},
        required_field="game_count",
    )
    try:
        snapshot = parse_snapshot(results[family_method], results[library_method], results.get(players_method, {}))
        report["production_snapshot"] = {"status": "pass", "games": len(snapshot["apps"]), "members": len(members)}
    except (RuntimeError, KeyError, TypeError):
        report["production_snapshot"] = {"status": "failed"}
    return report


def save_report(root: Path, report: dict) -> None:
    write_private(root / "report.json", json.dumps(report, indent=2).encode())


async def run(root: Path, resume: bool) -> bool:
    from scripts.steam_browser_session import (
        BROWSER_HEADERS,
        begin_browser_login,
        finalize_login,
        poll_browser_login,
        renew_access_token,
    )

    report = {"checked_at": datetime.now(UTC).isoformat(), "stage": "starting", "mutations": "not tested"}
    save_report(root, report)
    timeout = aiohttp.ClientTimeout(total=30)

    async def persist_cookie(cookie: str) -> None:
        save_credentials(root, cookie, steam_id)

    if resume:
        refresh_cookie, steam_id = load_credentials(root)
    else:
        async with aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=timeout) as http:
            login = await begin_browser_login(http)
            qr_path = root / "qr.png"
            write_private(qr_path, qr_png(login["url"]))
            report["stage"] = "awaiting_approval"
            save_report(root, report)
            print(f"Approve the local browser-session QR: {qr_path}", flush=True)
            deadline = time.monotonic() + 300
            try:
                while time.monotonic() < deadline:
                    await asyncio.sleep(max(1, login["interval"]))
                    previous = login["url"]
                    refresh_token = await poll_browser_login(http, login)
                    if refresh_token:
                        steam_id = str(jwt_payload(refresh_token).get("sub", ""))
                        refresh_cookie = await finalize_login(http, refresh_token, steam_id, on_refresh=persist_cookie)
                        save_credentials(root, refresh_cookie, steam_id)
                        break
                    if login["url"] != previous:
                        write_private(qr_path, qr_png(login["url"]))
                        print("QR updated", flush=True)
                else:
                    report["stage"] = "approval_timed_out"
                    save_report(root, report)
                    return False
            finally:
                qr_path.unlink(missing_ok=True)

    # Start with an empty cookie jar: cached access cookies must not conceal a broken renewal path.
    async with aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=timeout) as http:
        access_token, effective_cookie = await renew_access_token(
            http, refresh_cookie, steam_id, on_refresh=persist_cookie
        )
        save_credentials(root, effective_cookie, steam_id)
        payload = jwt_payload(access_token)
        report.update(
            stage="renewed_after_restart" if resume else "renewed_in_fresh_session",
            refresh_cookie_rotated=effective_cookie != refresh_cookie,
            access_expires_in_seconds=max(0, int(payload.get("exp", 0) - time.time())),
            endpoints=await check_family_endpoints(http, access_token, steam_id),
        )
    save_report(root, report)
    print(json.dumps(report, indent=2), flush=True)
    return bool(report["endpoints"]) and all(item["status"] == "pass" for item in report["endpoints"].values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Renew a saved experimental session without a new login")
    args = parser.parse_args()
    root = args.state_dir.absolute()
    if root.is_symlink():
        parser.error("State directory cannot be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        parser.error("State directory must have mode 0700")
    try:
        return 0 if asyncio.run(run(root, args.resume)) else 1
    except (Exception, KeyboardInterrupt) as error:
        # HTTP exception strings can contain credential-bearing request URLs.
        save_report(root, {"stage": "failed", "error_type": type(error).__name__, "login_flow_changed": False})
        print(f"Browser check failed ({type(error).__name__}); credentials and response bodies withheld.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
