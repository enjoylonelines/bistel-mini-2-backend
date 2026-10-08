import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class RecommendationExecutionEventRepository:
    """Append-only execution observations for recommendation requests."""

    async def record(
        self,
        db: AsyncSession,
        *,
        request_id: int,
        event_type: str,
        stage: str,
        execution_token: str | None = None,
        outcome: str | None = None,
        error_type: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await db.execute(
            text(
                """
                INSERT INTO recommendation_execution_event (
                    request_id, execution_token, event_type, stage, outcome,
                    error_type, details_json
                ) VALUES (
                    :request_id, :execution_token, :event_type, :stage, :outcome,
                    :error_type, CAST(:details_json AS jsonb)
                )
                """
            ),
            {
                "request_id": request_id,
                "execution_token": execution_token,
                "event_type": event_type,
                "stage": stage,
                "outcome": outcome,
                "error_type": error_type,
                "details_json": json.dumps(details or {}, ensure_ascii=False),
            },
        )
