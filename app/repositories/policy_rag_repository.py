from typing import Any


POLICY_RAG_METADATA_VERSION = "2026-06-26.1"


class PolicyRagRepository:
    @staticmethod
    async def find_embedding_targets(
        conn,
        limit: int,
        source_type: str | None = None,
    ) -> list[dict[str, Any]]:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                    SELECT to_regclass('public.langchain_pg_collection') IS NOT NULL
                       AND to_regclass('public.langchain_pg_embedding') IS NOT NULL
                """
            )
            vector_tables_exist = (await cur.fetchone())[0]

            if not vector_tables_exist:
                return await PolicyRagRepository._find_all_embedding_targets(
                    cur=cur,
                    limit=limit,
                    source_type=source_type,
                )

            await cur.execute(
                """
                    SELECT
                        c.chunk_id,
                        c.document_id,
                        c.chunk_index,
                        c.chunk_text,
                        c.metadata_json,
                        d.source_title,
                        d.source_url,
                        d.source_type,
                        p.policy_id,
                        p.policy_code,
                        p.policy_name,
                        p.main_category,
                        p.sub_category,
                        p.provider_name,
                        p.provider_type,
                        p.region_scope,
                        p.region_code,
                        p.benefit_type,
                        p.application_status,
                        p.application_start_date,
                        p.application_end_date,
                        md5(c.chunk_text) AS chunk_hash
                    FROM policy_document_chunk c
                    JOIN policy_document d ON d.document_id = c.document_id
                    JOIN policy p ON p.policy_id = d.policy_id
                    LEFT JOIN langchain_pg_collection collection
                        ON collection.name = 'policy_documents'
                    LEFT JOIN langchain_pg_embedding embedding
                        ON embedding.collection_id = collection.uuid
                       AND embedding.id = c.chunk_id::text
                    WHERE c.chunk_text IS NOT NULL
                      AND btrim(c.chunk_text) <> ''
                      AND d.is_current = TRUE
                      AND (%s::varchar IS NULL OR d.source_type = %s::varchar)
                      AND (
                          embedding.id IS NULL
                          OR embedding.cmetadata->>'chunk_hash' IS DISTINCT FROM md5(c.chunk_text)
                          OR embedding.cmetadata->>'metadata_version' IS DISTINCT FROM %s
                      )
                    ORDER BY c.chunk_id
                    LIMIT %s
                """,
                (source_type, source_type, POLICY_RAG_METADATA_VERSION, limit),
            )
            rows = await cur.fetchall()

        columns = [
            "chunk_id",
            "document_id",
            "chunk_index",
            "chunk_text",
            "metadata_json",
            "source_title",
            "source_url",
            "source_type",
            "policy_id",
            "policy_code",
            "policy_name",
            "main_category",
            "sub_category",
            "provider_name",
            "provider_type",
            "region_scope",
            "region_code",
            "benefit_type",
            "application_status",
            "application_start_date",
            "application_end_date",
            "chunk_hash",
        ]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    @staticmethod
    async def _find_all_embedding_targets(
        cur,
        limit: int,
        source_type: str | None = None,
    ) -> list[dict[str, Any]]:
        await cur.execute(
            """
                SELECT
                    c.chunk_id,
                    c.document_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.metadata_json,
                    d.source_title,
                    d.source_url,
                    d.source_type,
                    p.policy_id,
                    p.policy_code,
                    p.policy_name,
                    p.main_category,
                    p.sub_category,
                    p.provider_name,
                    p.provider_type,
                    p.region_scope,
                    p.region_code,
                    p.benefit_type,
                    p.application_status,
                    p.application_start_date,
                    p.application_end_date,
                    md5(c.chunk_text) AS chunk_hash
                FROM policy_document_chunk c
                JOIN policy_document d ON d.document_id = c.document_id
                JOIN policy p ON p.policy_id = d.policy_id
                WHERE c.chunk_text IS NOT NULL
                  AND btrim(c.chunk_text) <> ''
                  AND d.is_current = TRUE
                  AND (%s::varchar IS NULL OR d.source_type = %s::varchar)
                ORDER BY c.chunk_id
                LIMIT %s
            """,
            (source_type, source_type, limit),
        )
        rows = await cur.fetchall()
        columns = [
            "chunk_id",
            "document_id",
            "chunk_index",
            "chunk_text",
            "metadata_json",
            "source_title",
            "source_url",
            "source_type",
            "policy_id",
            "policy_code",
            "policy_name",
            "main_category",
            "sub_category",
            "provider_name",
            "provider_type",
            "region_scope",
            "region_code",
            "benefit_type",
            "application_status",
            "application_start_date",
            "application_end_date",
            "chunk_hash",
        ]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    @staticmethod
    async def mark_documents_embedded(
        conn,
        document_ids: list[int],
    ) -> None:
        if not document_ids:
            return
        async with conn.cursor() as cur:
            await cur.execute(
                """
                    UPDATE policy_document
                    SET ingest_status = 'EMBEDDED',
                        embedded_metadata_version = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE document_id = ANY(%s)
                """,
                (POLICY_RAG_METADATA_VERSION, document_ids),
            )
