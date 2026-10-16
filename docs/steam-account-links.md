# Recovering Steam account links

Each Steam account can be linked to one Telegram account across the whole bot.
Pending, approved and blocked accounts reserve the link, including accounts whose
Steam token has expired or been removed. Signing in again from the same Telegram
account is allowed. A rejected duplicate login does not replace the existing link.

## Moving your own link

Use the Telegram account that originally linked Steam. Open **Settings → Log out**
and confirm, then sign in from the other Telegram account. Logout forgets that
Telegram account's bot login and settings. Family owners must review the logout
warning: the existing logout flow hands ownership to another registered member,
or leaves the family without an owner when nobody else is registered.

If you cannot access the original Telegram account, ask the operator to verify
ownership and review the stored link. Logging in elsewhere never takes it over.

## Startup fails with legacy duplicate links

Startup checks existing accounts before creating a unique index on `steam_id`.
When duplicates exist, startup fails with this runbook's path. It does not choose
a winning account, delete accounts or move family ownership. The migration holds
an accounts-table lock across the check and index creation to prevent a write race.

1. Stop **all bot instances and other database writers**, including older bot
   versions. Keep them stopped throughout review and repair. With Docker Compose:

   ```sh
   docker compose stop bot
   docker compose exec -T db pg_dump -U bot -d bot --format=custom > before-steam-link-repair.dump
   docker compose exec -T db pg_restore --list < before-steam-link-repair.dump
   ```

   Check that both backup commands succeed. Store the backup securely; it contains
   account data and encrypted tokens. Keep the existing `SECRET_KEY` securely too.
   Adapt the database/user names for installations that do not use `compose.yaml`.

2. Run these **read-only** queries in the affected database. They do not reveal
   tokens. Review every duplicate group, its Telegram users and family owners:

   ```sql
   SELECT steam_id, array_agg(telegram_id ORDER BY telegram_id) AS telegram_ids
   FROM accounts
   GROUP BY steam_id
   HAVING count(*) > 1
   ORDER BY steam_id;

   SELECT a.steam_id, a.telegram_id, a.family_id, a.status, a.created_at,
          a.tg_username, a.tg_name, a.refresh_token IS NOT NULL AS has_token,
          f.owner_telegram_id
   FROM accounts a
   JOIN families f USING (family_id)
   WHERE a.steam_id IN (
       SELECT steam_id FROM accounts GROUP BY steam_id HAVING count(*) > 1
   )
   ORDER BY a.steam_id, a.telegram_id;

   SELECT family_id, owner_telegram_id
   FROM families
   WHERE owner_telegram_id IN (
       SELECT telegram_id FROM accounts
       WHERE steam_id IN (
           SELECT steam_id FROM accounts GROUP BY steam_id HAVING count(*) > 1
       )
   );
   ```

3. Verify the intended Telegram user with the affected people and explicitly
   record which account row to keep and which rows to remove. Age, status and
   token availability do not determine a winner. Keep family owner links when
   possible. If a reviewed losing account owns any family, resolve ownership
   separately with the affected family first. Explicitly choose and verify any
   replacement's family membership and approval, or agree to leave it ownerless.
   Do not automatically transfer or clear ownership. The removal below refuses
   to delete a family owner.

4. Open an interactive SQL session (`docker compose exec db psql -U bot -d bot`).
   Replace these **example values** with one reviewed losing Telegram account and
   its exact Steam ID. Remove only that account row inside a transaction:

   ```sql
   \set losing_telegram_id 222222222
   \set expected_steam_id '76561198000000000'

   BEGIN;
   LOCK TABLE accounts IN SHARE ROW EXCLUSIVE MODE;
   SELECT telegram_id, steam_id, family_id, status
   FROM accounts
   WHERE telegram_id = :losing_telegram_id
   FOR UPDATE;

   DELETE FROM accounts a
   WHERE a.telegram_id = :losing_telegram_id
     AND a.steam_id = :'expected_steam_id'
     AND NOT EXISTS (
         SELECT 1 FROM families f WHERE f.owner_telegram_id = a.telegram_id
     )
   RETURNING telegram_id, steam_id, family_id;
   ```

   Verify that exactly the reviewed row was returned. Run `COMMIT;` only after
   checking it; otherwise run `ROLLBACK;` and investigate. `DELETE 0` can mean the
   account is a family owner or the supplied IDs do not match. This removes the
   losing login, token and account profile; it keeps family data and Telegram user
   preferences. Do not delete families, bulk-delete duplicate groups or copy
   tokens between accounts. Repeat only for separately reviewed losing rows.

5. Repeat the duplicate query; it must return no rows. Start the updated bot
   (`docker compose up -d bot`) and check startup logs. Schema setup is idempotent;
   restarting after explicit repair creates the unique index. Confirm that the
   retained Telegram user can log in and another Telegram account is rejected.

If review or verification is incomplete, keep writers stopped and retain the
backup. Do not drop the unique index to make an older version accept duplicate
links. A version rollback must preserve the constraint; restoring a database
backup is a separate, explicitly reviewed recovery operation.
