# Steam response fixtures

Tests replay these files locally; they never contact Steam or require a real
account. Recorded samples are allowlisted projections of actual replies, with
private identifiers and credentials replaced before writing files. Synthetic
examples remain separate and cover cases absent from the recordings.

## Recorded responses

The following files were captured through an authorized session on the bot
server on **2026-10-17 at 01:44:18 UTC**:

| File | Steam response |
| --- | --- |
| `family_recorded.json` | `IFamilyGroupsService/GetFamilyGroupForUser/v1` |
| `library_snapshot_recorded.json` | `IFamilyGroupsService/GetSharedLibraryApps/v1` |
| `players_recorded.json` | `IPlayerService/GetPlayerLinkDetails/v1` |
| `playtime_recorded.json` | POST `IFamilyGroupsService/GetPlaytimeSummary/v1` |
| `store_recorded.json` | `IStoreBrowseService/GetItems/v1` |
| `auth_recorded.json` | Browser renewal redirects/JSON, anonymous QR begin/pending protobuf, and invalid-token rejection |

The family, library, player and playtime files share one mapping. Family ID,
Steam IDs, names, vanity names, library app IDs/titles and nonzero private
timestamps were replaced. Original field types, missing fields, empty strings,
roles, ownership, exclusions, presence and playtime values were preserved.
Unused fields were dropped. All five members were retained; three library rows
were selected for observed playtime, exclusion and multiple owners. The playtime
file retains the observed entry matching a selected app. The library request used
`include_own=true`, `include_excluded=true`, `include_free=false`,
`include_non_games=false`, and `language=english`.

Store items are public Portal 2 (`620`) and Counter-Strike 2 (`730`) data requested
in English for `US`, with basic info, assets and 20 tags. Their public names,
prices, tags, categories, descriptions and asset paths were retained; unused
fields were dropped. No personal library association is implied.

Authentication recording used separate sessions for browser renewal and an
anonymous, unapproved QR attempt. QR client/request IDs and challenge URL were
replaced; only consumed protobuf fields were retained, preserving the observed
float32 polling interval. The pending response was empty. The recorded rejection
is HTTP 200 / EResult 15 from an actual request using the deliberately invalid
value `recorded-invalid-refresh`, not a rejected user's credential.

The renewal chain preserves the observed HTTP statuses, Steam host/path redirects
and JSON structure. Redirect queries and unused headers/fields were removed.
JWT subjects/expiry were replaced and signatures deliberately made invalid;
`aud` preserves the observed access-token audience. The fixture refresh cookie is
entirely synthetic. No replacement refresh cookie was issued in this capture.
Real credentials and raw private replies were held only in the capture process's
memory and were never written to fixture files.

These samples do not claim that QR approval, QR rotation, refresh-cookie rotation,
every auth failure, or all optional API shapes were observed live. Existing
synthetic tests cover those boundary cases.

`shared_library_recorded.json` is an earlier sanitized capture for issue #12,
with an explicitly derived missing-metadata variant; see
[its provenance and sanitization details](../README.md#recorded-steam-response).
Its independent member/app mapping is not shared with the newer snapshot files.

## Play-mode category provenance (issue #19)

Public Store feature links (`category2` values) were checked on **2026-10-17**:
[Portal 2](https://store.steampowered.com/app/620/Portal_2/?l=english) identifies
`38` as Online Co-op, `39` as Shared/Split Screen Co-op, `24` as Shared/Split
Screen, and `44` as Remote Play Together.
[Brawlhalla](https://store.steampowered.com/app/291550/Brawlhalla/?l=english)
identifies `36` as Online PvP and `37` as Shared/Split Screen PvP.

The recorded Portal 2 response contains `38`, `39`, and `24` under
`supported_player_categoryids`, and `44` under `feature_categoryids`. The
recorded Counter-Strike 2 player categories are `[1, 27]`; these do not assert
exact Online PvP support. Categories `36` and `37` were verified through the
public Store links, are absent from these recordings, and are covered by
synthetic tests. No recorded response was changed to add categories.

## Synthetic examples

Files without `_recorded` in their name were authored from the fields consumed
by `bot/steam.py`, `bot/steam_auth.py`, and `bot/steam_browser.py`; they are not
recordings. No account, session, QR code or token in them is real.

`auth.json` stores fixed protobuf request/response bytes as hexadecimal. Tests
replay them through a local HTTP server without using the production encoder to
construct expected wire messages. It includes QR creation with/without an
interval, pending approval, QR rotation, approval, legacy token renewal, missing
access tokens and EResult rejection. Other synthetic JSON files cover optional
and missing fields in family, library, player, playtime and store responses.
