# Group alert delivery

Observed library/membership state and its outgoing messages commit together in
Postgres. Pending acquisition, membership and removal alerts survive restarts
and are delivered even when the family has no working Steam login. Refreshes
and deliveries are serialized per family in the single bot process.

Delivery checks run every 5 seconds, with at most 20 messages per family per
pass. Network/server errors wait 60 seconds; Telegram flood-control errors wait
at least `retry_after` seconds. The family delay persists across restarts and
applies to newly queued events too.

Online/playing alerts expire after 5 minutes. Each member has at most one pending
online alert and one playing alert; a newer transition replaces the older one.
These alerts describe the latest transition rather than replaying old sessions.
They require the member's publication permission. Every delivery and retry checks
fresh membership, current consent/version and the current group link; stale consent
or missing legacy metadata discards the presence alert. See [publication preferences](publication.md).

When Telegram reports a removed/blocked/missing group, the bot logs the failure,
unlinks that destination and discards its pending messages. Other permanent
message errors discard the rejected message with a warning and continue the
queue. Manually unlinking or changing a linked group also discards the old
destination's backlog; linking the same group again preserves it.

Group-to-supergroup service messages and API migration responses move only a
destination that still matches the old chat ID. Pending event IDs and retry
delays are preserved. Duplicate or stale notices do nothing, and migration
cannot take a destination already linked to another family. An API migration
response follows the normal delayed retry path; an occupied destination is
logged and the old link is removed.

Delivery is at least once for retryable failures, with no exactly-once promise:
if Telegram accepts a message but the acknowledgement is lost, a retry can
produce a duplicate. Coordination assumes one running bot process.
