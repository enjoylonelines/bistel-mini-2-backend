"""Probe a metadata-aware application-section fallback on the local RAG stack.

The probe uses the real PolicyRagService vector store and the disposable local
pgvector fixture. It is mechanism evidence only, not a production-quality
retrieval benchmark.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault(
    "PSYCOPG_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault("OPENAI_API_KEY", "local-ollama")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
os.environ.setdefault("JWT_SECRET_KEY", "local-deep-dive-only")

from langchain_openai import OpenAIEmbeddings

import app.services.policy_rag_service as policy_rag_module
from app.services.policy_rag_service import PolicyRagService
from experiments.chatbot.application_section_subtype import (
    classify_application_subtype,
)


def _local_init_embeddings(model: str, **_: object) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model="text-embedding-3-large",
        api_key="local-ollama",
        base_url="http://127.0.0.1:11434/v1",
        check_embedding_ctx_length=False,
    )


policy_rag_module.init_embeddings = _local_init_embeddings


SECTION_BY_SUBTYPE = {
    "method": "신청 방법",
    "period": "신청 기간",
}


@dataclass(slots=True)
class ProbeResult:
    baseline_sections: list[str | None]
    target_section: str | None
    fallback_used: bool
    selected_chunk_id: int | None
    selected_section: str | None
    baseline_latency_ms: float
    fallback_latency_ms: float


def build_section_filter(
    *,
    policy_id: int,
    source_type: str,
    section: str,
) -> dict[str, Any]:
    return {
        "$and": [
            {"source_type": source_type},
            {"policy_id": {"$in": [policy_id]}},
            {"section": section},
        ]
    }


async def run_probe(
    query: str,
    *,
    policy_id: int = 242,
    top_k: int = 5,
) -> ProbeResult:
    service = PolicyRagService()

    started = time.perf_counter()
    baseline = await service.search(
        query=query,
        k=top_k,
        source_type="POLICY_DETAIL",
        policy_ids=[policy_id],
    )
    baseline_latency_ms = (time.perf_counter() - started) * 1000

    baseline_sections = [item.section for item in baseline.results]
    subtype = classify_application_subtype(query)
    target_section = SECTION_BY_SUBTYPE.get(subtype)
    if target_section is None:
        return ProbeResult(
            baseline_sections=baseline_sections,
            target_section=None,
            fallback_used=False,
            selected_chunk_id=None,
            selected_section=None,
            baseline_latency_ms=baseline_latency_ms,
            fallback_latency_ms=0.0,
        )

    existing = next(
        (item for item in baseline.results if item.section == target_section),
        None,
    )
    if existing is not None:
        return ProbeResult(
            baseline_sections=baseline_sections,
            target_section=target_section,
            fallback_used=False,
            selected_chunk_id=existing.chunk_id,
            selected_section=existing.section,
            baseline_latency_ms=baseline_latency_ms,
            fallback_latency_ms=0.0,
        )

    started = time.perf_counter()
    filtered = await service._vectorstore().asimilarity_search_with_score(
        query=query,
        k=1,
        filter=build_section_filter(
            policy_id=policy_id,
            source_type="POLICY_DETAIL",
            section=target_section,
        ),
    )
    fallback_latency_ms = (time.perf_counter() - started) * 1000
    if not filtered:
        selected_chunk_id = None
        selected_section = None
    else:
        selected = service._to_search_result(
            document=filtered[0][0],
            distance=filtered[0][1],
        )
        selected_chunk_id = selected.chunk_id
        selected_section = selected.section

    return ProbeResult(
        baseline_sections=baseline_sections,
        target_section=target_section,
        fallback_used=True,
        selected_chunk_id=selected_chunk_id,
        selected_section=selected_section,
        baseline_latency_ms=baseline_latency_ms,
        fallback_latency_ms=fallback_latency_ms,
    )


async def main() -> None:
    method_query = "법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?"
    period_query = "무료법률상담은 언제 신청할 수 있나요?"

    method_normal = await run_probe(method_query, top_k=5)
    method_forced_miss = await run_probe(method_query, top_k=4)
    period_normal = await run_probe(period_query, top_k=5)
    period_larger_pool = await run_probe(period_query, top_k=7)

    print(
        {
            "method_k5": asdict(method_normal),
            "method_k4_forced_missing_section": asdict(method_forced_miss),
            "period_k5": asdict(period_normal),
            "period_k7": asdict(period_larger_pool),
        }
    )


if __name__ == "__main__":
    asyncio.run(main())
