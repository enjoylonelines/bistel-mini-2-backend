-- #275: 수동 fixture/import ID 뒤로 serial sequence를 정렬한다.
--
-- 문서와 청크는 과거에 결정적 ID로 적재된 적이 있다. 이후 append-only
-- document revision을 DB sequence로 생성할 때 기존 PK를 재사용하지 않도록
-- 현재 최대 ID 다음 값을 사용하게 한다.

BEGIN;

SELECT setval(
    pg_get_serial_sequence('policy_document', 'document_id'),
    COALESCE((SELECT MAX(document_id) FROM policy_document), 1),
    EXISTS (SELECT 1 FROM policy_document)
);

SELECT setval(
    pg_get_serial_sequence('policy_document_chunk', 'chunk_id'),
    COALESCE((SELECT MAX(chunk_id) FROM policy_document_chunk), 1),
    EXISTS (SELECT 1 FROM policy_document_chunk)
);

COMMIT;
