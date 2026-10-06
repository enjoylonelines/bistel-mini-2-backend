-- #270: 정책 문서 재적재 시 원본 근거와 대화 evidence를 보존한다.
--
-- POLICY_REFERENCE 문서는 source_fingerprint 단위의 append-only revision으로 관리한다.
-- 이전 revision은 검색/embedding 대상에서만 제외하며 삭제하지 않는다.

BEGIN;

ALTER TABLE policy_document
    ADD COLUMN IF NOT EXISTS source_fingerprint VARCHAR(64),
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS ingest_status VARCHAR(30) NOT NULL DEFAULT 'PENDING_TEXT',
    ADD COLUMN IF NOT EXISTS ingest_error TEXT,
    ADD COLUMN IF NOT EXISTS embedded_metadata_version VARCHAR(30),
    ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMP;

UPDATE policy_document d
SET source_fingerprint = md5(
        concat_ws('|', d.policy_id::text, d.source_type, d.source_url, d.source_title)
    )
WHERE d.source_fingerprint IS NULL;

UPDATE policy_document d
SET content_hash = md5(d.raw_text)
WHERE d.content_hash IS NULL
  AND d.raw_text IS NOT NULL
  AND btrim(d.raw_text) <> '';

UPDATE policy_document d
SET ingest_status = CASE
    WHEN EXISTS (
        SELECT 1
        FROM policy_document_chunk c
        WHERE c.document_id = d.document_id
    ) THEN 'CHUNK_READY'
    WHEN d.raw_text IS NOT NULL AND btrim(d.raw_text) <> '' THEN 'TEXT_READY'
    ELSE 'PENDING_TEXT'
END
WHERE d.ingest_status = 'PENDING_TEXT';

WITH duplicate_current_documents AS (
    SELECT
        document_id,
        row_number() OVER (
            PARTITION BY policy_id, source_fingerprint
            ORDER BY updated_at DESC, document_id DESC
        ) AS revision_rank
    FROM policy_document
    WHERE is_current = TRUE
      AND source_fingerprint IS NOT NULL
)
UPDATE policy_document d
SET is_current = FALSE,
    superseded_at = CURRENT_TIMESTAMP
FROM duplicate_current_documents duplicate
WHERE d.document_id = duplicate.document_id
  AND duplicate.revision_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS policy_document_current_source_fingerprint_uq
    ON policy_document(policy_id, source_fingerprint)
    WHERE is_current;

CREATE INDEX IF NOT EXISTS policy_document_current_ingest_idx
    ON policy_document(is_current, ingest_status, source_type);

COMMIT;
