-- Append-only observability for the bounded recommendation execution slice.
-- It records process-local execution facts; it is not a global queue ledger.
BEGIN;

CREATE TABLE IF NOT EXISTS recommendation_execution_event (
    event_id BIGSERIAL PRIMARY KEY,
    request_id BIGINT NOT NULL
        REFERENCES recommendation_request(request_id) ON DELETE CASCADE,
    execution_token VARCHAR(36),
    event_type VARCHAR(50) NOT NULL,
    stage VARCHAR(50) NOT NULL,
    outcome VARCHAR(50),
    error_type VARCHAR(100),
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS recommendation_execution_event_request_idx
    ON recommendation_execution_event (request_id, event_id);

COMMIT;
