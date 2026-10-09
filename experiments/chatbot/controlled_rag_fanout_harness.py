"""Measure candidate-level RAG fan-out control with an in-process fake searcher.

This invokes RecommendationService._search_evidences(), the same fan-out path
used before reranking, while replacing only the external chunk-search call. It
does not contact an embedding provider, database, queue, or production system.

Example:
  PYTHONPATH=. .venv/bin/python experiments/chatbot/controlled_rag_fanout_harness.py \
    --output output/controlled-rag-fanout.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.services.recommendation_candidate_service import PolicyCandidate
from app.services.recommendation_evidence_lane import ProcessLocalEvidenceLane
from app.services.recommendation_service import RecommendationService


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return round(ordered[round((len(ordered) - 1) * percentile)], 3)


def _candidates(count: int) -> list[PolicyCandidate]:
    return [
        PolicyCandidate(
            policy=SimpleNamespace(
                policy_id=1000 + index,
                policy_name=f"격리 정책 {index}",
                policy_code=f"CONTROL-{index}",
                benefit_type=None,
            ),
            detail=SimpleNamespace(target_description="격리 대상"),
            retrieval_score=0.8,
            candidate_status="CANDIDATE",
            filter_match_json={},
            matched_rules=[],
        )
        for index in range(count)
    ]


async def _run_once(
    *, candidate_count: int, delay_seconds: float, capacity: int | None
) -> dict[str, Any]:
    in_flight = 0
    max_in_flight = 0
    call_count = 0

    async def fake_chunk_searcher(**_kwargs: Any) -> list[Any]:
        nonlocal call_count, in_flight, max_in_flight
        call_count += 1
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(delay_seconds)
            return []
        finally:
            in_flight -= 1

    service = RecommendationService(
        chunk_searcher=fake_chunk_searcher,
        evidence_timeout_seconds=max(5, delay_seconds * candidate_count * 2),
        evidence_lane=(
            ProcessLocalEvidenceLane(capacity) if capacity is not None else None
        ),
    )
    started_at = time.perf_counter()
    evidences, error, debug = await service._search_evidences(
        condition={"stage": "controlled", "needs": ["fanout"]},
        candidates=_candidates(candidate_count),
    )
    return {
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
        "search_call_count": call_count,
        "returned_evidence_count": len(evidences),
        "fake_searcher_max_in_flight": max_in_flight,
        "service_lane": debug["lane"],
        "error": error,
    }


async def _measure(
    *, candidate_count: int, delay_seconds: float, capacity: int | None, runs: int
) -> dict[str, Any]:
    samples = [
        await _run_once(
            candidate_count=candidate_count,
            delay_seconds=delay_seconds,
            capacity=capacity,
        )
        for _ in range(runs)
    ]
    elapsed = [sample["elapsed_ms"] for sample in samples]
    return {
        "mode": "unbounded" if capacity is None else f"capacity_{capacity}",
        "configured_capacity": capacity,
        "runs": runs,
        "candidate_count": candidate_count,
        "fake_delay_ms": round(delay_seconds * 1000, 3),
        "all_calls_completed": all(
            sample["search_call_count"] == candidate_count and sample["error"] is None
            for sample in samples
        ),
        "max_in_flight": max(sample["fake_searcher_max_in_flight"] for sample in samples),
        "elapsed_p50_ms": round(statistics.median(elapsed), 3),
        "elapsed_p95_ms": _percentile(elapsed, 0.95),
        "lane_samples": [sample["service_lane"] for sample in samples],
    }


async def _run_queued_cancellation_probe() -> dict[str, Any]:
    """Cancel after one fake search starts and remaining work is lane-queued.

    The service receives the same execution-activity callback supplied by the
    graph node in production. This narrow probe keeps that callback in-process;
    durable repository integration is covered by the graph-node regression.
    """
    active = True
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    provider_calls = 0

    async def fake_chunk_searcher(**_kwargs: Any) -> list[Any]:
        nonlocal provider_calls
        provider_calls += 1
        first_started.set()
        await release_first.wait()
        return []

    async def execution_is_active() -> bool:
        return active

    service = RecommendationService(
        chunk_searcher=fake_chunk_searcher,
        evidence_timeout_seconds=5,
        evidence_lane=ProcessLocalEvidenceLane(1),
    )
    search_task = asyncio.create_task(
        service._search_evidences(
            condition={"stage": "controlled", "needs": ["cancellation"]},
            candidates=_candidates(4),
            execution_is_active=execution_is_active,
        )
    )
    await first_started.wait()
    active = False
    release_first.set()
    evidences, error, debug = await search_task
    return {
        "scope": "in-process fake search and execution callback; no provider or database",
        "lane_capacity": 1,
        "candidate_count": 4,
        "provider_calls_started": provider_calls,
        "avoided_calls": debug["avoided_call_count"],
        "estimated_embedding_input_tokens_avoided": debug[
            "estimated_embedding_input_tokens_avoided"
        ],
        "token_estimate_model": debug["token_estimate_model"],
        "returned_evidence_count": len(evidences),
        "error": error,
    }


async def main(args: argparse.Namespace) -> dict[str, Any]:
    modes = [None, 1, 2]
    return {
        "scope": "in-process fake chunk search only; not provider or production capacity evidence",
        "contract": "candidate-level RAG fan-out is separate from the rerank lane",
        "input": {
            "candidate_count": args.candidate_count,
            "fake_delay_ms": args.delay_ms,
            "runs_per_mode": args.runs,
        },
        "results": [
            await _measure(
                candidate_count=args.candidate_count,
                delay_seconds=args.delay_ms / 1000,
                capacity=capacity,
                runs=args.runs,
            )
            for capacity in modes
        ],
        "queued_cancellation_probe": await _run_queued_cancellation_probe(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-count", type=int, default=4)
    parser.add_argument("--delay-ms", type=float, default=20)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--output", type=Path, default=Path("output/controlled-rag-fanout.json")
    )
    parsed = parser.parse_args()
    if parsed.candidate_count < 1 or parsed.delay_ms <= 0 or parsed.runs < 1:
        parser.error("candidate-count, delay-ms, and runs must be positive")
    payload = asyncio.run(main(parsed))
    parsed.output.parent.mkdir(parents=True, exist_ok=True)
    parsed.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
