-- Minimal reusable core for solution-oriented AI applications.
-- Existing policy/chat UX remains a consumer of this core during migration.
BEGIN;

CREATE TABLE IF NOT EXISTS source_snapshot (
    snapshot_id BIGSERIAL PRIMARY KEY,
    source_name VARCHAR(100) NOT NULL,
    source_locator TEXT NOT NULL,
    source_fingerprint VARCHAR(64) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    mapping_version VARCHAR(50) NOT NULL,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_name, source_fingerprint, payload_hash)
);

CREATE TABLE IF NOT EXISTS ingestion_attempt (
    attempt_id BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES source_snapshot(snapshot_id) ON DELETE CASCADE,
    stage VARCHAR(50) NOT NULL,
    status VARCHAR(30) NOT NULL,
    error_message TEXT,
    counters_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED'))
);
CREATE INDEX IF NOT EXISTS ingestion_attempt_snapshot_stage_idx
    ON ingestion_attempt (snapshot_id, stage, started_at DESC);

CREATE TABLE IF NOT EXISTS domain_entity (
    entity_id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(100) NOT NULL,
    canonical_key VARCHAR(255) NOT NULL,
    display_name TEXT NOT NULL,
    attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_type, canonical_key)
);

CREATE TABLE IF NOT EXISTS domain_relation (
    relation_id BIGSERIAL PRIMARY KEY,
    subject_entity_id BIGINT NOT NULL REFERENCES domain_entity(entity_id) ON DELETE CASCADE,
    predicate VARCHAR(100) NOT NULL,
    object_entity_id BIGINT NOT NULL REFERENCES domain_entity(entity_id) ON DELETE CASCADE,
    attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence NUMERIC(5,4),
    review_status VARCHAR(30) NOT NULL DEFAULT 'UNREVIEWED',
    UNIQUE (subject_entity_id, predicate, object_entity_id)
);

CREATE TABLE IF NOT EXISTS evidence_span (
    evidence_span_id BIGSERIAL PRIMARY KEY,
    document_id BIGINT REFERENCES policy_document(document_id) ON DELETE CASCADE,
    chunk_id BIGINT REFERENCES policy_document_chunk(chunk_id) ON DELETE CASCADE,
    snapshot_id BIGINT REFERENCES source_snapshot(snapshot_id) ON DELETE SET NULL,
    char_start INTEGER,
    char_end INTEGER,
    quoted_text TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS relation_evidence (
    relation_id BIGINT NOT NULL REFERENCES domain_relation(relation_id) ON DELETE CASCADE,
    evidence_span_id BIGINT NOT NULL REFERENCES evidence_span(evidence_span_id) ON DELETE CASCADE,
    PRIMARY KEY (relation_id, evidence_span_id)
);

CREATE TABLE IF NOT EXISTS decision_run (
    decision_run_id BIGSERIAL PRIMARY KEY,
    decision_type VARCHAR(50) NOT NULL,
    subject_ref VARCHAR(255) NOT NULL,
    input_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(30) NOT NULL,
    model_or_rule_version VARCHAR(100),
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS decision_claim (
    decision_claim_id BIGSERIAL PRIMARY KEY,
    decision_run_id BIGINT NOT NULL REFERENCES decision_run(decision_run_id) ON DELETE CASCADE,
    claim_type VARCHAR(100) NOT NULL,
    claim_text TEXT NOT NULL,
    confidence NUMERIC(5,4),
    review_status VARCHAR(30) NOT NULL DEFAULT 'UNREVIEWED'
);
CREATE TABLE IF NOT EXISTS claim_evidence (
    decision_claim_id BIGINT NOT NULL REFERENCES decision_claim(decision_claim_id) ON DELETE CASCADE,
    evidence_span_id BIGINT NOT NULL REFERENCES evidence_span(evidence_span_id) ON DELETE CASCADE,
    PRIMARY KEY (decision_claim_id, evidence_span_id)
);

COMMIT;
