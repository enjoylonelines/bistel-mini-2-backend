"""Keep recommendation lifecycle outcomes distinct from rerank outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.common.ai_status import RequestStatus


@dataclass(frozen=True)
class RecommendationLifecycleOutcome:
    event_type: str
    stage: str
    outcome: str
    emit_completed_telemetry: bool


def recommendation_lifecycle_outcome(
    status: RequestStatus,
    summary: dict[str, Any],
) -> RecommendationLifecycleOutcome | None:
    """Map a durable request status to one non-overlapping lifecycle outcome.

    Rerank success/fallback is meaningful only after the durable recommendation
    request reached ``COMPLETED``. A follow-up gate is a terminal user outcome,
    not an implicit rerank success.
    """
    if status == RequestStatus.COMPLETED:
        return RecommendationLifecycleOutcome(
            event_type="EXECUTION_TERMINAL",
            stage="RERANK",
            outcome="FALLBACK" if bool(summary.get("llm_fallback_used")) else "AUGMENTED",
            emit_completed_telemetry=True,
        )
    if status == RequestStatus.FOLLOW_UP_REQUIRED:
        return RecommendationLifecycleOutcome(
            event_type="EXECUTION_FOLLOW_UP_REQUIRED",
            stage="FOLLOW_UP",
            outcome="FOLLOW_UP_REQUIRED",
            emit_completed_telemetry=False,
        )
    if status == RequestStatus.CANCELLED:
        return RecommendationLifecycleOutcome(
            event_type="EXECUTION_CANCELLED",
            stage="TERMINAL",
            outcome="CANCELLED",
            emit_completed_telemetry=False,
        )
    return None


async def record_recommendation_lifecycle_outcome(
    lifecycle: Any,
    db: Any,
    *,
    request_id: int,
    execution_token: str | None,
    status: RequestStatus,
    summary: dict[str, Any],
) -> RecommendationLifecycleOutcome | None:
    """Persist the lifecycle outcome once, and leave telemetry to the caller."""
    outcome = recommendation_lifecycle_outcome(status, summary)
    if outcome is None:
        return None
    await lifecycle.record_recommendation_execution_event(
        db,
        request_id=request_id,
        event_type=outcome.event_type,
        stage=outcome.stage,
        execution_token=execution_token,
        outcome=outcome.outcome,
        error_type=(
            str(summary.get("llm_error") or "").split(":", 1)[0] or None
            if outcome.stage == "RERANK"
            else None
        ),
        details={
            "request_status": status.value,
            "llm_fallback_used": bool(summary.get("llm_fallback_used")),
            "provider_call_count": int(summary.get("llm_provider_call_count") or 0),
            "provider_token_usage_available": bool(
                summary.get("llm_provider_token_usage_available")
            ),
        },
    )
    return outcome
