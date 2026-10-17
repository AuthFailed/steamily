# Steam response fixtures

These are **synthetic wire-shape examples**, not recorded Steam responses. They
were authored from the fields consumed by `bot/steam.py`, `bot/steam_auth.py`, and
`bot/steam_browser.py`.
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

Issue #8 also calls for sanitized recordings from Steam. None were supplied or
present in the repository when these examples were added; these fixtures do not
claim that part of the acceptance criteria is complete. Replace or supplement
them with reviewed, sanitized captures when available, preserving the boundary
cases in the synthetic samples.
