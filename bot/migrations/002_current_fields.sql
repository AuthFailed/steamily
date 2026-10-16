-- Existing unversioned installations may already have any subset of these fields.
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
