-- Adopt the durable queue from unversioned installations without resetting its contents.
ALTER TABLE families ADD COLUMN IF NOT EXISTS alert_retry_at DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE families ADD COLUMN IF NOT EXISTS alert_link_version BIGINT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS pending_alerts (
    id           BIGSERIAL PRIMARY KEY,
    family_id    TEXT NOT NULL REFERENCES families ON DELETE CASCADE,
    chat_id      BIGINT NOT NULL,
    text         TEXT NOT NULL,
    coalesce_key TEXT,
    expires_at   DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS pending_alerts_family ON pending_alerts (family_id, id);
