import json
from hashlib import sha256

from app.schemas.policy_rag_schema import PolicyRagSearchResponse


class EvidenceTraceRepository:
    @staticmethod
    async def record_search(conn, response: PolicyRagSearchResponse) -> int:
        """Persist retrieved chunks as evidence, not as asserted answers."""
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO decision_run (
                    decision_type, subject_ref, input_snapshot_json, status,
                    model_or_rule_version, finished_at
                ) VALUES (
                    'RAG_RETRIEVAL', %s, %s::jsonb, 'SUCCEEDED',
                    'policy-rag-search-v1', CURRENT_TIMESTAMP
                ) RETURNING decision_run_id
                """,
                (response.query, json.dumps({"query": response.query})),
            )
            decision_run_id = (await cur.fetchone())[0]

            for rank, result in enumerate(response.results, start=1):
                if result.document_id is None or result.chunk_id is None:
                    continue
                content_hash = sha256(result.chunk_text.encode("utf-8")).hexdigest()
                await cur.execute(
                    """
                    INSERT INTO evidence_span (
                        document_id, chunk_id, quoted_text, content_hash
                    ) VALUES (%s, %s, %s, %s)
                    RETURNING evidence_span_id
                    """,
                    (result.document_id, result.chunk_id, result.chunk_text, content_hash),
                )
                evidence_span_id = (await cur.fetchone())[0]
                await cur.execute(
                    """
                    INSERT INTO decision_claim (
                        decision_run_id, claim_type, claim_text, confidence,
                        review_status
                    ) VALUES (%s, 'RETRIEVED_EVIDENCE', %s, %s, 'UNREVIEWED')
                    RETURNING decision_claim_id
                    """,
                    (
                        decision_run_id,
                        f"검색 순위 {rank}: {result.policy_name or result.policy_code or '정책'}",
                        max(0.0, 1.0 - float(result.distance)),
                    ),
                )
                claim_id = (await cur.fetchone())[0]
                await cur.execute(
                    "INSERT INTO claim_evidence (decision_claim_id, evidence_span_id) VALUES (%s, %s)",
                    (claim_id, evidence_span_id),
                )
        return decision_run_id
