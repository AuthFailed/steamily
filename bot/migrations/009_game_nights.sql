CREATE TABLE IF NOT EXISTS game_nights (
    id BIGSERIAL PRIMARY KEY,
    family_id TEXT NOT NULL REFERENCES families ON DELETE CASCADE,
    creator_telegram_id BIGINT NOT NULL REFERENCES accounts (telegram_id) ON DELETE CASCADE,
    creator_steam_id TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    link_version BIGINT NOT NULL,
    message_id BIGINT,
    starts_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    appids BIGINT[] NOT NULL CHECK (cardinality(appids) BETWEEN 2 AND 5),
    chosen_appid BIGINT,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS game_nights_family ON game_nights (family_id);
CREATE TABLE IF NOT EXISTS game_night_participants (
    event_id BIGINT NOT NULL REFERENCES game_nights ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL REFERENCES accounts ON DELETE CASCADE,
    steam_id TEXT NOT NULL,
    vote BIGINT,
    rsvp TEXT CHECK (rsvp IN ('going', 'maybe', 'cannot')),
    PRIMARY KEY (event_id, telegram_id)
);
