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

    nodes = RecommendationGraphNodes(
        rerank_service=FakeRerankService(),
        rerank_lane=ProcessLocalRerankLane(capacity=1),
        execution_event_repository=FakeEventRepository(),
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
