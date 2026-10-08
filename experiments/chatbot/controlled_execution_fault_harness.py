"""Run the recommendation rerank fault matrix against the isolated local DB.

This is a control-flow experiment, not a provider benchmark.  It uses the
real durable recommendation-request repository and a controllable in-process
fake provider.  No OpenAI-compatible endpoint, corpus retrieval, queue, or
production database is used.

Example:
  PYTHONPATH=. .venv/bin/python experiments/chatbot/controlled_execution_fault_harness.py \
    --output output/controlled-execution-fault-matrix.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# Keep this experiment explicitly on the disposable deep-dive database.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault(
    "PSYCOPG_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault("JWT_SECRET_KEY", "local-deep-dive-only")
os.environ.setdefault("OPENAI_API_KEY", "controlled-fake-provider")

from sqlalchemy import text

from app.ai.nodes.recommendation.recommendation_nodes import RecommendationGraphNodes
from app.common.ai_status import RequestStatus
from app.db.session import AsyncSessionLocal
from app.repositories.ai_request_repository import AiRequestRepository
from app.repositories.recommendation_execution_event_repository import (
    RecommendationExecutionEventRepository,
)
from app.schemas.recommendation_rerank_schema import (
    LlmRecommendationItem,
    LlmRecommendationRerankResult,
)
from app.services.recommendation_rerank_service import RecommendationRerankService
from app.services.recommendation_rerank_lane import ProcessLocalRerankLane


ScenarioKind = Literal[
    "fixed_delay",
    "long_tail_delay",
    "timeout",
    "rate_limit",
    "provider_5xx",
    "cancel_before_completion",
]


@dataclass(frozen=True)
class Scenario:
    name: ScenarioKind
    delay_seconds: float
    timeout_seconds: float
    provider_error: str | None = None
    cancel_while_in_flight: bool = False


SCENARIOS = (
    Scenario("fixed_delay", delay_seconds=0.002, timeout_seconds=0.05),
    Scenario("long_tail_delay", delay_seconds=0.02, timeout_seconds=0.05),
    Scenario("timeout", delay_seconds=0.05, timeout_seconds=0.002),
    Scenario(
        "rate_limit",
        delay_seconds=0.002,
        timeout_seconds=0.05,
        provider_error="429 rate limited",
    ),
    Scenario(
        "provider_5xx",
        delay_seconds=0.002,
        timeout_seconds=0.05,
        provider_error="503 provider unavailable",
    ),
    Scenario(
        "cancel_before_completion",
        delay_seconds=0.03,
        timeout_seconds=0.05,
        cancel_while_in_flight=True,
    ),
)


def _base_result() -> dict[str, Any]:
    return {
        "results": [
            {
                "policy_id": "100",
                "policy_name": "격리 실험 정책",
                "candidate_status": "CANDIDATE",
                "assessment_status": "LIKELY_MATCH",
                "match_score": 0.8,
                "reason_summary": "결정론 기반 결과",
                "recommendation_reason": "결정론 기반 결과",
                "benefit_summary": "격리 실험용 지원",
                "evidences": [],
            }
        ],
        "summary": {"source": "controlled-deterministic-fixture"},
    }


def _provider_success() -> LlmRecommendationRerankResult:
    return LlmRecommendationRerankResult(
        recommendations=[
            LlmRecommendationItem(
                policy_id="100",
                rerank_score=0.9,
                priority_score=0.9,
                priority_label="가장 먼저 확인",
                reason_summary="격리 fake provider가 만든 재순위 결과입니다.",
            )
        ]
    )


def _percentile_ms(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return round(ordered[index], 3)


async def _create_user_and_request() -> tuple[int, int]:
    suffix = uuid.uuid4().hex
    email = f"execution-harness-{suffix}@local.invalid"
    nickname = f"execution-harness-{suffix[:16]}"
    repository = AiRequestRepository()
    async with AsyncSessionLocal() as db:
        await repository.ensure_request_schema(db)
        user_id = (
            await db.execute(
                text(
                    """
                    INSERT INTO users (email, password_hash, nickname, role)
                    VALUES (:email, 'controlled-harness', :nickname, 'USER')
                    RETURNING user_id
                    """
                ),
                {"email": email, "nickname": nickname},
            )
        ).scalar_one()
        request = await repository.create(
            db,
            request_type="recommendation",
            user_id=int(user_id),
            source_type="CONTROLLED_FAKE_PROVIDER",
            raw_query="격리 실행 제어 실험",
            selected_conditions={"fixture": "controlled"},
        )
        await repository.update_status(
            db,
            request,
            RequestStatus.PROCESSING,
        )
        await RecommendationExecutionEventRepository().record(
            db,
            request_id=int(request.request_id),
            event_type="REQUEST_OFFERED",
            stage="ADMISSION",
            outcome="PROCESSING",
        )
        await db.commit()
        return int(user_id), int(request.request_id)


async def _claim(request_id: int) -> str:
    repository = AiRequestRepository()
    events = RecommendationExecutionEventRepository()
    async with AsyncSessionLocal() as db:
        token = await repository.claim_recommendation_execution(db, request_id)
        if token is None:
            raise RuntimeError(f"failed to claim controlled request {request_id}")
        await events.record(
            db,
            request_id=request_id,
            execution_token=token,
            event_type="EXECUTION_CLAIMED",
            stage="ADMISSION",
            outcome="CLAIMED",
        )
        await db.commit()
        return token


async def _cancel(request_id: int) -> None:
    repository = AiRequestRepository()
    events = RecommendationExecutionEventRepository()
    async with AsyncSessionLocal() as db:
        request = await repository.find_by_id(db, "recommendation", request_id)
        if request is None:
            raise RuntimeError("controlled request disappeared before cancellation")
        await repository.update_status(db, request, RequestStatus.CANCELLED)
        await events.record(
            db,
            request_id=request_id,
            event_type="REQUEST_CANCELLED",
            stage="TERMINAL",
            outcome="CANCELLED",
        )
        await db.commit()


async def _persist_terminal(
    *,
    request_id: int,
    execution_token: str,
    result_json: dict[str, Any],
) -> tuple[bool, str]:
    repository = AiRequestRepository()
    events = RecommendationExecutionEventRepository()
    summary = result_json.get("summary") or {}
    provider_call_count = int(summary.get("llm_provider_call_count") or 0)
    provider_token_usage_available = bool(
        summary.get("llm_provider_token_usage_available")
    )
    async with AsyncSessionLocal() as db:
        request = await repository.find_by_id(db, "recommendation", request_id)
        if request is None:
            raise RuntimeError("controlled request disappeared before terminal write")
        updated = await repository.update_result(
            db,
            request,
            result_json,
            execution_token=execution_token,
        )
        if updated is None:
            await events.record(
                db,
                request_id=request_id,
                execution_token=execution_token,
                event_type="LATE_WRITE_BLOCKED",
                stage="TERMINAL",
                outcome="DROPPED",
                details={
                    "provider_call_count": provider_call_count,
                    "provider_token_usage_available": provider_token_usage_available,
                },
            )
            await db.commit()
            return False, RequestStatus.CANCELLED.value

        terminal = await repository.update_status(
            db,
            request,
            RequestStatus.COMPLETED,
            execution_token=execution_token,
        )
        if terminal is None:
            raise RuntimeError("terminal status fence unexpectedly lost after result write")
        fallback_used = bool(summary.get("llm_fallback_used"))
        await events.record(
            db,
            request_id=request_id,
            execution_token=execution_token,
            event_type="EXECUTION_TERMINAL",
            stage="RERANK",
            outcome="FALLBACK" if fallback_used else "AUGMENTED",
            error_type=(
                str(summary.get("llm_error") or "").split(":", 1)[0] or None
            ),
            details={
                "llm_fallback_used": fallback_used,
                "provider_call_count": provider_call_count,
                "provider_token_usage_available": provider_token_usage_available,
            },
        )
        await db.commit()
        return True, RequestStatus.COMPLETED.value


async def _event_types(request_id: int) -> list[str]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                """
                SELECT event_type FROM recommendation_execution_event
                WHERE request_id = :request_id
                ORDER BY event_id
                """
            ),
            {"request_id": request_id},
        )
        return [str(value) for value in result.scalars().all()]


async def _event_records(request_id: int) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                """
                SELECT event_type, outcome, error_type, details_json
                FROM recommendation_execution_event
                WHERE request_id = :request_id
                ORDER BY event_id
                """
            ),
            {"request_id": request_id},
        )
        return [
            {
                "event_type": str(row.event_type),
                "outcome": row.outcome,
                "error_type": row.error_type,
                "details": dict(row.details_json or {}),
            }
            for row in result
        ]


async def _cleanup(user_id: int) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM users WHERE user_id = :user_id"), {"user_id": user_id})
        await db.commit()


async def _run_scenario(scenario: Scenario) -> dict[str, Any]:
    user_id, request_id = await _create_user_and_request()
    started = asyncio.Event()
    calls = 0
    in_flight = 0
    max_in_flight = 0

    async def fake_provider(_messages):
        nonlocal calls, in_flight, max_in_flight
        calls += 1
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        started.set()
        try:
            await asyncio.sleep(scenario.delay_seconds)
            if scenario.provider_error:
                raise RuntimeError(scenario.provider_error)
            return _provider_success()
        finally:
            in_flight -= 1

    try:
        token = await _claim(request_id)
        service = RecommendationRerankService(
            timeout_seconds=scenario.timeout_seconds,
            llm_invoker=fake_provider,
        )
        started_at = time.perf_counter()
        rerank_task = asyncio.create_task(
            service.rerank(
                merged_condition_json={},
                candidates=[],
                assessments=[],
                base_result_json=_base_result(),
                result_limit=1,
            )
        )
        await started.wait()
        if scenario.cancel_while_in_flight:
            await _cancel(request_id)
        rerank_output = await rerank_task
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
        write_applied, terminal_status = await _persist_terminal(
            request_id=request_id,
            execution_token=token,
            result_json=rerank_output.result_json,
        )
        events = await _event_types(request_id)
        return {
            "scenario": scenario.name,
            "request_id": request_id,
            "provider_calls": calls,
            "max_in_flight": max_in_flight,
            "elapsed_ms": elapsed_ms,
            "fallback_used": rerank_output.fallback_used,
            "fallback_error": rerank_output.error,
            "terminal_status": terminal_status,
            "durable_result_written": write_applied,
            "late_write_blocked": "LATE_WRITE_BLOCKED" in events,
            "events": events,
        }
    finally:
        await _cleanup(user_id)


async def _run_lane_probe(capacity: int) -> dict[str, Any]:
    """Force overlapping fake work through one process-local lane.

    This probe deliberately has no durable recommendation write. Durable
    transitions are covered by the fault rows above; this narrow slice measures
    the in-process semaphore's queueing behavior only.
    """
    lane = ProcessLocalRerankLane(capacity=capacity)
    start = asyncio.Event()
    arrivals = 0

    async def worker(name: str) -> dict[str, Any]:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            start.set()
        await start.wait()
        async with lane.admit() as admission:
            await asyncio.sleep(0.02)
            return {
                "worker": name,
                "queue_wait_ms": admission.queue_wait_ms,
                "in_flight": admission.in_flight,
                "max_in_flight": admission.max_in_flight,
            }

    rows = await asyncio.gather(worker("a"), worker("b"))
    return {
        "capacity": capacity,
        "arrival_pattern": "two simultaneous in-process fake-provider tasks",
        "rows": rows,
        "max_in_flight": max(int(row["max_in_flight"]) for row in rows),
        "queue_wait_p50_ms": round(
            statistics.median(float(row["queue_wait_ms"]) for row in rows), 3
        ),
        "queue_wait_p95_ms": _percentile_ms(
            [float(row["queue_wait_ms"]) for row in rows], 0.95
        ),
    }


async def _run_durable_queued_cancellation_probe() -> dict[str, Any]:
    """Cancel a claimed request while it waits behind another durable request.

    This is deliberately a two-request, capacity-one experiment.  It checks
    the same durable ownership fence used by the graph node immediately after
    lane admission and before the provider call.  It does not claim a global
    queue or distributed coordination.
    """
    first_user_id, first_request_id = await _create_user_and_request()
    queued_user_id, queued_request_id = await _create_user_and_request()
    lane = ProcessLocalRerankLane(capacity=1)
    first_provider_started = asyncio.Event()
    queued_lane_requested = asyncio.Event()
    calls = {"first": 0, "queued_cancel": 0}

    async def execute(
        *,
        label: str,
        request_id: int,
        execution_token: str,
    ) -> dict[str, Any]:
        async def fake_provider(_messages):
            calls[label] += 1
            if label == "first":
                first_provider_started.set()
            await asyncio.sleep(0.03)
            return _provider_success()

        if label == "queued_cancel":
            queued_lane_requested.set()
        async with lane.admit() as admission:
            repository = AiRequestRepository()
            async with AsyncSessionLocal() as db:
                active = await repository.has_active_recommendation_execution(
                    db, request_id, execution_token
                )
            if not active:
                async with AsyncSessionLocal() as db:
                    await RecommendationExecutionEventRepository().record(
                        db,
                        request_id=request_id,
                        execution_token=execution_token,
                        event_type="RERANK_DROPPED",
                        stage="RERANK_LANE",
                        outcome="CANCELLED_BEFORE_PROVIDER_CALL",
                        details={"provider_call_count": 0},
                    )
                    await db.commit()
                return {
                    "terminal_status": RequestStatus.CANCELLED.value,
                    "durable_result_written": False,
                    "provider_calls": calls[label],
                    "queue_wait_ms": admission.queue_wait_ms,
                    "dropped_before_provider_call": True,
                }

            rerank_output = await RecommendationRerankService(
                timeout_seconds=0.1,
                llm_invoker=fake_provider,
            ).rerank(
                merged_condition_json={},
                candidates=[],
                assessments=[],
                base_result_json=_base_result(),
                result_limit=1,
            )
            write_applied, terminal_status = await _persist_terminal(
                request_id=request_id,
                execution_token=execution_token,
                result_json=rerank_output.result_json,
            )
            return {
                "terminal_status": terminal_status,
                "durable_result_written": write_applied,
                "provider_calls": calls[label],
                "queue_wait_ms": admission.queue_wait_ms,
                "dropped_before_provider_call": False,
            }

    try:
        first_token = await _claim(first_request_id)
        queued_token = await _claim(queued_request_id)
        first_task = asyncio.create_task(
            execute(
                label="first",
                request_id=first_request_id,
                execution_token=first_token,
            )
        )
        await first_provider_started.wait()
        queued_task = asyncio.create_task(
            execute(
                label="queued_cancel",
                request_id=queued_request_id,
                execution_token=queued_token,
            )
        )
        await queued_lane_requested.wait()
        await _cancel(queued_request_id)
        first, queued_cancel = await asyncio.gather(first_task, queued_task)
        first["events"] = await _event_types(first_request_id)
        queued_cancel["events"] = await _event_types(queued_request_id)
        return {
            "capacity": 1,
            "arrival_pattern": (
                "first durable request admitted; second claimed request cancelled "
                "while waiting in the same process-local lane"
            ),
            "first": first,
            "queued_cancel": queued_cancel,
        }
    finally:
        await _cleanup(first_user_id)
        await _cleanup(queued_user_id)


async def _run_mixed_load_point(capacity: int) -> dict[str, Any]:
    """Run one deterministic mixed-load point through the actual rerank node.

    The node, optional lane, durable-fence read, and event repository are real.
    Candidate retrieval and the rest of the recommendation graph are intentionally
    outside this narrow rerank experiment; the base result is a fixed fixture.
    """
    lane = ProcessLocalRerankLane(capacity=capacity)
    first_provider_started = asyncio.Event()
    cancelled_request_queued = asyncio.Event()
    actual_events = RecommendationExecutionEventRepository()
    requests: list[dict[str, Any]] = []
    tasks: list[asyncio.Task] = []

    for label in ("augmented", "rate_limit", "timeout", "queued_cancel"):
        user_id, request_id = await _create_user_and_request()
        requests.append(
            {
                "label": label,
                "user_id": user_id,
                "request_id": request_id,
                "execution_token": await _claim(request_id),
                "provider_calls": 0,
            }
        )

    cancelled_request_id = next(
        int(row["request_id"])
        for row in requests
        if row["label"] == "queued_cancel"
    )

    class ObservingEvents:
        async def record(self, db, **event):
            await actual_events.record(db, **event)
            if (
                event.get("request_id") == cancelled_request_id
                and event.get("event_type") == "RERANK_QUEUED"
            ):
                cancelled_request_queued.set()

    async def run_request(row: dict[str, Any]) -> dict[str, Any]:
        label = str(row["label"])

        async def fake_provider(_messages):
            row["provider_calls"] = int(row["provider_calls"]) + 1
            if label == "augmented":
                first_provider_started.set()
                await asyncio.sleep(0.03)
                return _provider_success()
            if label == "rate_limit":
                await asyncio.sleep(0.03)
                raise RuntimeError("429 controlled mixed-load rate limit")
            if label == "timeout":
                await asyncio.sleep(0.03)
                return _provider_success()
            raise AssertionError("queued cancellation must not call the provider")

        timeout_seconds = 0.003 if label == "timeout" else 0.1
        node = RecommendationGraphNodes(
            rerank_service=RecommendationRerankService(
                timeout_seconds=timeout_seconds,
                llm_invoker=fake_provider,
            ),
            rerank_lane=lane,
            execution_event_repository=ObservingEvents(),
            request_repository=AiRequestRepository(),
        )
        started_at = time.perf_counter()
        async with AsyncSessionLocal() as db:
            state = await node.llm_rerank(
                {
                    "db": db,
                    "request_id": int(row["request_id"]),
                    "execution_token": str(row["execution_token"]),
                    "merged_condition_json": {},
                    "base_result_json": _base_result(),
                    "result_json": _base_result(),
                    "candidates": [],
                    "assessments": [],
                }
            )
            await db.commit()
        result_json = dict(state["result_json"])
        write_applied, terminal_status = await _persist_terminal(
            request_id=int(row["request_id"]),
            execution_token=str(row["execution_token"]),
            result_json=result_json,
        )
        events = await _event_records(int(row["request_id"]))
        summary = result_json.get("summary") or {}
        return {
            "label": label,
            "request_id": row["request_id"],
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "terminal_status": terminal_status,
            "durable_result_written": write_applied,
            "provider_calls": row["provider_calls"],
            "fallback_used": bool(summary.get("llm_fallback_used")),
            "fallback_error": summary.get("llm_error"),
            "skipped_due_to_cancellation": bool(
                summary.get("llm_skipped_due_to_cancellation")
            ),
            "events": events,
        }

    try:
        augmented = next(row for row in requests if row["label"] == "augmented")
        augmented_task = asyncio.create_task(run_request(augmented))
        tasks.append(augmented_task)
        await first_provider_started.wait()
        trailing_tasks = [
            asyncio.create_task(run_request(row))
            for row in requests
            if row["label"] != "augmented"
        ]
        tasks.extend(trailing_tasks)
        await cancelled_request_queued.wait()
        await _cancel(cancelled_request_id)
        rows = [await augmented_task, *await asyncio.gather(*trailing_tasks)]
        queue_waits = [
            float(event["details"].get("queue_wait_ms", 0))
            for row in rows
            for event in row["events"]
            if event["event_type"] == "RERANK_ADMITTED"
        ]
        max_in_flight = max(
            int(event["details"].get("max_in_flight", 0))
            for row in rows
            for event in row["events"]
            if event["event_type"] == "RERANK_ADMITTED"
        )
        elapsed = [float(row["elapsed_ms"]) for row in rows]
        return {
            "capacity": capacity,
            "arrival_pattern": (
                "one delayed augmentation, then 429, timeout, and a request "
                "cancelled after it enters the node's lane queue"
            ),
            "scope": "one process; actual rerank node and durable fence; fixed base result",
            "summary": {
                "offered": len(rows),
                "admitted": len(queue_waits),
                "max_in_flight": max_in_flight,
                "queue_wait_p50_ms": round(statistics.median(queue_waits), 3),
                "queue_wait_p95_ms": _percentile_ms(queue_waits, 0.95),
                "completion_p50_ms": round(statistics.median(elapsed), 3),
                "completion_p95_ms": _percentile_ms(elapsed, 0.95),
                "augmented": sum(
                    row["terminal_status"] == "COMPLETED"
                    and not row["fallback_used"]
                    for row in rows
                ),
                "fallback": sum(bool(row["fallback_used"]) for row in rows),
                "cancelled": sum(row["terminal_status"] == "CANCELLED" for row in rows),
                "timeout": sum(
                    "TimeoutError" in str(row["fallback_error"] or "") for row in rows
                ),
                "rate_limit": sum(
                    "429" in str(row["fallback_error"] or "") for row in rows
                ),
                "provider_calls": sum(int(row["provider_calls"]) for row in rows),
                "queued_cancel_provider_calls": next(
                    int(row["provider_calls"])
                    for row in rows
                    if row["label"] == "queued_cancel"
                ),
                "late_write_blocked": sum(
                    any(
                        event["event_type"] == "LATE_WRITE_BLOCKED"
                        for event in row["events"]
                    )
                    for row in rows
                ),
            },
            "rows": rows,
        }
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for row in requests:
            await _cleanup(int(row["user_id"]))


async def main(
    output_path: Path | None,
    lane_capacities: list[int],
) -> dict[str, Any]:
    rows = [await _run_scenario(scenario) for scenario in SCENARIOS]
    lane_probes = [await _run_lane_probe(capacity) for capacity in lane_capacities]
    durable_queued_cancellation_probe = await _run_durable_queued_cancellation_probe()
    mixed_load_points = [
        await _run_mixed_load_point(capacity) for capacity in lane_capacities
    ]
    latencies = [float(row["elapsed_ms"]) for row in rows]
    result = {
        "experiment": "controlled_recommendation_rerank_fault_matrix",
        "scope": {
            "database": "isolated local PostgreSQL on 127.0.0.1:55432",
            "provider": "in-process controlled fake",
            "concurrency_scope": "process-local lane only; no global limit",
            "not_production_evidence": True,
        },
        "scenario_count": len(rows),
        "summary": {
            "offered": len(rows),
            "augmented": sum(
                row["terminal_status"] == "COMPLETED" and not row["fallback_used"]
                for row in rows
            ),
            "fallback": sum(bool(row["fallback_used"]) for row in rows),
            "cancelled": sum(row["terminal_status"] == "CANCELLED" for row in rows),
            "late_write_blocked": sum(bool(row["late_write_blocked"]) for row in rows),
            "provider_calls": sum(int(row["provider_calls"]) for row in rows),
            "in_flight_cancel_provider_calls": next(
                int(row["provider_calls"])
                for row in rows
                if row["scenario"] == "cancel_before_completion"
            ),
            "queued_cancel_provider_calls": int(
                durable_queued_cancellation_probe["queued_cancel"]["provider_calls"]
            ),
            "elapsed_p50_ms": round(statistics.median(latencies), 3),
            "elapsed_p95_ms": _percentile_ms(latencies, 0.95),
        },
        "rows": rows,
        "lane_probes": lane_probes,
        "durable_queued_cancellation_probe": durable_queued_cancellation_probe,
        "mixed_load_points": mixed_load_points,
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--lane-capacities", type=int, nargs="+", default=[1, 2])
    args = parser.parse_args()
    if any(capacity < 1 for capacity in args.lane_capacities):
        parser.error("--lane-capacities values must be positive")
    print(
        json.dumps(
            asyncio.run(main(args.output, args.lane_capacities)),
            ensure_ascii=False,
            indent=2,
        )
    )
