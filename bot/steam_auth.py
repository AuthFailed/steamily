"""Steam mobile-app auth over the WebAPI, the same protocol steam-session (Node) used."""

import asyncio
import base64
import json
import struct
import time

import aiohttp

API = "https://api.steampowered.com/IAuthenticationService"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "user-agent": "okhttp/4.9.2",
    "cookie": "mobileClient=android; mobileClientVersion=777777 3.10.3",
}
MOBILE_APP = 3
ANDROID_UNKNOWN = -500
ACCESS_TOKEN_MARGIN = 300
RENEW_INTERVAL = 24 * 60 * 60


class SteamError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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
        eresult = response.headers.get("x-eresult", "1")
        if response.status != 200 or eresult != "1":
            message = response.headers.get("x-error_message", "")
            raise SteamError(
                response.status, f"Steam {method} failed: HTTP {response.status}, EResult {eresult} {message}".strip()
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
        self._pending_renewal: tuple[str, float, str] | None = None
        self._lock = asyncio.Lock()

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
        await self._persist_renewal()
        if not expires_within(self.access_token, ACCESS_TOKEN_MARGIN):
            return self.access_token
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
            if error.status in (200, 401, 403):  # an answer about the token, not an outage
                raise TokenRejected(str(error)) from error
            raise
        if not response.get(1):
            raise TokenRejected("Steam returned no access token")
        token = response[2].decode() if response.get(2) else self.refresh_token
        renewed_at = time.time() if renew else self.last_renewal
        if self.on_renew and (renew or token != self.refresh_token):
            self._pending_renewal = (token, renewed_at, response[1].decode())
            await self._persist_renewal()
        else:
            self.refresh_token, self.last_renewal, self.access_token = token, renewed_at, response[1].decode()
        return self.access_token


async def begin_qr_login(http: aiohttp.ClientSession) -> dict:
    device = encode([(1, "Galaxy S25"), (2, MOBILE_APP), (3, ANDROID_UNKNOWN), (4, 528)])
    start = await call(http, "BeginAuthSessionViaQR", [(3, device)])
    return {
        "client_id": start[1],
        "request_id": start[3],
        "url": start[2].decode(),
        "interval": struct.unpack("<f", start[4])[0] if 4 in start else 5.0,
    }


async def poll_qr_login(http: aiohttp.ClientSession, login: dict) -> str | None:
    """Returns the refresh token once approved; updates login["url"] when Steam rotates the QR code."""
    poll = await call(http, "PollAuthSessionStatus", [(1, login["client_id"]), (2, login["request_id"])])
    if 1 in poll:
        login["client_id"], login["url"] = poll[1], poll[2].decode()
    return poll[3].decode() if 3 in poll else None
