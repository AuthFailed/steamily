-- Generations are never reused after logout; rotated credentials retain their generation.
CREATE SEQUENCE IF NOT EXISTS steamily_token_generation;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS token_generation BIGINT NOT NULL DEFAULT 0;
-- Scheduled reads retain one selected account, independently of bot ownership.
ALTER TABLE families ADD COLUMN IF NOT EXISTS reader_telegram_id BIGINT;
