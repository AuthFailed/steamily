# Steamily

A self-hosted Telegram bot for Steam Families: shared games, family statistics,
and change notifications. It uses Telegram long polling; no public port or
webhook domain is required.

## Create the Telegram bot

Open [@BotFather](https://t.me/BotFather), run `/newbot`, and keep its token private.
Configure the new bot:

| BotFather command | Setting |
| --- | --- |
| `/setinline` | Enable inline mode; use `Search family games` as the placeholder. |
| `/setjoingroups` | Enable adding the bot to groups for family notifications. |
| `/setprivacy` | Keep **Enabled**. `/link@YourBotUsername` works with privacy enabled; reading every group message is unnecessary. |
| `/setcommands` | Paste the commands below. |
| `/setdescription` | Suggested: `See your Steam Family's shared games, statistics, and change notifications. Open the bot to link your Steam account.` |
| `/setabouttext` | Suggested: `Your Steam Family in Telegram.` |
| `/setuserpic` | Upload a picture you own for your instance. |

```text
start - Open the family menu
link - Link this group to your family notifications
gamenight - Propose a game night in the linked group
```

These settings are managed in BotFather; the bot does not apply them on startup.
See the [Telegram bot settings reference](https://core.telegram.org/bots/features#botfather).

## Run with Docker Compose

Install Docker with Compose **2.24.0 or newer**. The image supports **64-bit
amd64 and arm64**, including a Raspberry Pi with a 64-bit operating system.

```sh
git clone https://github.com/AuthFailed/steamily.git
cd steamily
docker compose pull
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
  --volume "$PWD:/setup" bot python -m bot.setup --env-file /setup/.env
docker compose up -d
docker compose ps
docker compose logs --tail=100 bot
```

Setup asks for the BotFather token, generates a PostgreSQL password and encryption
key, and writes `.env` with owner-only permissions. It can also update an existing
file while retaining its encryption key. Back up `.env` immediately: **losing or
replacing `SECRET_KEY` makes the stored Steam logins unreadable**. Never commit or
share `.env`. The setup command needs the directory bind mount so it can replace
the file atomically; the running bot does not need that mount.

Startup records applied schema versions in `schema_migrations` and applies pending
migrations in one transaction before accepting Telegram updates. A failed pending
batch rolls back; it does not leave a partly upgraded schema. Migrations are
forward-only. Keep only one bot instance per token. In a private chat with the bot, run
`/start` and follow the Steam sign-in flow. The family owner can then add the bot
to a group and send `/link@YourBotUsername` there.

`main` is a moving development image and works before the first tagged release.
To build the checked-out source, use `docker compose build bot` instead of
`docker compose pull`. To pin a published release, set `STEAMILY_VERSION` in `.env`
to its image version without the leading `v` (for example `1.1.0`). Do not select
a version until it appears in the repository's package list.

Manual configuration is available in [.env.example](.env.example). Use a URL-safe
PostgreSQL password containing letters, digits, `.`, `_`, `~`, or `-`, because
Compose inserts it into `DATABASE_URL`. Runtime validation lists missing or invalid
settings by name without printing their values. A missing key at runtime is an
error: only explicit setup generates a key, preventing accidental replacement
when recovering an existing database.

For a native Python 3.13+ installation with an existing PostgreSQL database:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m bot.setup --external-db
set -a
. ./.env
set +a
python -m bot
```

## Organize a game night

An approved family member can propose a session in the linked group:

```text
/gamenight@YourBotUsername 2099-01-02 19:30 Europe/Moscow 570 730
```

Replace the example with your future date, local time, explicit IANA timezone
(or `UTC`), and **2–5 different games from your family library**. Game IDs are
the numbers in the games' Steam store links. Send `/gamenight@YourBotUsername`
without arguments for help. For ambiguous or nonexistent times at a daylight-saving
transition, convert your intended instant to UTC.

Each member can choose one game and independently answer **Going**, **Maybe**, or
**Cannot attend**. Clicking again replaces the previous response. The creator can
close the poll after a vote or cancel the event. The most-voted game wins, with
shortlist order breaking ties; closing fixes that choice and disables the buttons.
The card shows attendees and required versus eligible copies for each candidate.
Excluded copies only count for attending owners; missing or unknown sharing data
is labelled unknown. Copy counts do not reserve licenses or guarantee availability
now or at the proposed time.

Events and responses survive restarts. Current family membership, approval, saved
login, linked group, and original event message are checked on every action.
Unlinking or changing the group invalidates old events even if the same group is
linked again. Departed or unapproved participants no longer count. A Telegram
group upgrade requires a new event. No reminders or external calendar entries are
created.

## Steam login

New logins use a WebBrowser QR session labelled **Steamily**. The bot does not request a Steam password or authenticator secret. Each attempt and each renewal has a private cookie jar; cookies are never shared between accounts.

Before showing the QR, the bot explains what the session allows, the server location and how to revoke it. Users should check the browser identity and expected location in Steam Guard and cancel unexpected requests. Do not instruct users to bypass a warning or select an unrelated device or purpose.

This is a Steam website session, not a permission limited to family data. A login from a server in another country can still trigger Steam security checks. Successful API calls do not establish that account restrictions cannot occur.

The bot stores the durable refresh credential encrypted in PostgreSQL. Access tokens are cached until shortly before expiry. Browser renewal uses Steam's website refresh flow; replacement cookies are saved before later requests or parsing can fail. Steam decides when a replacement is issued. Existing mobile credentials remain readable, so an upgrade does not require everyone to log in again.

Only the required Store transfer is completed during browser finalization. Other website destinations receive no transfer credentials. HTTPS destinations, account identity and token expiry are checked before credentials are used.

## Personal game statuses

Approved family users can open a game card and mark it **Backlog**, **Playing**,
or **Completed**, or clear its status. In the library's **Filters**, select a status
alongside the existing filters. Marks are manual and do not sync with Steam
achievements or playtime. If a marked game leaves the shared library, it remains
under **Unavailable marked games (N)**, where its status can be cleared.

Your marks are private to your bot account; fellow users cannot see them. The
hosting operator controls the database. Marks survive restarts and are deleted
when you log out, use `/delete`, or are administratively removed.

## Scheduled reads

Each family has one persisted reader account. Initial selection prefers its approved bot owner, then another approved account with a saved session. That selection survives restarts and bot-owner changes. It does not grant Steam adult permissions.

If Steam rejects the reader's session, the bot stops using it and asks for login again when that user opens the menu. It does not silently try every other member's session. A member who logs out releases their reader selection; another approved account can then be selected. Explicit family changes always use the acting user's own session.

If the departing owner was the only approved reader, scheduled reads wait. A
remaining pending member can log in again to supply a fresh membership check and
recover eligible bot ownership; the scheduler does not use pending credentials
to make that decision.

`POLL_INTERVAL_SECONDS` defaults to **1800** and must be at least **300**. Presence checks run every **300 seconds** when enabled. Temporary Steam failures share a family backoff starting at 60 seconds and increasing to one hour. Successful reads reset it. After upgrading, change an older `.env` value below 300 before starting the bot.

## Logout and revocation

**Settings → Log out** revokes only the selected Steam refresh token before deleting its local account data. It does not deauthorize every device. If Steam revocation fails, the bot keeps the local login so the user can retry. Users can also remove the Steamily session from **Steam Guard → Authorized devices**.

Already-issued access tokens can have a different invalidation lifetime from the refresh token. Logout stops the bot using its cached access token immediately. Revocation verification must check that the revoked credential cannot obtain another access token in a fresh cookie jar.

Failed or cancelled sign-ins attempt to revoke the newly approved session if registration has not been saved, including when family registration fails. Cancellation after a saved registration keeps that login. A Steam outage can prevent cleanup; the failure screen explains manual removal through Steam Guard.

## Check health and delivery

```sh
docker compose ps
docker compose exec -T bot python -m bot.health
docker compose logs --since=30m --tail=200 bot
```

The health command prints credential-free JSON and exits nonzero when unhealthy.
Docker runs the same check automatically. Its local heartbeat must be less than
30 seconds old; refresh, presence, and delivery workers must be running and making
progress. A worker that stops, fails, or stalls is unhealthy even if Telegram
still responds. Docker marks a container unhealthy but does **not** automatically
restart it solely for that reason.

The JSON reports worker status and age, the age of the last successful Steam
family snapshot read, and delayed snapshot reads by family ID. This timestamp
does not cover presence or store requests. No active families is healthy; a family
that has never been read gets an initial grace period. Reads become delayed after
twice `POLL_INTERVAL_SECONDS` plus 60 seconds. Check logs for the affected worker
and database/Steam reachability before restarting. Keep raw logs private: review
and redact account identifiers and upstream errors before sharing them. Do not
post `.env`, tokens, database URLs, or raw database rows for diagnosis.

Group alerts use a durable PostgreSQL queue. `notifications.pending_retry` counts
all nonexpired pending alerts for linked groups; `deferred` is the subset whose
family retry time is still in the future. These counts survive process restarts.
If the queue cannot be queried, both counts are `null`, health fails, and the
report shows a sanitized queue-unavailable error. `null` never means an empty queue.

`in_flight`, `failed_total`, and `last_failure_age_seconds` describe actual group
delivery attempts in this process and reset on restart. Login and approval-request
notifications are not included. Health exposes counts without queued message text
or credentials. A healthy process does not prove every notification arrived; see
the [group-alert delivery runbook](docs/group-alert-delivery.md) for retry delays,
expiry, discarded messages, and the possibility of duplicates.

Database enumeration and per-family failures are logged, and workers retry on
their next scheduled cycle. The affected worker stays unhealthy until a successful
cycle completes. After database access is restored, verify that workers return to
`ok` and family snapshot ages reset. Unexpected worker exits remain unhealthy;
inspect the logged failure before restarting with `docker compose restart bot`.
Refreshes for the same family are serialized, including manual and background
checks, so overlapping requests do not announce the same change twice.

## Back up, update, and restore

Keep **both** a database backup and its matching `.env` in secure storage. The
database contains personal data and encrypted Steam tokens; the encryption key is
only in `.env`. A database dump without its original key cannot restore logins.

Before an update, stop writers and create a consistent backup:

```sh
umask 077
mkdir -p backups/before-update
docker compose stop bot
docker compose exec -T db pg_dump -U bot -d bot --format=custom > backups/before-update/database.dump
docker compose exec -T db pg_restore --list < backups/before-update/database.dump
cp .env backups/before-update/.env
```

Check every command succeeds before continuing. Use a new backup directory for
each update, and copy it to secure storage outside this host. Select the desired
`STEAMILY_VERSION` in `.env`, then update:

```sh
docker compose pull bot
docker compose up -d
docker compose exec -T bot python -m bot.health
docker compose logs --tail=100 bot
```

Wait for startup and a successful Steam read before accepting the update.
Migrations are forward-only. Do not run an older image against a newer schema
unless its compatibility is confirmed. For a failed migration, keep writers
stopped, retain the backup, and inspect the startup error. Legacy duplicate Steam
links have a [separate recovery runbook](docs/steam-account-links.md).

To restore, stop all writers first. The following replaces the current database
contents with the chosen backup; changes after that backup are lost. Save the
current database separately before restoring if it may be needed. Restore the
matching `.env` and select an image compatible with that backup's schema:

```sh
docker compose stop bot
cp backups/before-update/.env .env
chmod 600 .env
docker compose up -d --wait db
docker compose exec -T db pg_restore --clean --if-exists --create --no-owner --exit-on-error \
  -U bot -d postgres < backups/before-update/database.dump
docker compose up -d bot
docker compose exec -T bot python -m bot.health
```

`--create` recreates the archived `bot` database, removing newer migration metadata
that may be absent from an older backup. Stop if `pg_restore` fails. Never use
`docker compose down -v` as an update or repair step: it deletes the PostgreSQL
data volume. Changing the PostgreSQL major version also requires a separate
database upgrade procedure.

## Browser-session upgrades and compatibility checks

Reader selection and credential generations are added by a versioned migration.
Existing encrypted logins and approval states are preserved. A binary from before
browser-session support cannot use newly stored browser credentials. Keep a
matching backup for rollback instead of deleting credentials to fit an older image.

The compatibility probe is opt-in: run `python -m scripts.check_browser_auth
--state-dir /private/path`, then repeat with `--resume` in a separate process.
The state directory must be private. Reports contain statuses and counts, without
credentials, member identities or game titles. The probe exercises reads only.

## Releases for maintainers

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `fix:`
bumps patch, `feat:` bumps minor, and `!`/`BREAKING CHANGE:` bumps major.
[Release Please](https://github.com/googleapis/release-please-action) opens a
release PR updating `pyproject.toml`, the manifest, and `CHANGELOG.md`. The
manifest starts at the existing `1.0.0`; subsequent versions follow the commits.
Merge that reviewed PR to create the `vX.Y.Z` tag and GitHub release. The workflow
then builds the exact tag for amd64 and arm64 and publishes the image as `X.Y.Z`
(and `latest` for stable releases). Normal main pushes continue publishing `main`.

Enable **Settings → Actions → General → Allow GitHub Actions to create and approve
pull requests**. The default `GITHUB_TOKEN` can create release PRs and tags, but
its generated events do not start CI runs. To run CI automatically on those PRs,
configure a GitHub App/user token with the necessary repository permissions as
`RELEASE_PLEASE_TOKEN`. Otherwise, a maintainer must close and reopen the release
PR to trigger normal PR checks before merging. The release workflow calls the
same CI checks for the exact tag before image publication, so it also works
with `GITHUB_TOKEN`.
