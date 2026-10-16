# Steamily

Telegram bot for Steam Families: shared library, statistics, notifications and family management.

## Run

Copy `.env.example` to `.env` and set the bot token, database password and encryption key. Generate the key once using the command in `.env.example`; keep it with your encrypted backups. Losing or replacing it makes saved Steam logins unreadable.

Set `LOGIN_LOCATION` to the real location of the bot server, then run `docker compose up -d`. The bot and PostgreSQL start together. Keep `.env` and database backups private.

## Steam login

New logins use a WebBrowser QR session labelled **Steamily**. The bot does not request a Steam password or authenticator secret. Each attempt and each renewal has a private cookie jar; cookies are never shared between accounts.

Before showing the QR, the bot explains what the session allows, the server location and how to revoke it. Users should check the browser identity and expected location in Steam Guard and cancel unexpected requests. Do not instruct users to bypass a warning or select an unrelated device or purpose.

This is a Steam website session, not a permission limited to family data. A login from a server in another country can still trigger Steam security checks. Successful API calls do not establish that account restrictions cannot occur.

The bot stores the durable refresh credential encrypted in PostgreSQL. Access tokens are cached until shortly before expiry. Browser renewal uses Steam's website refresh flow; replacement cookies are saved before later requests or parsing can fail. Steam decides when a replacement is issued. Existing mobile credentials remain readable, so an upgrade does not require everyone to log in again.

Only the required Store transfer is completed during browser finalization. Other website destinations receive no transfer credentials. HTTPS destinations, account identity and token expiry are checked before credentials are used.

## Scheduled reads

Each family has one persisted reader account. Initial selection prefers its approved bot owner, then another approved account with a saved session. That selection survives restarts and bot-owner changes. It does not grant Steam adult permissions.

If Steam rejects the reader's session, the bot stops using it and asks for login again when that user opens the menu. It does not silently try every other member's session. A member who logs out releases their reader selection; another approved account can then be selected. Explicit family changes always use the acting user's own session.

`POLL_INTERVAL_SECONDS` defaults to **1800** and must be at least **300**. Presence checks run every **300 seconds** when enabled. Temporary Steam failures share a family backoff starting at 60 seconds and increasing to one hour. Successful reads reset it. After upgrading, change an older `.env` value below 300 before starting the bot.

## Logout and revocation

**Settings → Log out** revokes only the selected Steam refresh token before deleting its local account data. It does not deauthorize every device. If Steam revocation fails, the bot keeps the local login so the user can retry. Users can also remove the Steamily session from **Steam Guard → Authorized devices**.

Already-issued access tokens can have a different invalidation lifetime from the refresh token. Logout stops the bot using its cached access token immediately. Revocation verification must check that the revoked credential cannot obtain another access token in a fresh cookie jar.

Failed or cancelled sign-ins attempt to revoke the newly approved session if registration has not been saved, including when family registration fails. Cancellation after a saved registration keeps that login. A Steam outage can prevent cleanup; the failure screen explains manual removal through Steam Guard.

## Updates and recovery

Back up PostgreSQL and retain the matching `SECRET_KEY` before updating. The reader-selection schema change is additive and preserves existing accounts, approval states and encrypted credentials.

To pause all Steam traffic, stop the bot. A binary from before browser-session support cannot use newly stored browser credentials. Do not deploy that older binary against the upgraded data or delete credentials to make it fit; plan a rollback with a matching backup instead.

The browser compatibility probe is opt-in: run `python -m scripts.check_browser_auth --state-dir /private/path`, then repeat with `--resume` to check renewal in a separate process. The directory must be private. Reports contain statuses and counts, without credentials, member identities or game titles. The probe exercises reads only.

Account membership and execution-time authorization are tracked separately in issues #10 and #11. Complete those requirements before opening an instance to other users.
