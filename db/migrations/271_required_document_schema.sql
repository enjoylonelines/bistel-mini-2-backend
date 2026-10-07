-- Required-document records are derived from policy detail payloads during
-- policy import.  Keep the schema explicit so a fresh database can run the
-- import pipeline without relying on an out-of-band table creation step.
CREATE TABLE IF NOT EXISTS required_document (
    required_document_id BIGINT PRIMARY KEY,
    policy_id BIGINT NOT NULL REFERENCES policy(policy_id),
    document_name VARCHAR(255) NOT NULL,
    required_type VARCHAR(50),
    issue_place VARCHAR(255),
    description TEXT
);

CREATE INDEX IF NOT EXISTS required_document_policy_id_idx
    ON required_document (policy_id);

-- The import path upserts normalized tags by this natural key.  The index is
-- also the conflict target required by `ON CONFLICT (policy_id, tag_name)`.
CREATE UNIQUE INDEX IF NOT EXISTS policy_tag_policy_id_tag_name_uq
    ON policy_tag (policy_id, tag_name);
