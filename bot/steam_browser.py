"""Steam browser auth; each attempt/account needs its own new ClientSession.

Keep these sessions private: their cookie jars must never be shared across accounts.
JWT claims below check consistency of trusted Steam HTTPS responses, not signatures.
"""

import json
import math
import secrets
import struct
import time
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from yarl import URL

from bot.steam_auth import AUTH_REJECTIONS, HEADERS, SteamError, TokenRejected, call, encode, jwt_payload

BROWSER_HEADERS = {
    "user-agent": "Steamily",
    "accept": "application/json, text/plain, */*",
    "Origin": "https://steamcommunity.com",
    "Referer": "https://steamcommunity.com/",
}
LOGIN = "https://login.steampowered.com"
POINTS = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"
STORE_TRANSFER = "https://store.steampowered.com/login/settoken"
REFRESH_URL = LOGIN + "/jwt/refresh?redir=https%3A%2F%2Fstore.steampowered.com%2Fpointssummary%2Fajaxgetasyncconfig"
STEAM_HOSTS = {"login.steampowered.com", "store.steampowered.com", "help.steampowered.com", "steamcommunity.com"}


def _steam_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (
            not isinstance(url, str)
            or any(ord(char) <= 32 for char in url)
            or parsed.scheme != "https"
            or parsed.hostname not in STEAM_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("Steam returned an unsafe destination") from None
    return url


def _token_identity(value: str, steam_id: str) -> None:
    payload = jwt_payload(value)
    if not isinstance(payload, dict):
        raise TokenRejected("Steam token is malformed")
    exp = payload.get("exp")
    if (
        payload.get("sub") != steam_id
        or isinstance(exp, bool)
        or not isinstance(exp, (int, float))
        or not math.isfinite(exp)
        or not exp > time.time()
    ):
        raise TokenRejected("Steam token is invalid, expired, or belongs to another account")


def _refresh_identity(value: str, steam_id: str) -> None:
    if not isinstance(value, str) or not value:
        raise TokenRejected("Steam refresh cookie is invalid")
    decoded = unquote(value)
    if "||" in decoded:
        account, decoded = decoded.split("||", 1)
        if account != steam_id:
            raise TokenRejected("Steam refresh cookie belongs to another account")
    _token_identity(decoded, steam_id)


def _refresh_cookie(response, url: str, steam_id: str) -> str | None:
    cookie = response.cookies.get("steamRefresh_steam")
    if cookie is None:
        return None
    if urlsplit(url).hostname != "login.steampowered.com" or cookie["domain"].lstrip(".") not in (
        "",
        "login.steampowered.com",
    ):
        raise TokenRejected("Steam returned an unexpected refresh cookie")
    _refresh_identity(cookie.value, steam_id)
    return cookie.value


def _status(response) -> None:
    if response.status in (401, 403):
        raise TokenRejected("Steam rejected the browser session")
    if not 200 <= response.status < 300:
        raise RuntimeError("Steam browser session request failed")
    result = response.headers.get("x-eresult", "1")
    if result != "1":
        if result.isdecimal() and int(result) in AUTH_REJECTIONS:
            raise TokenRejected("Steam rejected the browser session")
        raise RuntimeError("Steam browser session request failed")


async def begin_browser_login(http: aiohttp.ClientSession) -> dict:
    """Start one browser QR attempt using a private, newly created session."""
    try:
        device = encode([(1, BROWSER_HEADERS["user-agent"]), (2, 2)])
        start = await call(http, "BeginAuthSessionViaQR", [(3, device), (4, "Community")], headers=BROWSER_HEADERS)
        interval = struct.unpack("<f", start[4])[0] if 4 in start else 5.0
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError
        return {"client_id": start[1], "request_id": start[3], "url": start[2].decode(), "interval": interval}
    except (
        SteamError,
        aiohttp.ClientError,
        TimeoutError,
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        struct.error,
    ):
        raise RuntimeError("Steam could not start browser login") from None


async def poll_browser_login(http: aiohttp.ClientSession, login: dict) -> str | None:
    """Poll once; preserve QR rotations without starting another login attempt."""
    try:
        poll = await call(
            http, "PollAuthSessionStatus", [(1, login["client_id"]), (2, login["request_id"])], headers=BROWSER_HEADERS
        )
        if poll.get(1):
            login["client_id"] = poll[1]
        if poll.get(2):
            login["url"] = poll[2].decode()
        return poll[3].decode() if 3 in poll else None
    except (SteamError, aiohttp.ClientError, TimeoutError, KeyError, ValueError, TypeError, AttributeError):
        raise RuntimeError("Steam could not check browser login") from None


async def _post(http, url: str, fields: dict, steam_id: str, on_refresh, *, require_json=True) -> tuple[dict, object]:
    form = aiohttp.FormData(default_to_multipart=True)
    for name, value in fields.items():
        form.add_field(name, str(value))
    async with http.post(url, data=form, headers=BROWSER_HEADERS, allow_redirects=False) as response:
        durable = _refresh_cookie(response, url, steam_id)
        if durable is not None and on_refresh is not None:
            await on_refresh(durable)
        _status(response)
        try:
            body = json.loads(await response.text())
        except json.JSONDecodeError:
            if require_json:
                raise
            body = {}
        if not isinstance(body, dict):
            if require_json:
                raise TokenRejected("Steam returned an invalid browser login response")
            body = {}
        if body.get("error") or body.get("result", 1) != 1:
            code = body.get("error") or body.get("result")
            if type(code) is int and code in AUTH_REJECTIONS:
                raise TokenRejected("Steam rejected browser login")
            raise RuntimeError("Steam returned an unsuccessful browser login response")
        return body, response


async def finalize_login(http: aiohttp.ClientSession, refresh_token: str, steam_id: str, *, on_refresh=None) -> str:
    """Persist the durable cookie and establish the Store session needed for API renewal."""
    _token_identity(refresh_token, steam_id)
    try:
        body, response = await _post(
            http,
            LOGIN + "/jwt/finalizelogin",
            {
                "nonce": refresh_token,
                "sessionid": secrets.token_hex(12),
                "redir": "https://steamcommunity.com/login/home/?goto=",
            },
            steam_id,
            on_refresh,
        )
        if body.get("steamID", steam_id) != steam_id or not isinstance(body.get("transfer_info"), list):
            raise TokenRejected("Steam returned an invalid browser login response")
        durable = _refresh_cookie(response, LOGIN, steam_id)
        if not durable:
            raise TokenRejected("Steam returned no durable browser refresh cookie")
        # WebAPI renewal needs the Store session; other website transfers are unnecessary.
        transfers = []
        for transfer in body["transfer_info"]:
            url = transfer.get("url", "") if isinstance(transfer, dict) else ""
            try:
                destination = urlsplit(url)
            except (TypeError, ValueError):
                continue
            if destination.hostname != "store.steampowered.com" or destination.path != "/login/settoken":
                continue
            _steam_url(url)
            if url not in (STORE_TRANSFER, "https://store.steampowered.com:443/login/settoken"):
                raise RuntimeError("Steam returned an unexpected Store transfer")
            params = transfer["params"]
            if not isinstance(params, dict) or params.get("steamID", steam_id) != steam_id:
                raise TokenRejected("Steam returned an invalid account transfer")
            transfers.append(transfer)
        if not transfers:
            raise TokenRejected("Steam returned no Store login transfer")
        for transfer in transfers:
            _, response = await _post(
                http,
                transfer["url"],
                {"steamID": steam_id, **transfer["params"]},
                steam_id,
                on_refresh,
                require_json=False,
            )
            secure = response.cookies.get("steamLoginSecure")
            if secure is None:
                raise TokenRejected("Steam returned no browser login cookie")
            _refresh_identity(secure.value, steam_id)
            rotated = _refresh_cookie(response, transfer["url"], steam_id)
            if rotated is not None:
                durable = rotated
        return durable
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
        raise RuntimeError("Steam browser login finalization failed") from None


async def renew_access_token(
    http: aiohttp.ClientSession, refresh_cookie: str, steam_id: str, *, on_refresh=None
) -> tuple[str, str]:
    """Renew in a new private session; await on_refresh(cookie) immediately on rotation."""
    _refresh_identity(refresh_cookie, steam_id)
    http.cookie_jar.update_cookies({"steamRefresh_steam": refresh_cookie}, response_url=URL(LOGIN))
    effective = refresh_cookie
    url = REFRESH_URL
    try:
        for _ in range(6):
            async with http.get(url, headers=BROWSER_HEADERS, allow_redirects=False) as response:
                rotated = _refresh_cookie(response, url, steam_id)
                if rotated is not None and rotated != effective:
                    if on_refresh is not None:
                        await on_refresh(rotated)
                    effective = rotated
                if response.status in (301, 302, 303, 307, 308):
                    destination = response.headers.get("Location")
                    if not destination:
                        raise RuntimeError("Steam returned an invalid browser redirect")
                    url = _steam_url(urljoin(url, destination))
                    continue
                _status(response)
                body = json.loads(await response.text())
                if not isinstance(body, dict) or body.get("success", 1) != 1 or body.get("error"):
                    code = body.get("error") if isinstance(body, dict) else None
                    if type(code) is int and code in AUTH_REJECTIONS:
                        raise TokenRejected("Steam rejected the browser session")
                    raise RuntimeError("Steam returned an incomplete browser session response")
                data = body.get("data", body)
                access = data.get("webapi_token") if isinstance(data, dict) else None
                if not isinstance(access, str) or not access:
                    raise RuntimeError("Steam returned no browser access token")
                _token_identity(access, steam_id)
                return access, effective
        raise RuntimeError("Steam browser redirect limit exceeded")
    except json.JSONDecodeError:
        raise RuntimeError("Steam returned an incomplete browser session response") from None
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
        raise RuntimeError("Steam browser session renewal failed") from None


async def revoke_session(http: aiohttp.ClientSession, refresh_token: str, steam_id: str) -> None:
    """Permanently revoke exactly this refresh JWT; never deauthorize other sessions."""
    raw = unquote(refresh_token)
    is_cookie = "||" in raw
    if is_cookie:
        account, raw = raw.split("||", 1)
        if account != steam_id:
            raise TokenRejected("Steam refresh cookie belongs to another account")
    payload = jwt_payload(raw)
    audience = payload.get("aud", []) if isinstance(payload, dict) else []
    if (
        not isinstance(audience, list)
        or "derive" not in audience
        or not all(isinstance(value, str) for value in audience)
        or not ("web" in audience or "mobile" in audience)
        or is_cookie
        and "web" not in audience
        or payload.get("sub") != steam_id
    ):
        raise TokenRejected("Steam refresh token identity is invalid")
    try:
        headers = HEADERS if "mobile" in audience and not is_cookie else BROWSER_HEADERS
        await call(http, "RevokeToken", [(1, raw), (2, 1)], headers=headers)
    except SteamError as error:
        if error.status in (401, 403) or error.status == 200 and error.eresult in (26, 27):
            return
        raise RuntimeError("Steam could not revoke this session") from None
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
        raise RuntimeError("Steam could not revoke this session") from None
