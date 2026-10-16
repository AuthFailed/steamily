-- Schema shipped in commit 48efc8f, before renewal timestamps and unique Steam links.
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
