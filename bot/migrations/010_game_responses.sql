ALTER TABLE accounts ADD COLUMN IF NOT EXISTS game_response_version TEXT NOT NULL DEFAULT gen_random_uuid()::text;

CREATE TABLE IF NOT EXISTS game_responses (
    family_id TEXT NOT NULL REFERENCES families ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL REFERENCES accounts ON DELETE CASCADE,
    appid BIGINT NOT NULL CHECK (appid > 0 AND appid < 4294967296),
    response TEXT NOT NULL CHECK (response IN ('recommend', 'together', 'not_interested')),
    named BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (family_id, telegram_id, appid),
    CHECK (response = 'recommend' OR NOT named)
);

-- Clear responses in the same transaction as every existing account reset path.
CREATE OR REPLACE FUNCTION clear_game_responses() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM game_responses WHERE telegram_id = OLD.telegram_id;
    NEW.game_response_version := gen_random_uuid()::text;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS reset_game_responses ON accounts;
CREATE TRIGGER reset_game_responses BEFORE UPDATE ON accounts
FOR EACH ROW WHEN (
    OLD.family_id IS DISTINCT FROM NEW.family_id
    OR OLD.steam_id IS DISTINCT FROM NEW.steam_id
    OR OLD.created_at IS DISTINCT FROM NEW.created_at
    OR NEW.status <> 'approved'
    OR NEW.refresh_token IS NULL
) EXECUTE FUNCTION clear_game_responses();
