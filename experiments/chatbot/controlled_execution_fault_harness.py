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
        summary = result_json.get("summary") or {}
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
            details={"llm_fallback_used": fallback_used},
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


async def main(output_path: Path | None) -> dict[str, Any]:
    rows = [await _run_scenario(scenario) for scenario in SCENARIOS]
    latencies = [float(row["elapsed_ms"]) for row in rows]
    result = {
        "experiment": "controlled_recommendation_rerank_fault_matrix",
        "scope": {
            "database": "isolated local PostgreSQL on 127.0.0.1:55432",
            "provider": "in-process controlled fake",
            "concurrency_scope": "no admission limit tested",
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
            "elapsed_p50_ms": round(statistics.median(latencies), 3),
            "elapsed_p95_ms": _percentile_ms(latencies, 0.95),
        },
        "rows": rows,
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main(args.output)), ensure_ascii=False, indent=2))
