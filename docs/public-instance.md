# Using a hosted Steamily instance

Steamily is a free, noncommercial Telegram bot for Steam Families. It is an
independent project, not a Valve or Telegram service. The source is available at
[AuthFailed/steamily](https://github.com/AuthFailed/steamily); running or modifying
it is subject to the [PolyForm Noncommercial license](../LICENSE.md).

## Who runs the instance

Send `/about` to see the instance operator and contact, hosting provider/location,
the server location expected in Steam's login confirmation, and the source link.
These details describe the instance you are using; the source repository alone
does not identify its operator. An unset value is shown as unknown. Ask the
operator for missing details before authorizing a login.

Operators set `INSTANCE_OPERATOR`, `INSTANCE_HOSTING`, and `LOGIN_LOCATION` in
their environment; see [.env.example](../.env.example). Use plain text, including
an operator contact, the actual hosting provider and country, and the location
observed in Steam's confirmation. Do not publish secrets in these values. The bot
cannot verify this information automatically. Operators must also disclose any
deployment-specific log and backup retention to users; this repository does not
set a log or backup expiry policy.

## Steam login and account safety

The QR code authorizes a Steam website session labelled **Steamily** on the operator's server. You approve it in
the official Steam app; the bot does not ask for or store your Steam password.
The bot stores an encrypted refresh token so it can access Steam without asking
you to approve every check. Encryption protects the stored token, but the running
bot and an operator with its database and encryption key can use it. Treat this
as granting account access to the operator, and authorize only an instance you
trust. This is website account access, not a permission limited to family data.

Steam may show an unusual sign-in warning or restrict or lock your account after
this server login. The confirmation can show a different country from your own.
Check the location against `/about`, and reject any login you did not initiate or
cannot explain. The bot cannot guarantee that Steam will accept the login or that
the account will remain unrestricted.

**Settings → Log out** or `/delete` revokes the selected Steam session before deleting
your bot data. Other devices remain signed in. If Steam revocation fails, the bot
keeps the local login for retry. You can also remove the session manually:

1. Open Steam's [Authorized Devices](https://store.steampowered.com/account/authorizeddevices)
   while signed in. Review the server session and remove its access. You can also
   review authorized devices in the official Steam Mobile app.
2. If you cannot identify the session, or suspect unauthorized access, use
   **Sign out everywhere** and follow Steam's
   [account security recommendations](https://help.steampowered.com/en/faqs/view/6639-EB3C-EC79-FF60).
   This also affects your other Steam sessions. Steam documents deauthorizing
   devices under **Account Details → Manage Steam Guard** in its
   [Steam Guard guide](https://help.steampowered.com/en/faqs/view/06B0-26E6-2CF8-254C).
3. Log out of the bot as described below to delete its local copy of your data.

## Data, purpose, and retention

Send `/privacy` for the in-bot summary. The application stores the following:

| Data | Why it is used | Retention |
| --- | --- | --- |
| Telegram user ID, username and display name; linked SteamID, family ID, registration time and approval status | Identify the linked account, control access, and show Telegram profile links when the member permits publication | Until logout or administrative removal |
| Encrypted Steam refresh token, token renewal time, temporary access token | Authenticate Steam checks and requested family actions | Until logout or administrative removal; the persisted refresh token is also cleared if Steam rejects it |
| Language, notification and publication preferences, publication version and Steam store country | Render the bot and local prices, deliver selected alerts, and reject work using withdrawn publication consent | Until logout or administrative removal |
| Saved dialog state and data, including login/menu state and temporary user input | Keep dialogs working across updates and restarts | Replaced as dialogs change; all of the user's saved dialog rows are deleted on logout or administrative removal |
| Telegram or Steam IDs selected in another active user's saved family-management dialog | Keep that user's member selection while they navigate | Retained in that viewer's dialog until it is closed or reset, or the viewer logs out; does not restore the selected person's account or credentials |
| Current family name/ID, members and roles, invitations, profile names/links, online/game presence, shared library and ownership/acquisition data, and playtime supplied by Steam | Show family information and statistics, detect changes, and send alerts | The latest snapshot replaces the previous one; retained while at least one account remains registered for the family |
| Previously seen family members' SteamIDs and game-to-owner SteamID mappings | Avoid repeating new-game alerts, including after a restart | Accumulates while the family has registered accounts; deleted when the last account is removed |
| Rendered family charts, which can contain Steam names and family statistics | Send statistics images to Telegram | Cached for the active family; removed when its last account is removed or changes families, and cleared on process startup |
| Family owner role, Telegram notification chat ID, family language and alert settings | Manage the bot and route family notifications | Shared with the family; removed when the last registered account is removed |
| Pending group alerts, including family member names and game events | Retry delivery after Telegram failures and bot restarts | Until delivered, discarded with a destination change, or removed with the last family account; online/playing alerts expire after 5 minutes |
| Local health report with family IDs, check ages and worker/queue counters; no Telegram IDs, SteamIDs or credentials | Diagnose process health | Removed families disappear on the next monitor write (normally every 5 seconds); after a crash the stale file remains until startup clears it |
| Public store metadata by country and game ID, such as prices, tags and categories | Show and filter games without fetching every item repeatedly | Shared cache, refreshed when needed; contains no Telegram or Steam account IDs and remains after logout |

The bot keeps a current snapshot and the shared member/ownership detection
history described above, with no fixed expiry while a family has registered
accounts. It does not archive every old snapshot or sent notification. The
current Steam snapshot can also contain older acquisition, membership and
playtime information supplied by Steam.

Family information is visible to the family's authorized bot users and can be
sent to its configured Telegram notification chat. Steam supplies this data for
the whole family, including people who have not registered with the bot.
Publishing a member's presence or Telegram identity requires that member's
separate permission in Settings; both default to off, including after upgrade.
Unregistered members cannot opt in through someone else. Logout or loss of family
access resets these permissions. New cards, inline answers and pending presence
alerts check current consent, but already delivered messages cannot reliably be
recalled. See [member publication preferences](publication.md) for details. Family
data and detection history about a person may therefore remain or be fetched
again through another registered family member after that person logs out. This
does not retain their bot account, Telegram profile link, settings or credentials.
Another active user's saved dialog may still refer to a selected Telegram or
Steam ID until that viewer closes or resets the dialog or logs out. A stale
selection cannot restore the deleted account or its Telegram profile mapping.

The host operator controls the server, database and encryption key. Telegram and
Steam process the requests and messages sent to their services. Operational logs
can contain identifiers and errors; external logging, database backups and their
retention depend on the deployment. Local deletion does not erase copies in
Telegram chats, Steam's own records, or pre-existing operator backups or logs.
Contact the operator for their handling and retention of those copies. Operators
restoring a backup must honor deletions before putting the restored data into
service.

## Delete your bot data

Send `/delete` or open **Settings → Log out**, then confirm. This also works when
your Steam session has expired or you have only chosen preferences and never
linked Steam. Logging out deletes the local account and encrypted token, Telegram
profile reference, preferences, and saved dialog state. It also cancels a pending
login and invalidates cached credentials and user-specific runtime data.
Administrative removal uses the same deletion rules.

If you are the family's bot owner, the confirmation explains how ownership is
passed on. If another account remains registered, shared family data and its
notification configuration stay available to them; your own Telegram profile
reference is removed. When the last registered account is removed, the abandoned
family record and its cached snapshot are deleted. The anonymous public store
cache remains. Logging out does not make you leave your Steam Family. Administrative
removal deletes local data without revoking the session at Steam; remove that
session through Authorized Devices if needed.

After deletion, old dialogs cannot restore your account. Starting again creates
new preferences and requires a fresh Steam login and any necessary approval.
If you cannot access your Telegram account, contact the instance operator to
verify ownership and arrange removal.

Any future user-linked backlog marks, transfer requests or votes must follow the
same logout and administrative-removal rules. They are not exceptions to this
policy and are not implemented by this document.

## Status and terms

Send `/status` to check whether the bot responds and see the last successful
Steam family check in UTC. For a registered account this is its own family;
without an account it is the latest family check across the instance. Timestamps
reset when the bot restarts. A response confirms that this process handled the
command; it does not guarantee that Telegram delivery, every family or Steam is
healthy. An old timestamp or no successful check means current Steam availability
is unknown.

Use of this instance is free and noncommercial, consistent with the software's
[license](../LICENSE.md). No uptime or account-safety guarantee is offered. The
operator may stop hosting the instance. Use only accounts you are authorized to
control, respect the family's access decisions, and do not share the bot's login
QR code or credentials with other people.
