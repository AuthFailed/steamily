# Shared-library fixtures

## Protocol examples

`shared_library_apps.json` is a synthetic, protocol-based Steam
`GetSharedLibraryApps` response, not a recorded response from a real account.
Its shape and `exclude_reason` semantics follow
[Steam's family-groups protobuf schema](https://github.com/SteamDatabase/Protobufs/blob/master/steam/steammessages_familygroups.steamclient.proto).

The fixture covers explicit inclusion (`0`), exclusion (`1`), an omitted optional
reason (protobuf default `0`), and a synthetic unknown nonzero reason (`999`).
App IDs, names, and owner IDs are invented. Owners `a` and `b` own every fixture
game; family member `c` is a potential borrower. Exclusions prevent borrowing,
while owners keep access to their own copies.

## Recorded Steam response

`steam/shared_library_recorded.json` contains two app rows recorded from a live
`IFamilyGroupsService/GetSharedLibraryApps/v1` response on
2026-10-17 at 01:30:11 UTC, plus one explicitly derived boundary case. The request
used `include_own=true`, `include_excluded=true`, `include_free=false`,
`include_non_games=false`, and `language=english`.

Rows `201` and `202` preserve the observed owner relationships, owner counts,
`exclude_reason` values (`0` and `1`), and app type. App IDs, titles, Steam IDs,
and nonzero acquisition timestamps were replaced. Fields outside the parser's
inputs and app type were removed. No credentials, family IDs, profile details,
or original game identities are retained. Owner `a` owns both recorded titles;
owner `b` owns only the included title.

Steam returned an explicit `exclude_reason` for every app in this capture. Row
`203` is therefore a boundary variant derived from recorded row `201`, with
`name`, `exclude_reason`, and `rt_time_acquired` deliberately omitted to test
optional-field defaults. It is not evidence that Steam omitted those fields in
the live response. The original synthetic examples remain useful for unknown
future exclusion codes.
