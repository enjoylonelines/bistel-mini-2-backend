"""One bounded real-provider probe for rerank usage metadata.

This sends only a fixed, non-personal recommendation fixture. It does not use a
database, RAG corpus, or real user request. Run only with explicit approval for
one external provider call and its associated cost.
"""

from __future__ import annotations

import asyncio
import json

from app.services.recommendation_rerank_service import RecommendationRerankService


def _base_result() -> dict:
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
        "summary": {"source": "approved-real-provider-usage-probe"},
    }


async def main() -> None:
    output = await RecommendationRerankService().rerank(
        merged_condition_json={"stage": "controlled"},
        candidates=[],
        assessments=[],
        base_result_json=_base_result(),
        result_limit=1,
        raw_query="고정 검증용 양육 지원을 찾고 있어요.",
    )
    summary = output.result_json.get("summary") or {}
    record = {
        "scope": "one approved external rerank call; fixed non-personal fixture; no database or RAG search",
        "fallback_used": output.fallback_used,
        "provider_call_count": output.provider_call_count,
        "provider_token_usage_available": output.provider_token_usage_available,
        "provider_token_usage": output.provider_token_usage,
        "summary_token_usage": summary.get("llm_provider_token_usage"),
        "error_type": (output.error or "").split(":", 1)[0] or None,
    }
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if not output.provider_token_usage_available:
        raise SystemExit("provider response did not expose token usage")


if __name__ == "__main__":
    asyncio.run(main())
