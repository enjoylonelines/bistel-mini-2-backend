"""Verify one real RAG search and its persisted retrieval-evidence trace.

The probe targets the disposable local PostgreSQL database by default. It uses
the configured embedding provider only for the query embedding, then reports
the rows created in decision_run, decision_claim, and evidence_span. It is not
a retrieval-quality benchmark or production latency result.

Example:
  PYTHONPATH=. .venv/bin/python experiments/chatbot/isolated_rag_evidence_probe.py \
    --query "산재근로자 심리상담 신청 방법" --policy-id 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault(
    "PSYCOPG_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault("JWT_SECRET_KEY", "local-deep-dive-only")

from app.ai.tools.policy_chunk_search_tool import search_policy_chunks
from app.common.psycopg_pool_conf import psycopg_pool


async def _latest_run_id() -> int:
    async with psycopg_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(MAX(decision_run_id), 0) FROM decision_run")
            return int((await cur.fetchone())[0])


async def _trace_counts(after_run_id: int) -> dict[str, int]:
    async with psycopg_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    COUNT(DISTINCT dr.decision_run_id),
                    COUNT(DISTINCT dc.decision_claim_id),
                    COUNT(DISTINCT ce.evidence_span_id)
                FROM decision_run dr
                LEFT JOIN decision_claim dc ON dc.decision_run_id = dr.decision_run_id
                LEFT JOIN claim_evidence ce ON ce.decision_claim_id = dc.decision_claim_id
                WHERE dr.decision_run_id > %s
                  AND dr.decision_type = 'RAG_RETRIEVAL'
                """,
                (after_run_id,),
            )
            runs, claims, spans = await cur.fetchone()
            return {
                "decision_runs": int(runs),
                "retrieved_evidence_claims": int(claims),
                "evidence_spans": int(spans),
            }


async def run_probe(
    *,
    query: str,
    policy_id: int,
    top_k: int,
) -> dict[str, Any]:
    await psycopg_pool.open()
    try:
        before_run_id = await _latest_run_id()
        started_at = time.perf_counter()
        chunks = await search_policy_chunks(
            query=query,
            policy_ids=[policy_id],
            top_k=top_k,
        )
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
        trace = await _trace_counts(before_run_id)
        return {
            "scope": {
                "database": "isolated local PostgreSQL on 127.0.0.1:55432",
                "search": "configured embedding provider plus local PGVector",
                "not_retrieval_quality_or_production_latency_evidence": True,
            },
            "query": query,
            "policy_id": policy_id,
            "top_k": top_k,
            "returned_chunk_count": len(chunks),
            "returned_chunk_ids": [chunk.chunk_id for chunk in chunks],
            "evidence_roles": [chunk.evidence_role for chunk in chunks],
            "search_and_trace_elapsed_ms": elapsed_ms,
            "persisted_trace": trace,
        }
    finally:
        await psycopg_pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--policy-id", type=int, required=True)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    result = asyncio.run(
        run_probe(query=args.query, policy_id=args.policy_id, top_k=args.top_k)
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
