-- Consent starts off for existing and new accounts; queued disclosures stay structured until delivery.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS publish_presence BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS publish_telegram BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS publication_version TEXT NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE pending_alerts ADD COLUMN IF NOT EXISTS publication JSONB;
