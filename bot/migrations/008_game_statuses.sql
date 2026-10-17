CREATE TABLE IF NOT EXISTS game_statuses (
    telegram_id BIGINT NOT NULL REFERENCES accounts (telegram_id) ON DELETE CASCADE,
    appid       BIGINT NOT NULL CHECK (appid > 0 AND appid <= 4294967295),
    name        TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    status      TEXT NOT NULL CHECK (status IN ('backlog', 'playing', 'completed')),
    PRIMARY KEY (telegram_id, appid)
);
