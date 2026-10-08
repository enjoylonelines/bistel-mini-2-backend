import asyncio
from types import SimpleNamespace

from app.common.ai_status import RequestStatus
from app.services.ai_request_lifecycle_service import AiRequestLifecycleService


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        request_id=71,
        request_status=RequestStatus.READY.value,
        user_id=7,
        source_type="FORM",
        source_ref_id=None,
        parsed_query_json={},
        merged_condition_json={},
        profile_conflict_json=[],
        result_json=None,
        error_message=None,
    )


def test_mark_processing_records_offered_event() -> None:
    request = _request()
    events: list[dict[str, object]] = []

    class FakeRequestRepository:
        async def find_by_id(self, db, request_type, request_id):
            return request

        async def update_status(self, db, request, status, error_message=None):
            request.request_status = status.value
            return request

    class FakeEventRepository:
        async def record(self, db, **event):
            events.append(event)

    service = AiRequestLifecycleService(
        repository=FakeRequestRepository(),
        execution_event_repository=FakeEventRepository(),
    )

    snapshot = asyncio.run(
        service.mark_processing(
            db=SimpleNamespace(),
            request_type="recommendation",
            request_id=71,
        )
    )

    assert snapshot.status == RequestStatus.PROCESSING
    assert events == [
        {
            "request_id": 71,
            "event_type": "REQUEST_OFFERED",
            "stage": "ADMISSION",
            "execution_token": None,
            "outcome": "PROCESSING",
            "error_type": None,
            "details": None,
        }
    ]
