"""Postgres: accounts, families, store cache and aiogram FSM state."""

import json
from collections.abc import Mapping
from typing import Any

import asyncpg
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from cryptography.fernet import Fernet

SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    family_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    notify_chat   BIGINT,
    state         JSONB
);
CREATE TABLE IF NOT EXISTS accounts (
    telegram_id   BIGINT PRIMARY KEY,
    steam_id      TEXT NOT NULL,
    family_id     TEXT NOT NULL REFERENCES families ON DELETE CASCADE,
    refresh_token BYTEA,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'blocked')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS accounts_family ON accounts (family_id);
CREATE TABLE IF NOT EXISTS store_cache (
    country TEXT PRIMARY KEY,
    data    JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_users (
    telegram_id BIGINT PRIMARY KEY,
    language    TEXT NOT NULL
);
ALTER TABLE families ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS country TEXT;
-- Unix seconds of the last successful refresh-token renewal, preserved across restarts.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS token_renewed_at DOUBLE PRECISION NOT NULL DEFAULT 0;
-- The family owner is a bot role: it passes on when the owner logs out (see Families.leave).
ALTER TABLE families ADD COLUMN IF NOT EXISTS owner_telegram_id BIGINT;
ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS notify_requests BOOLEAN NOT NULL DEFAULT TRUE;
-- Which group alerts are on; missing kinds use library.ALERTS defaults.
ALTER TABLE families ADD COLUMN IF NOT EXISTS alerts JSONB;
-- The Telegram side of a registered member, for links next to their Steam profile.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS tg_username TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS tg_name TEXT;
CREATE TABLE IF NOT EXISTS fsm (
    key   TEXT PRIMARY KEY,
    state TEXT,
    data  JSONB NOT NULL DEFAULT '{}'
);
DO $$
BEGIN
    -- Keep the duplicate check and index creation atomic against account writes.
    LOCK TABLE accounts IN SHARE ROW EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM accounts GROUP BY steam_id HAVING count(*) > 1) THEN
        RAISE EXCEPTION 'Duplicate Steam account links prevent startup. See docs/steam-account-links.md for recovery.'
            USING HINT = 'SELECT steam_id, array_agg(telegram_id ORDER BY telegram_id) FROM accounts '
                         'GROUP BY steam_id HAVING count(*) > 1;';
    END IF;
    CREATE UNIQUE INDEX IF NOT EXISTS accounts_steam_id_key ON accounts (steam_id);
END $$;
"""


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
            await pool.execute(SCHEMA)
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
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("DELETE FROM accounts WHERE telegram_id = $1", telegram_id)
            await connection.execute("DELETE FROM telegram_users WHERE telegram_id = $1", telegram_id)

    async def set_language(self, telegram_id: int, language: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO telegram_users (telegram_id, language) VALUES ($1, $2)
            ON CONFLICT (telegram_id) DO UPDATE SET language = $2
        """,
            telegram_id,
            language,
        )

    # Accounts
    async def account(self, telegram_id: int) -> asyncpg.Record | None:
        """The account plus its family's owner_telegram_id."""
        return await self.pool.fetchrow(
            """
            SELECT a.*, f.owner_telegram_id FROM accounts a JOIN families f USING (family_id) WHERE telegram_id = $1
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
    ) -> None:
        await self.check_steam_link(telegram_id, steam_id)
        try:
            # Re-login keeps the approval status unless the Steam account changed.
            await self.pool.execute(
                """
            INSERT INTO accounts (telegram_id, steam_id, family_id, refresh_token, country, token_renewed_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (telegram_id) DO UPDATE SET family_id = $3, refresh_token = $4, country = $5,
                token_renewed_at = $6,
                status = CASE WHEN accounts.steam_id = $2 THEN accounts.status ELSE 'pending' END, steam_id = $2
            """,
                telegram_id,
                steam_id,
                family_id,
                self.fernet.encrypt(refresh_token.encode()),
                country,
                token_renewed_at,
            )
        except asyncpg.UniqueViolationError as error:
            if error.constraint_name != "accounts_steam_id_key":
                raise
            raise SteamAlreadyLinked("Steam account is already linked to another Telegram account.") from error

    async def set_refresh_token(
        self, telegram_id: int, refresh_token: str | None, token_renewed_at: float = 0.0
    ) -> None:
        token = self.fernet.encrypt(refresh_token.encode()) if refresh_token else None
        await self.pool.execute(
            "UPDATE accounts SET refresh_token = $2, token_renewed_at = $3 WHERE telegram_id = $1",
            telegram_id,
            token,
            token_renewed_at,
        )

    def refresh_token(self, account: asyncpg.Record) -> str | None:
        return self.fernet.decrypt(account["refresh_token"]).decode() if account["refresh_token"] else None

    async def set_telegram_profile(self, telegram_id: int, username: str | None, name: str) -> None:
        await self.pool.execute(
            "UPDATE accounts SET tg_username = $2, tg_name = $3 WHERE telegram_id = $1", telegram_id, username, name
        )

    async def set_country(self, telegram_id: int, country: str) -> None:
        await self.pool.execute("UPDATE accounts SET country = $2 WHERE telegram_id = $1", telegram_id, country)

    async def set_status(self, telegram_id: int, status: str, family_id: str | None = None) -> bool:
        changed = await self.pool.fetchval(
            """
            UPDATE accounts SET status = $2
            WHERE telegram_id = $1 AND ($3::TEXT IS NULL OR family_id = $3)
            RETURNING telegram_id
            """,
            telegram_id,
            status,
            family_id,
        )
        return changed is not None

    async def delete_account(self, telegram_id: int, family_id: str | None = None) -> bool:
        deleted = await self.pool.fetchval(
            """
            DELETE FROM accounts WHERE telegram_id = $1 AND ($2::TEXT IS NULL OR family_id = $2)
            RETURNING telegram_id
            """,
            telegram_id,
            family_id,
        )
        return deleted is not None

    # Families
    async def family(self, family_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM families WHERE family_id = $1", family_id)

    async def unlink_chat(self, chat_id: int) -> None:
        await self.pool.execute("UPDATE families SET notify_chat = NULL WHERE notify_chat = $1", chat_id)

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

    async def claim_initial_owner(self, family_id: str, telegram_id: int) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            owner = await connection.fetchval(
                """
                UPDATE families SET owner_telegram_id = $2
                WHERE family_id = $1 AND owner_telegram_id IS NULL
                RETURNING owner_telegram_id
                """,
                family_id,
                telegram_id,
            )
            if owner is not None:
                approved = await connection.fetchval(
                    """
                    UPDATE accounts SET status = 'approved' WHERE telegram_id = $1 AND family_id = $2
                    RETURNING telegram_id
                    """,
                    owner,
                    family_id,
                )
                if approved is None:
                    raise RuntimeError("Initial owner account is no longer registered in this family")

    async def update_family(self, family_id: str, **fields) -> None:
        allowed = {"name", "notify_chat", "state", "language", "owner_telegram_id", "alerts"}
        assert fields and set(fields) <= allowed, fields
        columns = ", ".join(f"{name} = ${i}" for i, name in enumerate(fields, start=2))
        await self.pool.execute(f"UPDATE families SET {columns} WHERE family_id = $1", family_id, *fields.values())

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
