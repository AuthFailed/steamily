-- IF NOT EXISTS adopts databases created before versioned migrations.
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
CREATE TABLE IF NOT EXISTS fsm (
    key   TEXT PRIMARY KEY,
    state TEXT,
    data  JSONB NOT NULL DEFAULT '{}'
);
