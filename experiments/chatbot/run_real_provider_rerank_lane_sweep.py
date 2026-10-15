"""Bounded real-provider rerank lane sweep using a fixed non-personal fixture.

The runner never calls the recommendation API, database, or RAG search. It
does make external provider calls only with ``--execute``. Keep the run count
small and explicit because it consumes provider quota and tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from typing import Any

from app.services.recommendation_rerank_lane import ProcessLocalRerankLane
from app.services.recommendation_rerank_service import RecommendationRerankService


def _base_result() -> dict[str, Any]:
    return {
        "results": [
            {
                "policy_id": "controlled-policy-1",
                "policy_name": "격리 검증용 양육 지원",
                "candidate_status": "CANDIDATE",
                "assessment_status": "RECOMMENDABLE",
                "match_score": 0.8,
                "reason_summary": "고정 검증 fixture의 결정론적 추천입니다.",
                "recommendation_reason": "고정 검증 fixture의 결정론적 추천입니다.",
                "benefit_summary": "격리 검증용 지원입니다.",
                "target_description": "고정 검증 fixture 대상",
                "evidences": [],
            }
        ],
        "summary": {"source": "approved-real-provider-lane-sweep"},
    }


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    return round(sorted(values)[math.ceil(len(values) * percentile / 100) - 1], 3)


async def _run_capacity(
    capacity: int,
    runs: int,
    request_concurrency: int | None = None,
) -> dict[str, Any]:
    lane = ProcessLocalRerankLane(capacity)
    service = RecommendationRerankService()
    concurrency = min(request_concurrency or capacity * 2, runs)
    limiter = asyncio.Semaphore(concurrency)

    async def run_one() -> dict[str, Any]:
        started_at = time.perf_counter()
        async with lane.admit() as admission:
            output = await service.rerank(
                merged_condition_json={"stage": "controlled"},
                candidates=[],
                assessments=[],
                base_result_json=_base_result(),
                result_limit=1,
                raw_query="고정 검증용 양육 지원을 찾고 있어요.",
            )
        return {
            "elapsed_ms": (time.perf_counter() - started_at) * 1000,
            "queue_wait_ms": admission.queue_wait_ms,
            "fallback_used": output.fallback_used,
            "error": output.error,
            "provider_call_count": output.provider_call_count,
            "provider_token_usage": output.provider_token_usage,
        }

    async def limited_run() -> dict[str, Any]:
        async with limiter:
            return await run_one()

    rows = await asyncio.gather(*(limited_run() for _ in range(runs)))
    elapsed = [float(row["elapsed_ms"]) for row in rows]
    waits = [float(row["queue_wait_ms"]) for row in rows]
    usage = [row["provider_token_usage"] for row in rows if row["provider_token_usage"]]
    return {
        "capacity": capacity,
        "concurrency": concurrency,
        "runs": runs,
        "elapsed_ms": {
            "p50": _percentile(elapsed, 50),
            "p95": _percentile(elapsed, 95),
            "max": round(max(elapsed), 3),
        },
        "queue_wait_ms": {
            "p50": _percentile(waits, 50),
            "p95": _percentile(waits, 95),
            "max": round(max(waits), 3),
        },
        "provider_calls": sum(int(row["provider_call_count"]) for row in rows),
        "fallbacks": sum(bool(row["fallback_used"]) for row in rows),
        "errors": sum(row["error"] is not None for row in rows),
        "provider_total_tokens": sum(int(item.get("total_tokens", 0)) for item in usage),
        "provider_tokens_per_request": round(
            sum(int(item.get("total_tokens", 0)) for item in usage) / len(rows), 3
        ),
    }


async def run_sweep(
    capacities: list[int],
    runs: int,
    request_concurrency: int | None = None,
) -> dict[str, Any]:
    return {
        "scope": "approved fixed non-personal rerank fixture; no database, RAG search, or recommendation API",
        "runs_per_capacity": runs,
        "results": [
            await _run_capacity(capacity, runs, request_concurrency)
            for capacity in capacities
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--capacities", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument(
        "--request-concurrency",
        type=int,
        help="Fixed offered load for every capacity; defaults to capacity × 2.",
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("Refusing external provider calls without --execute")
    if (
        args.runs < 1
        or any(capacity < 1 for capacity in args.capacities)
        or (args.request_concurrency is not None and args.request_concurrency < 1)
    ):
        raise SystemExit("runs and capacities must be positive")
    payload = asyncio.run(
        run_sweep(args.capacities, args.runs, args.request_concurrency)
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as output:
            output.write(rendered + "\n")
    print(rendered)
