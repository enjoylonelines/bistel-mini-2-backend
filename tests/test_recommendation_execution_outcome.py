import asyncio

from app.common.ai_status import RequestStatus
from app.services.recommendation_execution_outcome import (
    record_recommendation_lifecycle_outcome,
    recommendation_lifecycle_outcome,
)


def test_recommendation_lifecycle_outcomes_do_not_overlap() -> None:
    augmented = recommendation_lifecycle_outcome(RequestStatus.COMPLETED, {})
    fallback = recommendation_lifecycle_outcome(
        RequestStatus.COMPLETED, {"llm_fallback_used": True}
    )
    cancelled = recommendation_lifecycle_outcome(RequestStatus.CANCELLED, {})
    follow_up = recommendation_lifecycle_outcome(
        RequestStatus.FOLLOW_UP_REQUIRED, {}
    )

    assert augmented is not None
    assert fallback is not None
    assert cancelled is not None
    assert follow_up is not None
    assert (augmented.event_type, augmented.stage, augmented.outcome) == (
        "EXECUTION_TERMINAL",
        "RERANK",
        "AUGMENTED",
    )
    assert (fallback.event_type, fallback.stage, fallback.outcome) == (
        "EXECUTION_TERMINAL",
        "RERANK",
        "FALLBACK",
    )
    assert (cancelled.event_type, cancelled.stage, cancelled.outcome) == (
        "EXECUTION_CANCELLED",
        "TERMINAL",
        "CANCELLED",
    )
    assert (follow_up.event_type, follow_up.stage, follow_up.outcome) == (
        "EXECUTION_FOLLOW_UP_REQUIRED",
        "FOLLOW_UP",
        "FOLLOW_UP_REQUIRED",
    )
    assert augmented.emit_completed_telemetry is True
    assert fallback.emit_completed_telemetry is True
    assert cancelled.emit_completed_telemetry is False
    assert follow_up.emit_completed_telemetry is False


def test_follow_up_records_no_rerank_terminal_or_completed_telemetry() -> None:
    events: list[dict[str, object]] = []

    class FakeLifecycle:
        async def record_recommendation_execution_event(self, _db, **event):
            events.append(event)

    outcome = asyncio.run(
        record_recommendation_lifecycle_outcome(
            FakeLifecycle(),
            object(),
            request_id=17,
            execution_token="owner",
            status=RequestStatus.FOLLOW_UP_REQUIRED,
            summary={"llm_provider_call_count": 0},
        )
    )

    assert outcome is not None
    assert outcome.emit_completed_telemetry is False
    assert events == [
        {
            "request_id": 17,
            "event_type": "EXECUTION_FOLLOW_UP_REQUIRED",
            "stage": "FOLLOW_UP",
            "execution_token": "owner",
            "outcome": "FOLLOW_UP_REQUIRED",
            "error_type": None,
            "details": {
                "request_status": "FOLLOW_UP_REQUIRED",
                "llm_fallback_used": False,
                "provider_call_count": 0,
                "provider_token_usage_available": False,
            },
        }
    ]
