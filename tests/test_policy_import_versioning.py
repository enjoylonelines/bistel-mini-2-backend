import asyncio

from langchain_core.documents import Document

from app.repositories.policy_document_repository import PolicyDocumentRepository
from app.repositories.policy_import_repository import PolicyImportRepository
from app.repositories.policy_rag_repository import PolicyRagRepository


class _RecordingCursor:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.queries: list[str] = []
        self.params: list[object] = []
        self._rows = rows or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def execute(self, query: str, params: object = None) -> None:
        self.queries.append(query)
        self.params.append(params)

    async def fetchone(self):
        return self._rows.pop(0) if self._rows else (0,)

    async def fetchall(self):
        return []


class _RecordingConnection:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.cursor_instance = _RecordingCursor(rows)

    def cursor(self) -> _RecordingCursor:
        return self.cursor_instance


def test_reference_import_versions_documents_without_deleting_history() -> None:
    conn = _RecordingConnection(rows=[(3,)])

    count = asyncio.run(PolicyImportRepository.replace_policy_documents(conn))

    queries = "\n".join(conn.cursor_instance.queries)
    assert count == 3
    assert "UPDATE policy_document d\n                SET is_current = FALSE" in queries
    assert "DELETE FROM policy_document" not in queries
    assert "source_fingerprint" in queries
    assert "ON CONFLICT (policy_id, source_fingerprint) WHERE is_current" in queries


def test_chunk_replacement_marks_document_ready_for_embedding() -> None:
    conn = _RecordingConnection()

    count = asyncio.run(
        PolicyDocumentRepository.replace_document_chunks(
            conn,
            document_id=42,
            chunk_documents=[Document(page_content="근거", metadata={"section": "지원 대상"})],
        )
    )

    assert count == 1
    assert "SET ingest_status = 'CHUNK_READY'" in conn.cursor_instance.queries[-1]


def test_document_ingest_failure_is_persisted() -> None:
    conn = _RecordingConnection()

    asyncio.run(
        PolicyDocumentRepository.mark_document_ingest_failed(
            conn, document_id=42, error="download timed out"
        )
    )

    assert "SET ingest_status = 'FAILED'" in conn.cursor_instance.queries[-1]
    assert conn.cursor_instance.params[-1] == ("download timed out", 42)


def test_embedding_targets_exclude_superseded_documents() -> None:
    conn = _RecordingConnection(rows=[(True,)])

    asyncio.run(PolicyRagRepository.find_embedding_targets(conn, limit=10))

    assert "AND d.is_current = TRUE" in conn.cursor_instance.queries[-1]


def test_embedding_completion_marks_only_affected_documents() -> None:
    conn = _RecordingConnection()

    asyncio.run(PolicyRagRepository.mark_documents_embedded(conn, [101, 102]))

    assert "SET ingest_status = 'EMBEDDED'" in conn.cursor_instance.queries[-1]
    assert conn.cursor_instance.params[-1][1] == [101, 102]
