import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.schemas.recommendation_rerank_schema import (
    LlmRecommendationItem,
    LlmRecommendationRerankResult,
)
from app.services.recommendation_rerank_service import (
    LlmInvocation,
    RecommendationRerankService,
)


def _base_result() -> dict[str, Any]:
    return {
        "results": [
            {
                "policy_id": "100",
                "policy_name": "테스트 정책",
                "candidate_status": "CANDIDATE",
                "assessment_status": "LIKELY_MATCH",
                "match_score": 0.8,
                "reason_summary": "룰 기반 결과",
                "recommendation_reason": "룰 기반 결과",
                "benefit_summary": "테스트 지원",
                "evidences": [],
            }
        ],
        "summary": {"source": "deterministic"},
    }


def _success() -> LlmRecommendationRerankResult:
    return LlmRecommendationRerankResult(
        recommendations=[
            LlmRecommendationItem(
                policy_id="100",
                rerank_score=0.9,
                priority_score=0.9,
                priority_label="가장 먼저 확인",
                reason_summary="사용자 상황에 맞는 지원입니다.",
            )
        ]
    )


async def _run(
    invoker: Callable[[list[tuple[str, str]]], Awaitable[LlmRecommendationRerankResult]],
    *,
    timeout_seconds: float = 0.05,
):
    service = RecommendationRerankService(
        timeout_seconds=timeout_seconds,
        llm_invoker=invoker,
    )
    return await service.rerank(
        merged_condition_json={},
        candidates=[],
        assessments=[],
        base_result_json=_base_result(),
        result_limit=1,
    )


@pytest.mark.parametrize("delay", [0, 0.01])
def test_controlled_fake_provider_fixed_and_long_tail_delay_succeed(delay: float) -> None:
    async def invoker(messages):
        await asyncio.sleep(delay)
        return _success()

    output = asyncio.run(_run(invoker))

    assert output.fallback_used is False
    assert output.result_json["summary"]["llm_rerank_used"] is True
    assert output.result_json["summary"]["llm_fallback_used"] is False


def test_fake_provider_usage_is_preserved_separately_from_fallback_state() -> None:
    async def invoker(messages):
        return LlmInvocation(
            result=_success(),
            provider_token_usage={
                "input_tokens": 120,
                "output_tokens": 45,
                "total_tokens": 165,
            },
        )

    output = asyncio.run(_run(invoker))

    assert output.fallback_used is False
    assert output.provider_token_usage_available is True
    assert output.provider_token_usage == {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    }
    assert output.result_json["summary"]["llm_provider_token_usage"] == {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    }


@pytest.mark.parametrize(
    ("name", "invoker", "timeout_seconds", "error_fragment"),
    [
        (
            "timeout",
            lambda: _delayed_success(0.05),
            0.001,
            "TimeoutError",
        ),
        ("rate_limit", lambda: _raise(RuntimeError("429 rate limited")), 0.05, "429"),
        ("provider_5xx", lambda: _raise(RuntimeError("503 provider error")), 0.05, "503"),
    ],
)
def test_controlled_fake_provider_failures_preserve_deterministic_fallback(
    name: str,
    invoker: Callable[[], Awaitable[LlmRecommendationRerankResult]],
    timeout_seconds: float,
    error_fragment: str,
) -> None:
    async def provider(messages):
        return await invoker()

    output = asyncio.run(_run(provider, timeout_seconds=timeout_seconds))

    assert output.fallback_used is True, name
    assert output.result_json["summary"]["llm_rerank_used"] is False
    assert output.result_json["summary"]["llm_fallback_used"] is True
    assert error_fragment in (output.error or "")
    assert output.result_json["results"][0]["policy_id"] == "100"


async def _delayed_success(delay: float) -> LlmRecommendationRerankResult:
    await asyncio.sleep(delay)
    return _success()


async def _raise(error: Exception) -> LlmRecommendationRerankResult:
    raise error
