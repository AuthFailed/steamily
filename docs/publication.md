# Member publication preferences

In Settings, an approved member can independently allow the bot to publish their presence
(online, last seen and current game) and their Telegram identity. Both permissions default
to **off**, including existing accounts after upgrade. Unregistered Steam relatives cannot
opt in through another member or the family owner. Recipient/group alert settings remain
separate and cannot grant publication permission.

Steam names, profile links, library ownership and historical statistics remain visible
according to Steam privacy settings. A member can still see their own presence privately.
These controls affect disclosures made by the bot, not Steam itself.

Logout, account deletion, family departure, changing the linked Steam account/family, and
blocking or withdrawing bot approval reset both permissions. Returning requires new consent.
An expired Steam token suppresses publication until reauthentication; logging in again with
the same Steam account and family preserves the choices unless access was revoked.

Every consent change or reset replaces an account's publication version. Buttons and pending
presence events carry that version, so old work cannot become valid again after opting out
and back in, or leaving and rejoining. Presence delivery checks fresh Steam membership and
the current account, version, consent and group settings immediately before sending. If
membership cannot be verified, the event is not published. This does not introduce retries
or a persistent notification queue.

Rendered Telegram links and presence are rebuilt from current account rows for every newly
opened card. Generated charts use Steam names and contain no Telegram identities or live
presence; store and Steam-statistics caches contain no rendered Telegram identity. Inline
answers are personal with `cache_time=0`; after slow store requests, the bot rechecks the
sender and reads owners' current preferences, including when it generates fallback cards.
Privacy changes therefore invalidate the bot's pending work without retaining a rendered
identity cache. On upgrade, previously cached Telegram inline answers may remain until
their former TTL (up to 60 seconds) expires.

Already delivered or forwarded messages, currently displayed cards and previously offered
inline results cannot reliably be recalled or changed. Reopening a card or making a new
inline query applies the latest choices.
