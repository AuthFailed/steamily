"""Postgres: accounts, families, store cache and aiogram FSM state."""

import json
from collections.abc import Mapping
from typing import Any

import asyncpg
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from cryptography.fernet import Fernet

from bot.migrations import migrate


class SteamAlreadyLinked(ValueError):
    """The Steam account is already linked to another Telegram account."""


class Db:
    def __init__(self, pool: asyncpg.Pool, secret_key: str):
        self.pool = pool
        self.fernet = Fernet(secret_key)

    @classmethod
    async def connect(cls, dsn: str, secret_key: str) -> "Db":
        async def init(connection):
            await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

        pool = await asyncpg.create_pool(dsn, init=init)
        try:
            async with pool.acquire() as connection:
                await migrate(connection)
            return cls(pool, secret_key)
        except BaseException:
            await pool.close()
            raise

    # Telegram users' language, chosen before anything else
    async def language(self, telegram_id: int) -> str | None:
        return await self.pool.fetchval("SELECT language FROM telegram_users WHERE telegram_id = $1", telegram_id)

    async def notify_requests(self, telegram_id: int) -> bool:
        return (
            await self.pool.fetchval("SELECT notify_requests FROM telegram_users WHERE telegram_id = $1", telegram_id)
            is not False
        )

    async def set_notify_requests(self, telegram_id: int, on: bool) -> None:
        await self.pool.execute(
            "UPDATE telegram_users SET notify_requests = $2 WHERE telegram_id = $1", telegram_id, on
        )

    async def delete_user(self, telegram_id: int) -> None:
        """Logout: forgets the Steam login and every setting of this Telegram user."""
        if not await self.delete_account(telegram_id):
            async with self.pool.acquire() as connection, connection.transaction():
                await self._delete_personal_data(connection, telegram_id, None)

    async def delete_dialogs(self, telegram_id: int) -> None:
        await self.pool.execute("DELETE FROM fsm WHERE split_part(key, ':', 3) = $1", str(telegram_id))

    async def _delete_personal_data(
        self, connection: asyncpg.Connection, telegram_id: int, family_id: str | None
    ) -> None:
        await connection.execute("DELETE FROM owner_transfers WHERE owner_id = $1 OR target_id = $1", telegram_id)
        await connection.execute("DELETE FROM telegram_users WHERE telegram_id = $1", telegram_id)
        await connection.execute("DELETE FROM fsm WHERE split_part(key, ':', 3) = $1", str(telegram_id))
        if family_id is not None:
            await connection.execute(
                "UPDATE families SET reader_telegram_id = NULL WHERE family_id = $1 AND reader_telegram_id = $2",
                family_id,
                telegram_id,
            )
            await connection.execute(
                "UPDATE families SET owner_telegram_id = NULL WHERE family_id = $1 AND owner_telegram_id = $2",
                family_id,
                telegram_id,
            )
            await connection.execute(
                "DELETE FROM families WHERE family_id = $1 AND NOT EXISTS "
                "(SELECT 1 FROM accounts WHERE family_id = $1)",
                family_id,
            )

    async def set_language(self, telegram_id: int, language: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO telegram_users (telegram_id, language) VALUES ($1, $2)
            ON CONFLICT (telegram_id) DO UPDATE SET language = $2
        """,
            telegram_id,
            language,
        )

    # Personal game status
    async def game_marks(self, telegram_id: int) -> dict[int, dict]:
        rows = await self.pool.fetch(
            "SELECT g.appid, g.name, g.status FROM game_statuses g "
            "JOIN accounts a USING (telegram_id) WHERE telegram_id = $1 AND a.status = 'approved'",
            telegram_id,
        )
        return {row["appid"]: {"name": row["name"], "status": row["status"]} for row in rows}

    async def set_game_status(
        self,
        telegram_id: int,
        steam_id: str,
        family_id: str,
        appid: int,
        name: str,
        status: str | None,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        if type(appid) is not int or not 0 < appid <= 4294967295:
            raise ValueError("Invalid Steam app ID")
        if not isinstance(name, str) or not name.strip() or "\0" in name:
            raise ValueError("Invalid game name")
        if status not in (None, "backlog", "playing", "completed"):
            raise ValueError("Invalid game status")
        async with self.pool.acquire() as connection, connection.transaction():
            # Serialize with logout and relinking so a stale click cannot recreate personal data.
            account = await connection.fetchval(
                "SELECT telegram_id FROM accounts WHERE telegram_id = $1 AND steam_id = $2 "
                "AND family_id = $3 AND status = 'approved' "
                "AND ($4::bigint IS NULL OR token_generation = $4) FOR UPDATE",
                telegram_id,
                steam_id,
                family_id,
                expected_generation,
            )
            if account is None:
                return False
            if status is None:
                await connection.execute(
                    "DELETE FROM game_statuses WHERE telegram_id = $1 AND appid = $2", telegram_id, appid
                )
            else:
                await connection.execute(
                    "INSERT INTO game_statuses (telegram_id, appid, name, status) VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (telegram_id, appid) DO UPDATE SET name = $3, status = $4",
                    telegram_id,
                    appid,
                    name,
                    status,
                )
            return True

    # Accounts
    async def account(self, telegram_id: int) -> asyncpg.Record | None:
        """The account plus its family's owner_telegram_id."""
        return await self.pool.fetchrow(
            """
            SELECT a.*, f.owner_telegram_id FROM accounts a JOIN families f USING (family_id) WHERE telegram_id = $1
        """,
            telegram_id,
        )

    async def publication_account(self, telegram_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """
            SELECT a.*, f.notify_chat, f.alerts, f.alert_link_version, f.language
            FROM accounts a JOIN families f USING (family_id) WHERE telegram_id = $1
            """,
            telegram_id,
        )

    async def family_accounts(self, family_id: str) -> list[asyncpg.Record]:
        return await self.pool.fetch("SELECT * FROM accounts WHERE family_id = $1 ORDER BY created_at", family_id)

    async def owner_steam_id(self, family_id: str) -> str | None:
        return await self.pool.fetchval(
            """
            SELECT a.steam_id FROM families f JOIN accounts a ON a.telegram_id = f.owner_telegram_id
            WHERE f.family_id = $1
            """,
            family_id,
        )

    async def check_steam_link(self, telegram_id: int, steam_id: str) -> None:
        if await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM accounts WHERE steam_id = $1 AND telegram_id <> $2)", steam_id, telegram_id
        ):
            raise SteamAlreadyLinked("Steam account is already linked to another Telegram account.")

    async def save_account(
        self,
        telegram_id: int,
        steam_id: str,
        family_id: str,
        refresh_token: str,
        country: str | None = None,
        token_renewed_at: float = 0.0,
        *,
        claim_owner: bool = False,
    ) -> None:
        await self.check_steam_link(telegram_id, steam_id)
        try:
            # Approval, ownership and publication consent belong to one Steam account in one family.
            async with self.pool.acquire() as connection, connection.transaction():
                if claim_owner:
                    await connection.fetchval(
                        "SELECT family_id FROM families WHERE family_id = $1 FOR UPDATE", family_id
                    )
                await connection.execute(
                    """
            WITH cleared_owner AS (
                UPDATE families f SET
                    owner_telegram_id = CASE WHEN owner_telegram_id = $1 THEN NULL ELSE owner_telegram_id END,
                    reader_telegram_id = CASE WHEN reader_telegram_id = $1 THEN NULL ELSE reader_telegram_id END
                WHERE (owner_telegram_id = $1 OR reader_telegram_id = $1) AND EXISTS (
                    SELECT 1 FROM accounts a WHERE a.telegram_id = $1 AND a.family_id = f.family_id
                    AND (a.family_id <> $3 OR a.steam_id <> $2)
                ) RETURNING family_id
            )
            INSERT INTO accounts
                (telegram_id, steam_id, family_id, refresh_token, country, token_renewed_at, token_generation)
            SELECT $1, $2, $3, $4, $5, $6, nextval('steamily_token_generation')
            FROM (SELECT count(*) FROM cleared_owner) AS cleared
            ON CONFLICT (telegram_id) DO UPDATE SET family_id = $3, refresh_token = $4, country = $5,
                token_renewed_at = $6, token_generation = EXCLUDED.token_generation,
                status = CASE WHEN accounts.steam_id = $2 AND accounts.family_id = $3
                    THEN accounts.status ELSE 'pending' END,
                publish_presence = CASE WHEN accounts.steam_id = $2 AND accounts.family_id = $3
                    THEN accounts.publish_presence ELSE FALSE END,
                publish_telegram = CASE WHEN accounts.steam_id = $2 AND accounts.family_id = $3
                    THEN accounts.publish_telegram ELSE FALSE END,
                publication_version = CASE WHEN accounts.steam_id = $2 AND accounts.family_id = $3
                    THEN accounts.publication_version ELSE gen_random_uuid()::text END, steam_id = $2
            """,
                    telegram_id,
                    steam_id,
                    family_id,
                    self.fernet.encrypt(refresh_token.encode()),
                    country,
                    token_renewed_at,
                )
                if claim_owner:
                    claimed = await connection.fetchval(
                        "UPDATE families SET owner_telegram_id = $2 WHERE family_id = $1 "
                        "AND owner_telegram_id IS NULL AND EXISTS (SELECT 1 FROM accounts "
                        "WHERE telegram_id = $2 AND family_id = $1 AND status <> 'blocked') "
                        "RETURNING owner_telegram_id",
                        family_id,
                        telegram_id,
                    )
                    if claimed is not None:
                        await connection.execute(
                            "UPDATE accounts SET status = 'approved' WHERE telegram_id = $1", telegram_id
                        )
        except asyncpg.UniqueViolationError as error:
            if error.constraint_name != "accounts_steam_id_key":
                raise
            raise SteamAlreadyLinked("Steam account is already linked to another Telegram account.") from error

    async def revoke_account(
        self, telegram_id: int, steam_id: str, family_id: str, *, expected_generation: int | None = None
    ) -> bool:
        result = await self.pool.execute(
            """
            UPDATE accounts SET status = CASE WHEN status = 'approved' THEN 'pending' ELSE status END,
                refresh_token = NULL, token_renewed_at = 0,
                token_generation = nextval('steamily_token_generation'),
                publish_presence = FALSE, publish_telegram = FALSE,
                publication_version = gen_random_uuid()::text
            WHERE telegram_id = $1 AND steam_id = $2 AND family_id = $3
                AND ($4::bigint IS NULL OR token_generation = $4)
            """,
            telegram_id,
            steam_id,
            family_id,
            expected_generation,
        )
        return result == "UPDATE 1"

    async def set_refresh_token(
        self,
        telegram_id: int,
        refresh_token: str | None,
        token_renewed_at: float = 0.0,
        *,
        expected_refresh_token: bytes | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        token = self.fernet.encrypt(refresh_token.encode()) if refresh_token else None
        saved = await self.pool.fetchval(
            "UPDATE accounts SET refresh_token = $2, token_renewed_at = $3, "
            "token_generation = CASE WHEN $2::bytea IS NULL THEN nextval('steamily_token_generation') "
            "ELSE token_generation END WHERE telegram_id = $1 "
            "AND ($4::bytea IS NULL OR refresh_token = $4) "
            "AND ($5::bigint IS NULL OR token_generation = $5) RETURNING telegram_id",
            telegram_id,
            token,
            token_renewed_at,
            expected_refresh_token,
            expected_generation,
        )
        return saved is not None

    def refresh_token(self, account: asyncpg.Record) -> str | None:
        return self.fernet.decrypt(account["refresh_token"]).decode() if account["refresh_token"] else None

    async def set_telegram_profile(self, telegram_id: int, username: str | None, name: str) -> None:
        await self.pool.execute(
            "UPDATE accounts SET tg_username = $2, tg_name = $3 WHERE telegram_id = $1", telegram_id, username, name
        )

    async def set_country(self, telegram_id: int, country: str) -> None:
        await self.pool.execute("UPDATE accounts SET country = $2 WHERE telegram_id = $1", telegram_id, country)

    async def set_publication(
        self, telegram_id: int, steam_id: str, family_id: str, setting: str, on: bool, *, expected_version: str
    ) -> bool:
        if setting not in {"publish_presence", "publish_telegram"}:
            return False
        return (
            await self.pool.fetchval(
                f"""
            UPDATE accounts SET {setting} = $4,
                publication_version = CASE WHEN {setting} IS DISTINCT FROM $4
                    THEN gen_random_uuid()::text ELSE publication_version END
            WHERE telegram_id = $1 AND steam_id = $2 AND family_id = $3
                AND status = 'approved' AND refresh_token IS NOT NULL AND publication_version = $5
            RETURNING telegram_id
            """,
                telegram_id,
                steam_id,
                family_id,
                on,
                expected_version,
            )
            is not None
        )

    async def set_status(
        self, telegram_id: int, status: str, *, family_id: str | None = None, members: list[str] | None = None
    ) -> bool:
        return (
            await self.pool.fetchval(
                """
            UPDATE accounts SET status = $2,
                publish_presence = CASE WHEN $2 = 'approved' THEN publish_presence ELSE FALSE END,
                publish_telegram = CASE WHEN $2 = 'approved' THEN publish_telegram ELSE FALSE END,
                publication_version = CASE WHEN $2 = 'approved'
                    THEN publication_version ELSE gen_random_uuid()::text END
            WHERE telegram_id = $1
                AND ($3::text IS NULL OR family_id = $3)
                AND ($4::text[] IS NULL OR steam_id = ANY($4))
            RETURNING telegram_id
            """,
                telegram_id,
                status,
                family_id,
                members,
            )
            is not None
        )

    async def delete_account(self, telegram_id: int, *, family_id: str | None = None) -> bool:
        async with self.pool.acquire() as connection:
            while True:
                async with connection.transaction():
                    registered_family = await connection.fetchval(
                        "SELECT family_id FROM accounts WHERE telegram_id = $1 "
                        "AND ($2::text IS NULL OR family_id = $2)",
                        telegram_id,
                        family_id,
                    )
                    if registered_family is None:
                        return False
                    # Match membership reconciliation's family-before-account lock order.
                    await connection.fetchval(
                        "SELECT family_id FROM families WHERE family_id = $1 FOR UPDATE", registered_family
                    )
                    removed_family = await connection.fetchval(
                        "DELETE FROM accounts WHERE telegram_id = $1 AND family_id = $2 RETURNING family_id",
                        telegram_id,
                        registered_family,
                    )
                    if removed_family is None:
                        continue  # A concurrent login moved families before the lock; read its current registration.
                    await self._delete_personal_data(connection, telegram_id, removed_family)
                    return True

    async def _game_response_account(self, connection: asyncpg.Connection, account: dict) -> bool:
        # Match membership reconciliation's family-before-account lock order.
        await connection.fetchval(
            "SELECT family_id FROM families WHERE family_id = $1 FOR UPDATE", account.get("family_id")
        )
        return (
            await connection.fetchval(
                "SELECT telegram_id FROM accounts WHERE telegram_id = $1 AND steam_id = $2 AND family_id = $3 "
                "AND created_at = $4 AND publication_version = $5 AND game_response_version = $6 "
                "AND status = 'approved' "
                "AND refresh_token IS NOT NULL FOR UPDATE",
                account.get("telegram_id"),
                account.get("steam_id"),
                account.get("family_id"),
                account.get("created_at"),
                account.get("publication_version"),
                account.get("game_response_version"),
            )
            is not None
        )

    async def set_game_response(self, account: dict, appid: int, response: str | None, *, named: bool = False) -> bool:
        if (
            type(appid) is not int
            or not 0 < appid < 2**32
            or response not in (None, "recommend", "together", "not_interested")
        ):
            return False
        async with self.pool.acquire() as connection, connection.transaction():
            if not await self._game_response_account(connection, account):
                return False
            if response is None:
                await connection.execute(
                    "DELETE FROM game_responses WHERE family_id = $1 AND telegram_id = $2 AND appid = $3",
                    account["family_id"],
                    account["telegram_id"],
                    appid,
                )
            else:
                await connection.execute(
                    "INSERT INTO game_responses (family_id, telegram_id, appid, response, named) "
                    "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (family_id, telegram_id, appid) "
                    "DO UPDATE SET response = EXCLUDED.response, named = EXCLUDED.named",
                    account["family_id"],
                    account["telegram_id"],
                    appid,
                    response,
                    named if response == "recommend" else False,
                )
            await connection.execute(
                "UPDATE accounts SET game_response_version = gen_random_uuid()::text WHERE telegram_id = $1",
                account["telegram_id"],
            )
            return True

    async def game_responses(self, account: dict) -> list[dict]:
        async with self.pool.acquire() as connection, connection.transaction():
            if not await self._game_response_account(connection, account):
                return []
            rows = await connection.fetch(
                "SELECT r.telegram_id, a.steam_id, r.appid, r.response, r.named, a.game_response_version "
                "FROM game_responses r JOIN accounts a ON a.telegram_id = r.telegram_id AND a.family_id = r.family_id "
                "WHERE r.family_id = $1 AND a.status = 'approved' AND a.refresh_token IS NOT NULL "
                "AND (r.response <> 'not_interested' OR r.telegram_id = $2) "
                "ORDER BY r.telegram_id, r.appid",
                account["family_id"],
                account["telegram_id"],
            )
            return [dict(row) for row in rows]

    # Families
    async def family(self, family_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM families WHERE family_id = $1", family_id)

    async def prune_family(self, family_id: str) -> bool:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval("SELECT family_id FROM families WHERE family_id = $1 FOR UPDATE", family_id)
            return (
                await connection.fetchval(
                    "DELETE FROM families WHERE family_id = $1 AND NOT EXISTS "
                    "(SELECT 1 FROM accounts WHERE family_id = $1) RETURNING family_id",
                    family_id,
                )
                is not None
            )

    async def unlink_chat(self, chat_id: int, *, family_id: str | None = None, link_version: int | None = None) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            families = await connection.fetch(
                "SELECT family_id FROM families WHERE notify_chat = $1 AND ($2::text IS NULL OR family_id = $2) "
                "AND ($3::bigint IS NULL OR alert_link_version = $3) FOR UPDATE",
                chat_id,
                family_id,
                link_version,
            )
            family_ids = [family["family_id"] for family in families]
            await connection.execute("DELETE FROM pending_alerts WHERE family_id = ANY($1::text[])", family_ids)
            await connection.execute(
                "UPDATE families SET notify_chat = NULL, alert_retry_at = 0, "
                "alert_link_version = alert_link_version + 1 "
                "WHERE family_id = ANY($1::text[])",
                family_ids,
            )

    async def migrate_chat(
        self, old_chat_id: int, new_chat_id: int, *, family_id: str | None = None, link_version: int | None = None
    ) -> bool:
        if old_chat_id == new_chat_id:
            return False
        async with self.pool.acquire() as connection, connection.transaction():
            # Migration is rare; block concurrent relinks while checking the destination.
            await connection.execute("LOCK TABLE families IN EXCLUSIVE MODE")
            if await connection.fetchval("SELECT EXISTS (SELECT 1 FROM families WHERE notify_chat = $1)", new_chat_id):
                return False
            families = await connection.fetch(
                "UPDATE families SET notify_chat = $2 WHERE notify_chat = $1 "
                "AND ($3::text IS NULL OR family_id = $3) "
                "AND ($4::bigint IS NULL OR alert_link_version = $4) RETURNING family_id",
                old_chat_id,
                new_chat_id,
                family_id,
                link_version,
            )
            await connection.execute(
                "UPDATE pending_alerts SET chat_id = $2 WHERE chat_id = $1 AND family_id = ANY($3::text[])",
                old_chat_id,
                new_chat_id,
                [family["family_id"] for family in families],
            )
            return bool(families)

    async def _enqueue_alerts(self, connection, family_id: str, chat_id: int | None, alerts: list[dict]) -> None:
        if chat_id is None:
            return
        for alert in alerts:
            if alert.get("coalesce_key") is not None:
                # A new identity keeps an in-flight acknowledgement from deleting its replacement.
                await connection.execute(
                    "DELETE FROM pending_alerts WHERE family_id = $1 AND coalesce_key = $2",
                    family_id,
                    alert["coalesce_key"],
                )
            await connection.execute(
                """
                INSERT INTO pending_alerts (family_id, chat_id, text, coalesce_key, expires_at, publication, parse_mode)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                family_id,
                chat_id,
                alert["text"],
                alert.get("coalesce_key"),
                alert.get("expires_at"),
                alert.get("publication"),
                alert.get("parse_mode"),
            )

    async def observe_family(self, family_id: str, state: dict, name: str, alerts: list[dict]) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            chat_id = await connection.fetchval(
                "UPDATE families SET state = $2, name = $3 WHERE family_id = $1 RETURNING notify_chat",
                family_id,
                state,
                name,
            )
            await self._enqueue_alerts(connection, family_id, chat_id, alerts)

    async def enqueue_alerts(self, family_id: str, alerts: list[dict]) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            chat_id = await connection.fetchval(
                "SELECT notify_chat FROM families WHERE family_id = $1 FOR UPDATE", family_id
            )
            await self._enqueue_alerts(connection, family_id, chat_id, alerts)

    async def pending_alerts(self, family_id: str, now: float) -> list[asyncpg.Record]:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM pending_alerts WHERE family_id = $1 AND expires_at <= $2", family_id, now
            )
            return await connection.fetch(
                """
                SELECT a.*, f.alert_link_version AS link_version FROM pending_alerts a JOIN families f USING (family_id)
                WHERE a.family_id = $1 AND a.chat_id = f.notify_chat AND f.alert_retry_at <= $2 ORDER BY a.id
                """,
                family_id,
                now,
            )

    async def delete_alert(self, alert_id: int) -> None:
        await self.pool.execute("DELETE FROM pending_alerts WHERE id = $1", alert_id)

    async def defer_alerts(
        self, family_id: str, chat_id: int, retry_at: float, *, link_version: int | None = None
    ) -> None:
        await self.pool.execute(
            """
            UPDATE families SET alert_retry_at = GREATEST(alert_retry_at, $3) WHERE family_id = $1
                AND notify_chat IS NOT NULL AND (notify_chat = $2 OR alert_link_version = $4::bigint)
            """,
            family_id,
            chat_id,
            retry_at,
            link_version,
        )

    async def alert_families(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM families f WHERE EXISTS (SELECT 1 FROM pending_alerts a WHERE a.family_id = f.family_id)"
        )

    async def alert_health(self, now: float) -> dict[str, int]:
        return dict(
            await self.pool.fetchrow(
                """
            SELECT count(*) AS pending_retry,
                   count(*) FILTER (WHERE f.alert_retry_at > $1) AS deferred
            FROM pending_alerts a JOIN families f USING (family_id)
            WHERE a.chat_id = f.notify_chat AND (a.expires_at IS NULL OR a.expires_at > $1)
            """,
                now,
            )
        )

    async def families_with_accounts(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM families f WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.family_id = f.family_id)"
        )

    async def ensure_family(self, family_id: str, name: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO families (family_id, name) VALUES ($1, $2) ON CONFLICT (family_id) DO UPDATE SET name = $2
        """,
            family_id,
            name,
        )

    async def claim_initial_owner(
        self, family_id: str, telegram_id: int, *, expected_generation: int | None = None
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            family = await connection.fetchrow(
                "SELECT owner_telegram_id FROM families WHERE family_id = $1 FOR UPDATE", family_id
            )
            if family is None or family["owner_telegram_id"] is not None:
                return
            status = await connection.fetchval(
                "SELECT status FROM accounts WHERE telegram_id = $1 AND family_id = $2 "
                "AND ($3::bigint IS NULL OR (token_generation = $3 AND refresh_token IS NOT NULL)) FOR UPDATE",
                telegram_id,
                family_id,
                expected_generation,
            )
            if status is None:
                if expected_generation is not None:
                    return
                raise RuntimeError("Initial owner account is no longer registered in this family")
            if status == "blocked":
                return
            await connection.execute(
                "UPDATE families SET owner_telegram_id = $2 WHERE family_id = $1",
                family_id,
                telegram_id,
            )
            if status != "approved":
                await connection.execute("UPDATE accounts SET status = 'approved' WHERE telegram_id = $1", telegram_id)

    async def handover_owner(self, family_id: str, owner_id: int, target_id: int | None) -> int | None:
        async with self.pool.acquire() as connection, connection.transaction():
            family = await connection.fetchrow(
                "SELECT owner_telegram_id FROM families WHERE family_id = $1 FOR UPDATE", family_id
            )
            if family is None or family["owner_telegram_id"] != owner_id:
                return None
            target = await connection.fetchval(
                "SELECT telegram_id FROM accounts WHERE telegram_id = $1 AND family_id = $2 "
                "AND status = 'approved' AND refresh_token IS NOT NULL FOR UPDATE",
                target_id,
                family_id,
            )
            await connection.execute(
                "UPDATE families SET owner_telegram_id = $2 WHERE family_id = $1", family_id, target
            )
            return target

    async def create_owner_transfer(
        self, family_id: str, owner_id: int, target_id: int, members: list[str]
    ) -> asyncpg.Record | None:
        if owner_id == target_id:
            return None
        async with self.pool.acquire() as connection, connection.transaction():
            family = await connection.fetchrow(
                "SELECT owner_telegram_id FROM families WHERE family_id = $1 FOR UPDATE", family_id
            )
            if family is None or family["owner_telegram_id"] != owner_id:
                return None
            accounts = await connection.fetch(
                "SELECT * FROM accounts WHERE telegram_id = ANY($1::bigint[]) ORDER BY telegram_id FOR UPDATE",
                [owner_id, target_id],
            )
            if len(accounts) != 2 or any(
                account["family_id"] != family_id
                or account["status"] != "approved"
                or account["refresh_token"] is None
                or account["steam_id"] not in members
                for account in accounts
            ):
                return None
            owner = next(account for account in accounts if account["telegram_id"] == owner_id)
            target = next(account for account in accounts if account["telegram_id"] == target_id)
            # shortcut: privacy changes also invalidate invites; use an approval version if this becomes disruptive.
            await connection.execute("DELETE FROM owner_transfers WHERE family_id = $1", family_id)
            return await connection.fetchrow(
                """
                INSERT INTO owner_transfers
                    (family_id, owner_id, target_id, owner_steam_id, target_steam_id,
                     owner_generation, target_generation, owner_publication_version, target_publication_version)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *
                """,
                family_id,
                owner_id,
                target_id,
                owner["steam_id"],
                target["steam_id"],
                owner["token_generation"],
                target["token_generation"],
                owner["publication_version"],
                target["publication_version"],
            )

    async def _active_owner_transfer(self, connection, family_id: str) -> asyncpg.Record | None:
        return await connection.fetchrow(
            """
            SELECT t.* FROM owner_transfers t JOIN families f USING (family_id)
            JOIN accounts owner ON owner.telegram_id = t.owner_id
            JOIN accounts target ON target.telegram_id = t.target_id
            WHERE t.family_id = $1 AND t.expires_at > clock_timestamp() AND f.owner_telegram_id = t.owner_id
                AND owner.family_id = t.family_id AND target.family_id = t.family_id
                AND owner.steam_id = t.owner_steam_id AND target.steam_id = t.target_steam_id
                AND owner.token_generation = t.owner_generation AND target.token_generation = t.target_generation
                AND owner.publication_version = t.owner_publication_version
                AND target.publication_version = t.target_publication_version
                AND owner.status = 'approved' AND target.status = 'approved'
                AND owner.refresh_token IS NOT NULL AND target.refresh_token IS NOT NULL
            """,
            family_id,
        )

    async def owner_transfer(self, family_id: str) -> asyncpg.Record | None:
        return await self._active_owner_transfer(self.pool, family_id)

    async def cancel_owner_transfer(self, family_id: str, owner_id: int, transfer_id: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as connection, connection.transaction():
            owner = await connection.fetchval(
                "SELECT owner_telegram_id FROM families WHERE family_id = $1 FOR UPDATE", family_id
            )
            if owner != owner_id:
                return None
            account = await connection.fetchval(
                "SELECT telegram_id FROM accounts WHERE telegram_id = $1 AND family_id = $2 "
                "AND status = 'approved' FOR UPDATE",
                owner_id,
                family_id,
            )
            if account is None:
                return None
            return await connection.fetchrow(
                "DELETE FROM owner_transfers WHERE family_id = $1 AND owner_id = $2 AND id = $3 RETURNING *",
                family_id,
                owner_id,
                transfer_id,
            )

    async def accept_owner_transfer(
        self, family_id: str, target_id: int, transfer_id: str, members: list[str]
    ) -> asyncpg.Record | None:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval("SELECT family_id FROM families WHERE family_id = $1 FOR UPDATE", family_id)
            invitation = await connection.fetchrow(
                "SELECT * FROM owner_transfers WHERE family_id = $1 AND target_id = $2 AND id = $3",
                family_id,
                target_id,
                transfer_id,
            )
            if invitation is None:
                return None
            # Lock accounts before consuming the invitation, matching logout's lock order.
            await connection.fetch(
                "SELECT telegram_id FROM accounts WHERE telegram_id = ANY($1::bigint[]) "
                "ORDER BY telegram_id FOR UPDATE",
                [invitation["owner_id"], target_id],
            )
            current = await self._active_owner_transfer(connection, family_id)
            if (
                current is None
                or current["id"] != transfer_id
                or any(current[field] not in members for field in ("owner_steam_id", "target_steam_id"))
            ):
                return None
            changed = await connection.fetchval(
                "UPDATE families SET owner_telegram_id = $3 WHERE family_id = $1 AND owner_telegram_id = $2 "
                "AND EXISTS (SELECT 1 FROM owner_transfers WHERE family_id = $1 AND id = $4 "
                "AND expires_at > clock_timestamp()) RETURNING owner_telegram_id",
                family_id,
                current["owner_id"],
                target_id,
                transfer_id,
            )
            return current if changed is not None else None

    async def revoke_membership(
        self,
        telegram_id: int,
        family_id: str,
        *,
        steam_id: str | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval("SELECT family_id FROM families WHERE family_id = $1 FOR UPDATE", family_id)
            account = await connection.fetchval(
                """
                SELECT telegram_id FROM accounts WHERE telegram_id = $1 AND family_id = $2
                    AND ($3::text IS NULL OR steam_id = $3)
                    AND ($4::bigint IS NULL OR token_generation = $4) FOR UPDATE
                """,
                telegram_id,
                family_id,
                steam_id,
                expected_generation,
            )
            if account is None:
                return False
            await connection.execute(
                """
                UPDATE accounts SET status = CASE WHEN status = 'approved' THEN 'pending' ELSE status END,
                    refresh_token = NULL, token_renewed_at = 0, publish_presence = FALSE, publish_telegram = FALSE,
                    token_generation = nextval('steamily_token_generation'),
                    publication_version = gen_random_uuid()::text
                WHERE telegram_id = $1 AND (status = 'approved' OR refresh_token IS NOT NULL OR token_renewed_at <> 0
                    OR publish_presence OR publish_telegram)
                """,
                telegram_id,
            )
            await connection.execute(
                "UPDATE families SET owner_telegram_id = NULL WHERE family_id = $1 AND owner_telegram_id = $2",
                family_id,
                telegram_id,
            )
            return True

    async def reconcile_members(self, family_id: str, members: list[str]) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            family = await connection.fetchrow(
                "SELECT owner_telegram_id FROM families WHERE family_id = $1 FOR UPDATE", family_id
            )
            if family is None:
                return
            await connection.execute(
                """
                UPDATE accounts SET status = CASE WHEN status = 'approved' THEN 'pending' ELSE status END,
                    refresh_token = NULL, token_renewed_at = 0, publish_presence = FALSE, publish_telegram = FALSE,
                    token_generation = nextval('steamily_token_generation'),
                    publication_version = gen_random_uuid()::text
                WHERE family_id = $1 AND NOT (steam_id = ANY($2::text[]))
                    AND (status = 'approved' OR refresh_token IS NOT NULL OR token_renewed_at <> 0
                        OR publish_presence OR publish_telegram)
                """,
                family_id,
                members,
            )
            accounts = await connection.fetch(
                """
                SELECT telegram_id, status FROM accounts
                WHERE family_id = $1 AND steam_id = ANY($2::text[]) AND status = 'approved'
                ORDER BY created_at, telegram_id FOR UPDATE
                """,
                family_id,
                members,
            )
            owner = next(
                (
                    account
                    for account in accounts
                    if account["telegram_id"] == family["owner_telegram_id"] and account["status"] == "approved"
                ),
                accounts[0] if accounts else None,
            )
            owner_id = owner["telegram_id"] if owner is not None else None
            if owner_id != family["owner_telegram_id"]:
                await connection.execute(
                    "UPDATE families SET owner_telegram_id = $2 WHERE family_id = $1", family_id, owner_id
                )

    async def update_family(self, family_id: str, **fields) -> None:
        allowed = {"name", "notify_chat", "state", "language", "owner_telegram_id", "reader_telegram_id", "alerts"}
        assert fields and set(fields) <= allowed, fields
        columns = ", ".join(f"{name} = ${i}" for i, name in enumerate(fields, start=2))
        query = f"UPDATE families SET {columns} WHERE family_id = $1"
        if "notify_chat" not in fields:
            await self.pool.execute(query, family_id, *fields.values())
            return
        async with self.pool.acquire() as connection, connection.transaction():
            family = await connection.fetchrow(
                "SELECT notify_chat FROM families WHERE family_id = $1 FOR UPDATE", family_id
            )
            if family and family["notify_chat"] != fields["notify_chat"]:
                await connection.execute("DELETE FROM pending_alerts WHERE family_id = $1", family_id)
                await connection.execute(
                    "UPDATE families SET alert_retry_at = 0, alert_link_version = alert_link_version + 1 "
                    "WHERE family_id = $1",
                    family_id,
                )
            await connection.execute(query, family_id, *fields.values())

    # Store cache, one JSON document per country
    async def store_cache(self, country: str) -> dict:
        return await self.pool.fetchval("SELECT data FROM store_cache WHERE country = $1", country) or {}

    async def save_store_cache(self, country: str, data: dict) -> None:
        await self.pool.execute(
            """
            INSERT INTO store_cache (country, data) VALUES ($1, $2)
            ON CONFLICT (country) DO UPDATE SET data = $2
        """,
            country,
            data,
        )


class PgStorage(BaseStorage):
    """aiogram FSM storage in Postgres, so open dialogs survive restarts."""

    def __init__(self, db: Db):
        self.pool = db.pool

    @staticmethod
    def _key(key: StorageKey) -> str:
        return ":".join(
            str(part)
            for part in (key.bot_id, key.chat_id, key.user_id, key.thread_id, key.business_connection_id, key.destiny)
        )

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        state = state.state if isinstance(state, State) else state
        await self.pool.execute(
            """
            INSERT INTO fsm (key, state) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET state = $2
        """,
            self._key(key),
            state,
        )

    async def get_state(self, key: StorageKey) -> str | None:
        return await self.pool.fetchval("SELECT state FROM fsm WHERE key = $1", self._key(key))

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self.pool.execute(
            """
            INSERT INTO fsm (key, data) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET data = $2
        """,
            self._key(key),
            dict(data),
        )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return await self.pool.fetchval("SELECT data FROM fsm WHERE key = $1", self._key(key)) or {}

    async def close(self) -> None:
        await self.pool.close()
