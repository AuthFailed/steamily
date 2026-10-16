"""Steam access tokens for browser sessions and previously stored mobile sessions."""

import asyncio
import base64
import json
import struct
import time
from urllib.parse import unquote

import aiohttp

API = "https://api.steampowered.com/IAuthenticationService"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "user-agent": "okhttp/4.9.2",
    "cookie": "mobileClient=android; mobileClientVersion=777777 3.10.3",
}
ACCESS_TOKEN_MARGIN = 300
RENEW_INTERVAL = 24 * 60 * 60
AUTH_REJECTIONS = {5, 15, 26, 27, 43}  # InvalidPassword, AccessDenied, Revoked, Expired, AccountDisabled.


class SteamError(RuntimeError):
    def __init__(self, status: int, message: str, eresult: int | None = None):
        super().__init__(message)
        self.status = status
        self.eresult = eresult


class TokenRejected(Exception):
    """Steam refused the refresh token (revoked or expired): the user must log in again."""


# Minimal protobuf codec: only the field kinds the auth messages use.
def _varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode(fields: list[tuple[int, object]]) -> bytes:
    """fields: (number, value); int -> varint, bytes/str -> length-delimited, ("fixed64", n) -> fixed64."""
    out = bytearray()
    for number, value in fields:
        if isinstance(value, tuple):
            out += _varint(number << 3 | 1) + struct.pack("<Q", value[1])
        elif isinstance(value, int):
            out += _varint(number << 3) + _varint(value)
        else:
            raw = value.encode() if isinstance(value, str) else value
            out += _varint(number << 3 | 2) + _varint(len(raw)) + raw
    return bytes(out)


def decode(data: bytes) -> dict[int, object]:
    result, position = {}, 0

    def read_varint():
        nonlocal position
        shift = value = 0
        while True:
            byte = data[position]
            position += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    while position < len(data):
        key = read_varint()
        number, wire = key >> 3, key & 7
        if wire == 0:
            result[number] = read_varint()
        elif wire == 1:
            result[number] = data[position : position + 8]
            position += 8
        elif wire == 2:
            length = read_varint()
            result[number] = data[position : position + length]
            position += length
        elif wire == 5:
            result[number] = data[position : position + 4]
            position += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
    return result


async def call(http: aiohttp.ClientSession, method: str, fields, *, headers: dict | None = None) -> dict[int, object]:
    form = aiohttp.FormData(default_to_multipart=True)
    form.add_field("input_protobuf_encoded", base64.b64encode(encode(fields)).decode())
    async with http.post(
        f"{API}/{method}/v1/", data=form, headers=HEADERS if headers is None else headers, allow_redirects=False
    ) as response:
        body = await response.read()
        raw_result = response.headers.get("x-eresult", "1")
        try:
            eresult = int(raw_result)
        except (TypeError, ValueError):
            eresult = None
        if response.status != 200 or eresult != 1:
            message = response.headers.get("x-error_message", "")
            raise SteamError(
                response.status,
                f"Steam {method} failed: HTTP {response.status}, EResult {eresult} {message}".strip(),
                eresult,
            )
    return decode(body)


def jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, AttributeError):
        return {}


def expires_within(token: str | None, seconds: int, now: float | None = None) -> bool:
    exp = jwt_payload(token or "").get("exp")
    return not isinstance(exp, (int, float)) or exp <= (now or time.time()) + seconds


def is_browser_token(token: str) -> bool:
    value = unquote(token)
    if "||" in value:
        return True
    payload = jwt_payload(value)
    audience = payload.get("aud", []) if isinstance(payload, dict) else []
    return audience == "web" or (
        isinstance(audience, list) and "web" in audience and "mobile" not in audience and "client" not in audience
    )


class SteamAuth:
    """Access tokens for one account; on_renew(token, renewed_at) persists credentials together."""

    def __init__(
        self, http: aiohttp.ClientSession, refresh_token: str, steam_id: str, on_renew=None, last_renewal: float = 0.0
    ):
        self.http = http
        self.refresh_token = refresh_token
        self.steam_id = steam_id
        self.on_renew = on_renew
        self.access_token: str | None = None
        self.last_renewal = last_renewal
        self._pending_renewal: tuple[str, float, str | None] | None = None
        self._lock = asyncio.Lock()
        self._revoked = False

    async def _persist_renewal(self) -> None:
        if self._pending_renewal is not None:
            token, renewed_at, access_token = self._pending_renewal
            await self.on_renew(token, renewed_at)
            self.refresh_token, self.last_renewal, self.access_token = token, renewed_at, access_token
            self._pending_renewal = None

    async def get_access_token(self) -> str:
        async with self._lock:
            return await self._get_access_token()

    async def _get_access_token(self) -> str:
        if self._revoked:
            raise TokenRejected("This Steam session has been revoked")
        await self._persist_renewal()
        if not expires_within(self.access_token, ACCESS_TOKEN_MARGIN):
            return self.access_token
        if is_browser_token(self.refresh_token):
            return await self._get_browser_access_token()
        renew = time.time() - self.last_renewal >= RENEW_INTERVAL
        try:
            response = await call(
                self.http,
                "GenerateAccessTokenForApp",
                [
                    (1, self.refresh_token),
                    (2, ("fixed64", int(self.steam_id))),
                    (3, 1 if renew else 0),
                ],
            )
        except SteamError as error:
            if error.status in (401, 403) or error.status == 200 and error.eresult in AUTH_REJECTIONS:
                raise TokenRejected("Steam rejected the refresh token") from None
            raise
        if not response.get(1):
            raise RuntimeError("Steam returned no access token")
        token = response[2].decode() if response.get(2) else self.refresh_token
        renewed_at = time.time() if renew else self.last_renewal
        if self.on_renew and (renew or token != self.refresh_token):
            self._pending_renewal = (token, renewed_at, response[1].decode())
            await self._persist_renewal()
        else:
            self.refresh_token, self.last_renewal, self.access_token = token, renewed_at, response[1].decode()
        return self.access_token

    async def _get_browser_access_token(self) -> str:
        from .steam_browser import BROWSER_HEADERS, renew_access_token

        async def persist_cookie(token):
            renewed_at = time.time()
            if self.on_renew is not None:
                self._pending_renewal = (token, renewed_at, None)
                await self._persist_renewal()
            else:
                self.refresh_token, self.last_renewal = token, renewed_at
                self.access_token = None

        async with aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as http:
            access, effective = await renew_access_token(
                http, self.refresh_token, self.steam_id, on_refresh=persist_cookie
            )
        if effective != self.refresh_token:
            await persist_cookie(effective)
        self.access_token = access
        return access

    async def revoke(self) -> None:
        """Revoke only this account's latest session, serialized with token renewal."""
        from .steam_browser import revoke_session

        async with self._lock:
            if self._revoked:
                return
            await self._persist_renewal()
            await revoke_session(self.http, self.refresh_token, self.steam_id)
            self._revoked = True
            self.access_token = None
