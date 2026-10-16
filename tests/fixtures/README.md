# Shared-library fixture

`shared_library_apps.json` is a synthetic, protocol-based Steam
`GetSharedLibraryApps` response, not a recorded response from a real account.
Its shape and `exclude_reason` semantics follow
[Steam's family-groups protobuf schema](https://github.com/SteamDatabase/Protobufs/blob/master/steam/steammessages_familygroups.steamclient.proto).

The fixture covers explicit inclusion (`0`), exclusion (`1`), an omitted optional
reason (protobuf default `0`), and a synthetic unknown nonzero reason (`999`).
App IDs, names, and owner IDs are invented. Owners `a` and `b` own every fixture
game; family member `c` is a potential borrower. Exclusions prevent borrowing,
while owners keep access to their own copies.
