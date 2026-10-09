import asyncio
from types import SimpleNamespace

from app.ai.nodes.recommendation.recommendation_nodes import RecommendationGraphNodes
from app.services.recommendation_rerank_lane import ProcessLocalRerankLane
from app.services.recommendation_rerank_service import RecommendationRerankOutput


def test_process_local_lane_serializes_overlapping_admissions() -> None:
    async def exercise():
        lane = ProcessLocalRerankLane(capacity=1)
        first_admitted = asyncio.Event()
        release_first = asyncio.Event()
        admissions = []

        async def first():
            async with lane.admit() as admission:
                admissions.append(("first", admission))
                first_admitted.set()
                await release_first.wait()

        async def second():
            await first_admitted.wait()
            async with lane.admit() as admission:
                admissions.append(("second", admission))

        first_task = asyncio.create_task(first())
        await first_admitted.wait()
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0.01)
        assert len(admissions) == 1
        release_first.set()
        await asyncio.gather(first_task, second_task)
        return admissions

    admissions = asyncio.run(exercise())

    first = admissions[0][1]
    second = admissions[1][1]
    assert first.in_flight == 1
    assert first.max_in_flight == 1
    assert second.in_flight == 1
    assert second.max_in_flight == 1
    assert second.queue_wait_ms > 0


def test_lane_emits_queue_admit_and_release_events() -> None:
    events: list[dict[str, object]] = []

    class FakeRerankService:
        async def rerank(self, **kwargs):
            return RecommendationRerankOutput(
                result_json={"results": [], "summary": {}},
                rerank_scores={},
                fallback_used=False,
            )

    class FakeEventRepository:
        async def record(self, db, **event):
            events.append(event)

    class ActiveRequestRepository:
        async def has_active_recommendation_execution(self, db, request_id, execution_token):
            return True

    nodes = RecommendationGraphNodes(
        rerank_service=FakeRerankService(),
        rerank_lane=ProcessLocalRerankLane(capacity=1),
        execution_event_repository=FakeEventRepository(),
        request_repository=ActiveRequestRepository(),
    )

    result = asyncio.run(
        nodes.llm_rerank(
            {
                "db": SimpleNamespace(),
                "request_id": 17,
                "execution_token": "owner",
                "merged_condition_json": {},
                "base_result_json": {"results": []},
            }
        )
    )

    assert result["llm_fallback_used"] is False
    assert [event["event_type"] for event in events] == [
        "RERANK_QUEUED",
        "RERANK_ADMITTED",
        "RERANK_RELEASED",
    ]
    details = events[1]["details"]
    assert isinstance(details, dict)
    assert details["capacity"] == 1
    assert details["queue_wait_ms"] >= 0
    assert details["in_flight"] == 1
    assert details["max_in_flight"] == 1


def test_lane_drops_cancelled_request_before_provider_call() -> None:
    events: list[dict[str, object]] = []

    class FakeRerankService:
        calls = 0

        async def rerank(self, **kwargs):
            self.calls += 1
            raise AssertionError("cancelled request must not call the provider")

    class FakeEventRepository:
        async def record(self, db, **event):
            events.append(event)

    class CancelledRequestRepository:
        async def has_active_recommendation_execution(self, db, request_id, execution_token):
            return False

    rerank_service = FakeRerankService()
    nodes = RecommendationGraphNodes(
        rerank_service=rerank_service,
        rerank_lane=ProcessLocalRerankLane(capacity=1),
        execution_event_repository=FakeEventRepository(),
        request_repository=CancelledRequestRepository(),
    )

    result = asyncio.run(
        nodes.llm_rerank(
            {
                "db": SimpleNamespace(),
                "request_id": 19,
                "execution_token": "cancelled-owner",
                "merged_condition_json": {},
                "base_result_json": {"results": [], "summary": {}},
            }
        )
    )

    assert rerank_service.calls == 0
    assert result["llm_rerank_result"] is None
    assert result["result_json"]["summary"]["llm_skipped_due_to_cancellation"] is True
    assert [event["event_type"] for event in events] == [
        "RERANK_QUEUED",
        "RERANK_ADMITTED",
        "RERANK_DROPPED",
    ]


def test_build_result_records_rag_call_avoidance_after_durable_cancellation() -> None:
    events: list[dict[str, object]] = []

    class FakeRerankService:
        def select_candidate_pool(self, **kwargs):
            return []

    class FakeRecommendationService:
        result_limit = 1

        async def build_result(self, **kwargs):
            assert await kwargs["execution_is_active"]() is False
            return {
                "results": [],
                "summary": {
                    "evidence_search_avoided_call_count": 2,
                    "evidence_estimated_embedding_input_tokens_avoided": 37,
                    "evidence_token_estimate_model": "text-embedding-3-large",
                },
            }

    class FakeEventRepository:
        async def record(self, db, **event):
            events.append(event)

    class CancelledRequestRepository:
        async def has_active_recommendation_execution(self, db, request_id, execution_token):
            return False

    nodes = RecommendationGraphNodes(
        recommendation_service=FakeRecommendationService(),
        rerank_service=FakeRerankService(),
        execution_event_repository=FakeEventRepository(),
        request_repository=CancelledRequestRepository(),
    )

    result = asyncio.run(
        nodes.build_result(
            {
                "db": SimpleNamespace(),
                "request_id": 23,
                "execution_token": "cancelled-owner",
                "merged_condition_json": {},
                "candidates": [],
                "assessments": [],
            }
        )
    )

    assert result["result_json"]["summary"]["evidence_search_avoided_call_count"] == 2
    assert [event["event_type"] for event in events] == ["RAG_EVIDENCE_DROPPED"]
    details = events[0]["details"]
    assert isinstance(details, dict)
    assert details["provider_call_count"] == 0
    assert details["avoided_call_count"] == 2
    assert details["estimated_embedding_input_tokens_avoided"] == 37
