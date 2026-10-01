"""Compare baseline K=5 with section-aware K=7 -> output K=5 locally.

This benchmark uses the disposable synthetic policy-242 corpus and local Ollama.
It is for mechanism validation only, not portfolio performance evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
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
os.environ.setdefault("OPENAI_API_KEY", "local-ollama")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
os.environ.setdefault("JWT_SECRET_KEY", "local-deep-dive-only")

from langchain_openai import OpenAIEmbeddings

import app.services.policy_rag_service as policy_rag_module
from app.ai.tools.policy_chunk_search_tool import (
    _select_section_aware_results,
    infer_application_section_subtype,
)
from app.services.policy_rag_service import PolicyRagService


FIXTURE = Path(__file__).with_name("application_section_challenge_set.json")


def _local_init_embeddings(model: str, **_: object) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model="text-embedding-3-large",
        api_key="local-ollama",
        base_url="http://127.0.0.1:11434/v1",
        check_embedding_ctx_length=False,
    )


policy_rag_module.init_embeddings = _local_init_embeddings


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def _timing(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * 0.95)))
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


async def main() -> None:
    cases: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    service = PolicyRagService()

    # warm up
    await service.search(
        query="신청 방법 알려줘",
        k=5,
        source_type="POLICY_DETAIL",
        policy_ids=[242],
    )

    rows: list[dict[str, Any]] = []
    baseline_latencies: list[float] = []
    challenger_latencies: list[float] = []

    for case in cases:
        predicted = infer_application_section_subtype(case["query"])

        started = time.perf_counter()
        baseline = await service.search(
            query=case["query"],
            k=5,
            source_type="POLICY_DETAIL",
            policy_ids=[242],
        )
        baseline_latencies.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        candidate_pool = await service.search(
            query=case["query"],
            k=7,
            source_type="POLICY_DETAIL",
            policy_ids=[242],
        )
        selected = _select_section_aware_results(
            candidate_pool.results,
            top_k=5,
            section_subtype=predicted,
        )
        challenger_latencies.append((time.perf_counter() - started) * 1000)

        baseline_sections = [item.section for item in baseline.results]
        challenger_sections = [item.section for item in selected]
        baseline_ids = [item.chunk_id for item in baseline.results]
        challenger_ids = [item.chunk_id for item in selected]
        target = case["target_section"]

        rows.append(
            {
                "id": case["id"],
                "query": case["query"],
                "expected_subtype": case["expected_subtype"],
                "predicted_subtype": predicted,
                "subtype_correct": predicted == case["expected_subtype"],
                "target_section": target,
                "baseline_hit": target in baseline_sections if target else None,
                "challenger_hit": target in challenger_sections if target else None,
                "negative_ranking_unchanged": (
                    baseline_ids == challenger_ids if target is None else None
                ),
                "baseline_sections": baseline_sections,
                "challenger_sections": challenger_sections,
            }
        )

    application_rows = [row for row in rows if row["target_section"] is not None]
    negative_rows = [row for row in rows if row["target_section"] is None]
    subtype_correct = sum(row["subtype_correct"] for row in rows)
    baseline_hits = sum(bool(row["baseline_hit"]) for row in application_rows)
    challenger_hits = sum(bool(row["challenger_hit"]) for row in application_rows)
    negative_unchanged = sum(
        bool(row["negative_ranking_unchanged"]) for row in negative_rows
    )

    result = {
        "environment": "local synthetic policy-242 + nomic-embed-text",
        "case_count": len(rows),
        "subtype_accuracy_pct": _pct(subtype_correct, len(rows)),
        "application_case_count": len(application_rows),
        "baseline_section_hit_at_5_pct": _pct(
            baseline_hits, len(application_rows)
        ),
        "challenger_section_hit_at_5_pct": _pct(
            challenger_hits, len(application_rows)
        ),
        "section_hit_delta_pp": round(
            _pct(challenger_hits, len(application_rows))
            - _pct(baseline_hits, len(application_rows)),
            2,
        ),
        "negative_case_count": len(negative_rows),
        "negative_top5_unchanged_pct": _pct(
            negative_unchanged, len(negative_rows)
        ),
        "baseline_timing": _timing(baseline_latencies),
        "challenger_timing": _timing(challenger_latencies),
        "rows": rows,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
