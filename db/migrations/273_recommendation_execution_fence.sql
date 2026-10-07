-- Durable ownership fence for recommendation execution.
-- This does not implement a queue or a global concurrency limit.
BEGIN;

ALTER TABLE recommendation_request
    ADD COLUMN IF NOT EXISTS execution_token VARCHAR(36),
    ADD COLUMN IF NOT EXISTS execution_claimed_at TIMESTAMP;

COMMIT;
