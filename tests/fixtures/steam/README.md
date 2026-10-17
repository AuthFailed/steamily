# Steam response fixtures

Except for `shared_library_recorded.json`, these are **synthetic wire-shape
examples**, not recorded Steam responses. They were authored from the fields
consumed by `bot/steam.py`, `bot/steam_auth.py`, and `bot/steam_browser.py`.
No account, session, QR code, refresh token, or access token is real. The JWT-shaped
access token contains only a test expiry and has no valid signature.

`auth.json` stores fixed protobuf request and response bytes as hexadecimal;
tests replay these bytes through an actual local HTTP server, without using the
production encoder to construct the expected wire messages. Its response samples
cover QR creation (with and without an interval), pending approval, QR rotation,
approval, access-token renewal, missing access tokens, and EResult rejection.

The JSON examples cover family membership, library ownership, player links and
presence, playtime, and store items, including optional or missing fields. Tests
use a local HTTP server and never contact Steam.

`shared_library_recorded.json` adds sanitized library rows captured from Steam
for issue #12, along with an explicitly derived missing-metadata variant; see
[provenance and sanitization details](../README.md#recorded-steam-response).
The authentication, membership, player, playtime, and store examples remain
synthetic, so this library recording does not complete the broader recorded
response coverage requested by issue #8.
