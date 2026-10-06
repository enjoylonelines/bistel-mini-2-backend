import json
from typing import Any

from langchain_core.documents import Document


class PolicyDocumentRepository:
    @staticmethod
    async def find_policy_detail_sources(
        conn,
        limit: int,
        rebuild: bool = False,
    ) -> list[dict[str, Any]]:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                    SELECT
                        cp.condition_profile_id,
                        cp.policy_id,
                        p.policy_code,
                        COALESCE(cp.condition_json->>'policy_name', p.policy_name)
                            AS policy_name,
                        p.main_category,
                        p.sub_category,
                        p.provider_name,
                        p.benefit_type,
                        p.official_url,
                        cp.condition_json,
                        cp.target_summary,
                        cp.confidence,
                        cp.review_required,
                        cp.quality_flags,
                        cp.source_text,
                        cp.source_fields,
                        cp.updated_at AS condition_profile_updated_at,
                        d.easy_summary,
                        d.target_description,
                        d.benefit_description,
                        d.application_method,
                        d.application_period_text,
                        d.caution
                    FROM policy_condition_profile cp
                    JOIN policy p ON p.policy_id = cp.policy_id
                    LEFT JOIN policy_detail d ON d.policy_id = cp.policy_id
                    LEFT JOIN policy_document existing_document
                        ON existing_document.policy_id = cp.policy_id
                       AND existing_document.source_type = 'POLICY_DETAIL'
                       AND existing_document.is_current = TRUE
                    WHERE p.is_active = TRUE
                      AND (
                        %s::boolean = TRUE
                        OR existing_document.document_id IS NULL
                      )
                    ORDER BY cp.policy_id
                    LIMIT %s
                """,
                (rebuild, limit),
            )
            rows = await cur.fetchall()

        columns = [
            "condition_profile_id",
            "policy_id",
            "policy_code",
            "policy_name",
            "main_category",
            "sub_category",
            "provider_name",
            "benefit_type",
            "official_url",
            "condition_json",
            "target_summary",
            "confidence",
            "review_required",
            "quality_flags",
            "source_text",
            "source_fields",
            "condition_profile_updated_at",
            "easy_summary",
            "target_description",
            "benefit_description",
            "application_method",
            "application_period_text",
            "caution",
        ]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    @staticmethod
    async def find_policy_reference_download_targets(
        conn,
        limit: int,
        rebuild: bool = False,
    ) -> list[dict[str, Any]]:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                    WITH target_urls AS (
                        SELECT d.source_url, MIN(d.document_id) AS first_document_id
                        FROM policy_document d
                        WHERE d.source_type = 'POLICY_REFERENCE'
                          AND d.is_current = TRUE
                          AND d.source_url IS NOT NULL
                          AND btrim(d.source_url) <> ''
                          AND lower(d.source_title) LIKE '%%.pdf%%'
                          AND (
                            %s::boolean = TRUE
                            OR d.raw_text IS NULL
                            OR btrim(d.raw_text) = ''
                          )
                        GROUP BY d.source_url
                        ORDER BY MIN(d.document_id)
                        LIMIT %s
                    )
                    SELECT
                        d.document_id,
                        d.policy_id,
                        p.policy_code,
                        p.policy_name,
                        d.source_title,
                        d.source_url
                    FROM target_urls u
                    JOIN policy_document d ON d.source_url = u.source_url
                    JOIN policy p ON p.policy_id = d.policy_id
                    WHERE d.source_type = 'POLICY_REFERENCE'
                      AND d.is_current = TRUE
                      AND (
                        %s::boolean = TRUE
                        OR d.raw_text IS NULL
                        OR btrim(d.raw_text) = ''
                      )
                    ORDER BY u.first_document_id, d.document_id
                """,
                (rebuild, limit, rebuild),
            )
            rows = await cur.fetchall()

        columns = [
            "document_id",
            "policy_id",
            "policy_code",
            "policy_name",
            "source_title",
            "source_url",
        ]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    @staticmethod
    async def update_document_raw_text(
        conn,
        document_id: int,
        raw_text: str,
    ) -> None:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE policy_document
                SET raw_text = %s,
                    content_hash = md5(%s),
                    ingest_status = 'TEXT_READY',
                    ingest_error = NULL,
                    embedded_metadata_version = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE document_id = %s
                """,
                (raw_text, raw_text, document_id),
            )

    @staticmethod
    async def mark_document_ingest_failed(
        conn,
        document_id: int,
        error: str,
    ) -> None:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                    UPDATE policy_document
                    SET ingest_status = 'FAILED',
                        ingest_error = %s,
                        embedded_metadata_version = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE document_id = %s
                """,
                (error[:2000], document_id),
            )

    @staticmethod
    async def upsert_policy_detail_document(
        conn,
        policy_id: int,
        condition_profile_id: int | None,
        source_title: str,
        source_url: str | None,
        raw_text: str,
    ) -> int:
        document_id = policy_id * 10000 + 9000
        async with conn.cursor() as cur:
            await cur.execute(
                """
                    INSERT INTO policy_document (
                        document_id,
                        policy_id,
                        condition_profile_id,
                        source_title,
                        source_url,
                        source_type,
                        raw_text,
                        updated_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        'POLICY_DETAIL',
                        %s,
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (document_id) DO UPDATE SET
                        policy_id = EXCLUDED.policy_id,
                        condition_profile_id = EXCLUDED.condition_profile_id,
                        source_title = EXCLUDED.source_title,
                        source_url = EXCLUDED.source_url,
                        source_type = EXCLUDED.source_type,
                        raw_text = EXCLUDED.raw_text,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING document_id
                """,
                (
                    document_id,
                    policy_id,
                    condition_profile_id,
                    source_title,
                    source_url,
                    raw_text,
                ),
            )
            row = await cur.fetchone()
        return int(row[0])

    @staticmethod
    async def delete_policy_detail_embeddings_for_document(
        conn,
        document_id: int,
    ) -> int:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                    SELECT to_regclass('public.langchain_pg_collection') IS NOT NULL
                       AND to_regclass('public.langchain_pg_embedding') IS NOT NULL
                """
            )
            vector_tables_exist = (await cur.fetchone())[0]
            if not vector_tables_exist:
                return 0

            await cur.execute(
                """
                    DELETE FROM langchain_pg_embedding embedding
                    USING langchain_pg_collection collection
                    WHERE embedding.collection_id = collection.uuid
                      AND collection.name = 'policy_documents'
                      AND (
                        embedding.cmetadata->>'document_id' = %s::text
                        OR embedding.id IN (
                            SELECT chunk_id::text
                            FROM policy_document_chunk
                            WHERE document_id = %s
                        )
                      )
                      AND COALESCE(
                        embedding.cmetadata->>'source_type',
                        'POLICY_DETAIL'
                      ) = 'POLICY_DETAIL'
                """,
                (document_id, document_id),
            )
            return cur.rowcount or 0

    @staticmethod
    async def delete_policy_reference_embeddings_for_document(
        conn,
        document_id: int,
    ) -> int:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                    SELECT to_regclass('public.langchain_pg_collection') IS NOT NULL
                       AND to_regclass('public.langchain_pg_embedding') IS NOT NULL
                """
            )
            vector_tables_exist = (await cur.fetchone())[0]
            if not vector_tables_exist:
                return 0

            await cur.execute(
                """
                    DELETE FROM langchain_pg_embedding embedding
                    USING langchain_pg_collection collection
                    WHERE embedding.collection_id = collection.uuid
                      AND collection.name = 'policy_documents'
                      AND (
                        embedding.cmetadata->>'document_id' = %s::text
                        OR embedding.id IN (
                            SELECT chunk_id::text
                            FROM policy_document_chunk
                            WHERE document_id = %s
                        )
                      )
                      AND embedding.cmetadata->>'source_type' = 'POLICY_REFERENCE'
                """,
                (document_id, document_id),
            )
            return cur.rowcount or 0

    @staticmethod
    async def replace_document_chunks(
        conn,
        document_id: int,
        chunk_documents: list[Document],
    ) -> int:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM policy_document_chunk WHERE document_id = %s",
                (document_id,),
            )

            for chunk_index, document in enumerate(chunk_documents):
                chunk_id = document_id * 10000 + chunk_index
                metadata = dict(document.metadata)
                metadata["chunk_id"] = chunk_id
                metadata["document_id"] = document_id
                metadata["chunk_index"] = chunk_index

                await cur.execute(
                    """
                        INSERT INTO policy_document_chunk (
                            chunk_id,
                            document_id,
                            chunk_index,
                            chunk_text,
                            metadata_json
                        )
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        chunk_id,
                        document_id,
                        chunk_index,
                        document.page_content,
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )

            await cur.execute(
                """
                    UPDATE policy_document
                    SET ingest_status = 'CHUNK_READY',
                        ingest_error = NULL,
                        embedded_metadata_version = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE document_id = %s
                """,
                (document_id,),
            )

        return len(chunk_documents)
