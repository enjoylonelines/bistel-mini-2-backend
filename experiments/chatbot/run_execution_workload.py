"""Run a bounded, local recommendation execution workload.

This is deliberately a fake-provider harness.  It exercises the production
RAG evidence lane and its cancellation check without issuing HTTP requests,
calling an LLM provider, or touching a user database.  Run one axis at a time
before using the ``mixed`` profile:

  PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_workload.py \
    --profile normal --repeat 20 --concurrency 1

  PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_workload.py \
    --profile mixed --repeat 20 --concurrency 8 --lane-capacity 2
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


BASE = Path(__file__).resolve().parent
DEFAULT_DATASET = BASE / "datasets" / "dodam_execution_workload_v1.jsonl"
PROFILES = {"normal", "cancellation", "fault", "mixed", "all"}


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("dataset must contain at least one workload fixture")
    if len({row["scenario_id"] for row in rows}) != len(rows):
        raise ValueError("scenario_id values must be unique")
    for row in rows:
        if row["axis"] not in {"normal", "cancellation", "fault", "mixed"}:
            raise ValueError("unsupported workload axis")
        if int(row["candidate_count"]) < 1:
            raise ValueError("candidate_count must be positive")
        if row["provider_outcome"] not in {"success", "provider_error"}:
            raise ValueError("unsupported provider_outcome")
        if row["cancel_after_ms"] is not None and int(row["cancel_after_ms"]) < 0:
            raise ValueError("cancel_after_ms must be non-negative")
    return rows


def _candidates(count: int) -> list[PolicyCandidate]:
    return [
        PolicyCandidate(
            policy=SimpleNamespace(
                policy_id=10_000 + index,
                policy_name=f"워크로드 정책 {index}",
                policy_code=f"WORKLOAD-{index}",
                benefit_type=None,
            ),
            detail=SimpleNamespace(target_description="워크로드 대상"),
            retrieval_score=0.8,
            candidate_status="CANDIDATE",
            filter_match_json={},
            matched_rules=[],
        )
        for index in range(count)
    ]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if percentile == 50:
        return round(statistics.median(values), 3)
    index = max(0, min(len(values) - 1, int((len(values) - 1) * percentile / 100)))
    return round(sorted(values)[index], 3)


async def _run_request(
    item: dict[str, Any],
    *,
    lane: ProcessLocalEvidenceLane,
    provider_delay_ms: int,
) -> dict[str, Any]:
    active = True
    provider_calls = 0
    post_cancel_calls = 0
    cancel_after_ms = item["cancel_after_ms"]
    if cancel_after_ms == 0:
        active = False

    async def execution_is_active() -> bool:
        return active

    async def fake_chunk_searcher(**_kwargs: Any) -> list[Any]:
        nonlocal provider_calls, post_cancel_calls
        provider_calls += 1
        if not active:
            post_cancel_calls += 1
        await asyncio.sleep(provider_delay_ms / 1000)
        if item["provider_outcome"] == "provider_error":
            raise RuntimeError("controlled workload provider error")
        return []

    async def cancel_later() -> None:
        nonlocal active
        if cancel_after_ms is None or cancel_after_ms == 0:
            return
        await asyncio.sleep(int(cancel_after_ms) / 1000)
        active = False

    service = RecommendationService(
        chunk_searcher=fake_chunk_searcher,
        evidence_timeout_seconds=5,
        evidence_lane=lane,
    )
    started_at = time.perf_counter()
    canceller = asyncio.create_task(cancel_later())
    try:
        _, error, debug = await service._search_evidences(
            condition={"stage": "workload", "needs": ["execution-control"]},
            candidates=_candidates(int(item["candidate_count"])),
            execution_is_active=execution_is_active,
        )
    finally:
        await canceller
    return {
        "scenario_id": item["scenario_id"],
        "axis": item["axis"],
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
        "provider_calls_started": provider_calls,
        "post_cancel_provider_calls": post_cancel_calls,
        "avoided_calls": int(debug["avoided_call_count"]),
        "error": error,
        "lane": debug["lane"],
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [float(row["elapsed_ms"]) for row in rows]
    waits = [
        float(row["lane"]["queue_wait_p95_ms"])
        for row in rows
        if row["lane"] is not None
    ]
    return {
        "request_count": len(rows),
        "elapsed_ms": {"p50": _percentile(elapsed, 50), "p95": _percentile(elapsed, 95), "max": round(max(elapsed), 3)},
        "queue_wait_p95_ms": _percentile(waits, 95),
        "provider_calls_started": sum(int(row["provider_calls_started"]) for row in rows),
        "post_cancel_provider_calls": sum(int(row["post_cancel_provider_calls"]) for row in rows),
        "avoided_calls": sum(int(row["avoided_calls"]) for row in rows),
        "requests_with_error": sum(row["error"] is not None for row in rows),
        "by_axis": {
            axis: sum(row["axis"] == axis for row in rows)
            for axis in sorted({str(row["axis"]) for row in rows})
        },
    }


async def run_workload(
    dataset_path: Path = DEFAULT_DATASET,
    *,
    profile: str = "all",
    repeat: int = 1,
    concurrency: int = 1,
    lane_capacity: int = 2,
    provider_delay_ms: int = 10,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(sorted(PROFILES))}")
    if repeat < 1 or concurrency < 1 or lane_capacity < 1 or provider_delay_ms < 0:
        raise ValueError("repeat, concurrency, lane_capacity must be positive; delay non-negative")
    dataset = _load_dataset(dataset_path)
    selected = dataset if profile == "all" else [row for row in dataset if row["axis"] == profile]
    if not selected:
        raise ValueError("profile has no workload fixtures")
    items = selected * repeat
    lane = ProcessLocalEvidenceLane(lane_capacity)
    limiter = asyncio.Semaphore(concurrency)

    async def limited(item: dict[str, Any]) -> dict[str, Any]:
        async with limiter:
            return await _run_request(
                item,
                lane=lane,
                provider_delay_ms=provider_delay_ms,
            )

    rows = await asyncio.gather(*(limited(item) for item in items))
    return {
        "workload_version": "v1",
        "scope": "local fake-provider RAG workload; no HTTP, database, LLM provider, or production traffic",
        "configuration": {
            "profile": profile,
            "repeat": repeat,
            "request_concurrency": concurrency,
            "process_local_lane_capacity": lane_capacity,
            "fake_provider_delay_ms": provider_delay_ms,
        },
        "summary": _summarize(rows),
        "requests": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--lane-capacity", type=int, default=2)
    parser.add_argument("--provider-delay-ms", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = asyncio.run(
        run_workload(
            args.dataset,
            profile=args.profile,
            repeat=args.repeat,
            concurrency=args.concurrency,
            lane_capacity=args.lane_capacity,
            provider_delay_ms=args.provider_delay_ms,
        )
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
