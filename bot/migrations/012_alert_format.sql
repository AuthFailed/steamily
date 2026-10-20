-- Existing queued text stays literal; new alerts may opt in to formatted delivery.
ALTER TABLE pending_alerts ADD COLUMN IF NOT EXISTS parse_mode TEXT;
