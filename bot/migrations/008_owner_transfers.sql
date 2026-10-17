CREATE TABLE IF NOT EXISTS owner_transfers (
    id                        TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    family_id                 TEXT NOT NULL UNIQUE REFERENCES families ON DELETE CASCADE,
    owner_id                  BIGINT NOT NULL,
    target_id                 BIGINT NOT NULL CHECK (target_id <> owner_id),
    owner_steam_id            TEXT NOT NULL,
    target_steam_id           TEXT NOT NULL,
    owner_generation          BIGINT NOT NULL,
    target_generation         BIGINT NOT NULL,
    owner_publication_version TEXT NOT NULL,
    target_publication_version TEXT NOT NULL,
    expires_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp() + interval '24 hours'
);

-- Every owner writer, including membership recovery, invalidates old invitations.
CREATE OR REPLACE FUNCTION invalidate_owner_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM owner_transfers WHERE family_id = NEW.family_id;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS invalidate_owner_transfer ON families;
CREATE TRIGGER invalidate_owner_transfer BEFORE UPDATE OF owner_telegram_id ON families
FOR EACH ROW WHEN (OLD.owner_telegram_id IS DISTINCT FROM NEW.owner_telegram_id)
EXECUTE FUNCTION invalidate_owner_transfer();
