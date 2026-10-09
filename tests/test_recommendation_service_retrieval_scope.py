import asyncio
from types import SimpleNamespace

from app.services.recommendation_candidate_service import PolicyCandidate
from app.services.recommendation_evidence_lane import ProcessLocalEvidenceLane
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


def test_recommendation_evidence_lane_bounds_candidate_fan_out() -> None:
    in_flight = 0
    max_in_flight = 0

    async def fake_chunk_searcher(**kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.01)
            return []
        finally:
            in_flight -= 1

    service = RecommendationService(
        chunk_searcher=fake_chunk_searcher,
        evidence_timeout_seconds=5,
        evidence_lane=ProcessLocalEvidenceLane(capacity=1),
    )
    candidates = [
        PolicyCandidate(
            policy=SimpleNamespace(
                policy_id=policy_id,
                policy_name=f"정책 {policy_id}",
                policy_code=str(policy_id),
                benefit_type=None,
            ),
            detail=SimpleNamespace(target_description="대상"),
            retrieval_score=0.8,
            candidate_status="CANDIDATE",
            filter_match_json={},
            matched_rules=[],
        )
        for policy_id in (101, 202, 303)
    ]

    _, error, debug = asyncio.run(
        service._search_evidences(
            condition={"stage": "pregnancy", "needs": ["의료비"]},
            candidates=candidates,
        )
    )

    assert error is None
    assert max_in_flight == 1
    assert debug["search_call_count"] == 3
    assert debug["lane"] is not None
    assert debug["lane"]["capacity"] == 1
    assert debug["lane"]["max_in_flight"] == 1
    assert debug["lane"]["queue_wait_p95_ms"] > 0


def test_cancelled_evidence_search_is_dropped_before_provider_call() -> None:
    calls = 0

    async def fake_chunk_searcher(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("cancelled request must not start evidence search")

    async def inactive_execution() -> bool:
        return False

    service = RecommendationService(
        chunk_searcher=fake_chunk_searcher,
        evidence_timeout_seconds=5,
        evidence_lane=ProcessLocalEvidenceLane(capacity=1),
    )
    candidate = PolicyCandidate(
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
    )

    evidences, error, debug = asyncio.run(
        service._search_evidences(
            condition={"stage": "pregnancy", "needs": ["의료비"]},
            candidates=[candidate],
            execution_is_active=inactive_execution,
        )
    )

    assert calls == 0
    assert evidences == []
    assert error is None
    assert debug["search_call_count"] == 0
    assert debug["avoided_call_count"] == 1
    assert debug["estimated_embedding_input_tokens_avoided"] is not None
    assert debug["estimated_embedding_input_tokens_avoided"] > 0
    assert debug["token_estimate_model"] == "text-embedding-3-large"


def test_failed_evidence_search_counts_started_provider_call() -> None:
    async def fake_chunk_searcher(**kwargs):
        raise RuntimeError("controlled search failure")

    service = RecommendationService(
        chunk_searcher=fake_chunk_searcher,
        evidence_timeout_seconds=5,
    )
    candidate = PolicyCandidate(
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
    )

    _, error, debug = asyncio.run(
        service._search_evidences(
            condition={"stage": "pregnancy", "needs": ["의료비"]},
            candidates=[candidate],
        )
    )

    assert "controlled search failure" in (error or "")
    assert debug["search_call_count"] == 1
    assert debug["avoided_call_count"] == 0
