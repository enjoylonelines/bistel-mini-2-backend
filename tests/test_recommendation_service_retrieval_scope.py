import asyncio
from types import SimpleNamespace

from app.services.recommendation_candidate_service import PolicyCandidate
from app.services.recommendation_service import RecommendationService


def test_recommendation_evidence_search_is_policy_scoped_per_candidate() -> None:
    calls: list[dict] = []

    async def fake_chunk_searcher(**kwargs):
        calls.append(kwargs)
        return []

    service = RecommendationService(
        chunk_searcher=fake_chunk_searcher,
        evidence_timeout_seconds=5,
    )
    candidates = [
        PolicyCandidate(
            policy=SimpleNamespace(
                policy_id=101,
                policy_name="정책 A",
                policy_code="A",
                benefit_type=None,
            ),
            detail=SimpleNamespace(target_description="대상 A"),
            retrieval_score=0.8,
            candidate_status="CANDIDATE",
            filter_match_json={},
            matched_rules=[],
        ),
        PolicyCandidate(
            policy=SimpleNamespace(
                policy_id=202,
                policy_name="정책 B",
                policy_code="B",
                benefit_type=None,
            ),
            detail=SimpleNamespace(target_description="대상 B"),
            retrieval_score=0.7,
            candidate_status="CANDIDATE",
            filter_match_json={},
            matched_rules=[],
        ),
    ]

    evidences, error, _ = asyncio.run(
        service._search_evidences(
            condition={"stage": "pregnancy", "needs": ["의료비"]},
            candidates=candidates,
        )
    )

    assert evidences == []
    assert error is None
    assert len(calls) == 2
    assert {tuple(call["policy_ids"]) for call in calls} == {(101,), (202,)}
    assert all(call["top_k"] == 3 for call in calls)
