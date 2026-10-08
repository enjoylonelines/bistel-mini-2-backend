from typing import Any

from sqlalchemy import func, select, text, update
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.eligibility_request import EligibilityRequest
from app.db.models.recommendation_request import RecommendationRequest
from app.repositories.user_repository import UserRepository
from app.schemas.ai_contract import RequestStatus


AiRequestModel = RecommendationRequest | EligibilityRequest


class AiRequestRepository:
    REQUEST_MODELS = {
        "recommendation": RecommendationRequest,
        "eligibility": EligibilityRequest,
    }

    @staticmethod
    async def ensure_request_schema(db: AsyncSession) -> None:
        await UserRepository.ensure_user_schema(db)
        await db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS recommendation_request (
                    request_id bigserial PRIMARY KEY,
                    user_id bigint NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    source_type varchar(30) NOT NULL DEFAULT 'FORM',
                    source_ref_id varchar(100),
                    raw_query text,
                    parsed_query_json jsonb,
                    merged_condition_json jsonb,
                    profile_conflict_json jsonb,
                    result_json jsonb,
                    error_message text,
                    execution_token varchar(36),
                    execution_claimed_at timestamp,
                    request_status varchar(50) NOT NULL DEFAULT 'READY',
                    created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS eligibility_request (
                    request_id bigserial PRIMARY KEY,
                    user_id bigint NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    policy_id bigint NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
                    source_type varchar(30) NOT NULL DEFAULT 'POLICY_DETAIL',
                    source_ref_id varchar(100),
                    raw_query text,
                    parsed_query_json jsonb,
                    merged_condition_json jsonb,
                    profile_conflict_json jsonb,
                    result_json jsonb,
                    error_message text,
                    request_status varchar(50) NOT NULL DEFAULT 'READY',
                    created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        for statement in [
            "CREATE INDEX IF NOT EXISTS recommendation_request_user_id_idx ON recommendation_request (user_id)",
            "CREATE INDEX IF NOT EXISTS eligibility_request_user_id_idx ON eligibility_request (user_id)",
            "CREATE INDEX IF NOT EXISTS eligibility_request_policy_id_idx ON eligibility_request (policy_id)",
            "ALTER TABLE recommendation_request ADD COLUMN IF NOT EXISTS error_message text",
            "ALTER TABLE recommendation_request ADD COLUMN IF NOT EXISTS result_json jsonb",
            "ALTER TABLE eligibility_request ADD COLUMN IF NOT EXISTS result_json jsonb",
            "ALTER TABLE eligibility_request ADD COLUMN IF NOT EXISTS error_message text",
            "ALTER TABLE recommendation_request ADD COLUMN IF NOT EXISTS source_ref_id varchar(100)",
            "ALTER TABLE recommendation_request ADD COLUMN IF NOT EXISTS execution_token varchar(36)",
            "ALTER TABLE recommendation_request ADD COLUMN IF NOT EXISTS execution_claimed_at timestamp",
            "ALTER TABLE eligibility_request ADD COLUMN IF NOT EXISTS source_ref_id varchar(100)",
            "ALTER TABLE recommendation_request ALTER COLUMN raw_query DROP NOT NULL",
            "ALTER TABLE eligibility_request ALTER COLUMN raw_query DROP NOT NULL",
            """
            CREATE TABLE IF NOT EXISTS recommendation_execution_event (
                event_id bigserial PRIMARY KEY,
                request_id bigint NOT NULL
                    REFERENCES recommendation_request(request_id) ON DELETE CASCADE,
                execution_token varchar(36),
                event_type varchar(50) NOT NULL,
                stage varchar(50) NOT NULL,
                outcome varchar(50),
                error_type varchar(100),
                details_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                occurred_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS recommendation_execution_event_request_idx
                ON recommendation_execution_event (request_id, event_id)
            """,
        ]:
            await db.execute(text(statement))

    async def create(
        self,
        db: AsyncSession,
        request_type: str,
        user_id: int,
        source_type: str,
        source_ref_id: str | None = None,
        raw_query: str | None = None,
        selected_conditions: dict[str, Any] | None = None,
        follow_up_resolved: bool = False,
        policy_id: int | None = None,
    ) -> AiRequestModel:
        await self.ensure_request_schema(db)
        model = self._model_for(request_type)
        parsed_query_json: dict[str, Any] = {}
        if selected_conditions is not None:
            parsed_query_json["selected_conditions"] = selected_conditions
        if follow_up_resolved:
            parsed_query_json["follow_up_resolved"] = True
        if not parsed_query_json:
            parsed_query_json = None
        values: dict[str, Any] = {
            "user_id": user_id,
            "source_type": source_type,
            "source_ref_id": source_ref_id,
            "raw_query": raw_query,
            "parsed_query_json": parsed_query_json,
            "request_status": RequestStatus.READY.value,
        }
        if model is EligibilityRequest:
            if policy_id is None:
                raise ValueError("policy_id is required for eligibility request")
            values["policy_id"] = policy_id

        request = model(**values)
        db.add(request)
        await db.flush()
        await db.refresh(request)
        return request

    async def find_by_id(
        self,
        db: AsyncSession,
        request_type: str,
        request_id: int,
    ) -> AiRequestModel | None:
        model = self._model_for(request_type)
        result = await db.execute(select(model).where(model.request_id == request_id))
        return result.scalar_one_or_none()

    async def list_completed_by_user(
        self,
        db: AsyncSession,
        request_type: str,
        user_id: int,
        limit: int = 20,
    ) -> list[AiRequestModel]:
        """사용자의 완료된 요청을 최신순으로 조회한다(추천 이력용)."""
        model = self._model_for(request_type)
        result = await db.execute(
            select(model)
            .where(model.user_id == user_id)
            .where(model.request_status == RequestStatus.COMPLETED.value)
            .order_by(model.created_at.desc(), model.request_id.desc())
            .limit(max(1, min(limit, 100)))
        )
        return list(result.scalars().all())

    async def update_status(
        self,
        db: AsyncSession,
        request: AiRequestModel,
        status: RequestStatus,
        error_message: str | None = None,
        execution_token: str | None = None,
    ) -> AiRequestModel | None:
        if execution_token is not None and isinstance(request, RecommendationRequest):
            result = await db.execute(
                update(RecommendationRequest)
                .where(
                    RecommendationRequest.request_id == request.request_id,
                    RecommendationRequest.request_status == RequestStatus.PROCESSING.value,
                    RecommendationRequest.execution_token == execution_token,
                )
                .values(request_status=status.value, error_message=error_message)
                .returning(RecommendationRequest.request_id)
            )
            if result.scalar_one_or_none() is None:
                return None
            await db.refresh(request)
            return request
        request.request_status = status.value
        request.error_message = error_message
        if isinstance(request, RecommendationRequest) and status == RequestStatus.PROCESSING:
            request.execution_token = None
            request.execution_claimed_at = None
        await db.flush()
        await db.refresh(request)
        return request

    async def update_payload(
        self,
        db: AsyncSession,
        request: AiRequestModel,
        parsed_query_json: dict[str, Any] | None = None,
        merged_condition_json: dict[str, Any] | None = None,
        profile_conflict_json: list[dict[str, Any]] | None = None,
        raw_query: str | None = None,
        execution_token: str | None = None,
    ) -> AiRequestModel | None:
        if execution_token is not None and isinstance(request, RecommendationRequest):
            values: dict[str, Any] = {}
            if parsed_query_json is not None:
                values["parsed_query_json"] = parsed_query_json
            if merged_condition_json is not None:
                values["merged_condition_json"] = merged_condition_json
            if profile_conflict_json is not None:
                values["profile_conflict_json"] = profile_conflict_json
            if raw_query is not None:
                values["raw_query"] = raw_query
            if values:
                result = await db.execute(
                    update(RecommendationRequest)
                    .where(
                        RecommendationRequest.request_id == request.request_id,
                        RecommendationRequest.request_status == RequestStatus.PROCESSING.value,
                        RecommendationRequest.execution_token == execution_token,
                    )
                    .values(**values)
                    .returning(RecommendationRequest.request_id)
                )
                if result.scalar_one_or_none() is None:
                    return None
                await db.refresh(request)
            return request
        if parsed_query_json is not None:
            request.parsed_query_json = parsed_query_json
        if merged_condition_json is not None:
            request.merged_condition_json = merged_condition_json
        if profile_conflict_json is not None:
            request.profile_conflict_json = profile_conflict_json
        if raw_query is not None:
            request.raw_query = raw_query
        await db.flush()
        await db.refresh(request)
        return request

    async def update_result(
        self,
        db: AsyncSession,
        request: AiRequestModel,
        result_json: dict[str, Any],
        execution_token: str | None = None,
    ) -> AiRequestModel | None:
        if execution_token is not None and isinstance(request, RecommendationRequest):
            result = await db.execute(
                update(RecommendationRequest)
                .where(
                    RecommendationRequest.request_id == request.request_id,
                    RecommendationRequest.request_status == RequestStatus.PROCESSING.value,
                    RecommendationRequest.execution_token == execution_token,
                )
                .values(result_json=result_json)
                .returning(RecommendationRequest.request_id)
            )
            if result.scalar_one_or_none() is None:
                return None
            await db.refresh(request)
            return request
        if isinstance(request, (RecommendationRequest, EligibilityRequest)):
            request.result_json = result_json
        await db.flush()
        await db.refresh(request)
        return request

    async def claim_recommendation_execution(
        self,
        db: AsyncSession,
        request_id: int,
    ) -> str | None:
        """Atomically admit one local runner for a PROCESSING recommendation.

        This is an ownership fence, not a queue or a global capacity control.
        The caller must commit this short transaction before performing any
        external work so a competing runner never waits on a long DB lock.
        """
        token = str(uuid4())
        result = await db.execute(
            update(RecommendationRequest)
            .where(
                RecommendationRequest.request_id == request_id,
                RecommendationRequest.request_status == RequestStatus.PROCESSING.value,
                RecommendationRequest.execution_token.is_(None),
            )
            .values(execution_token=token, execution_claimed_at=func.now())
            .returning(RecommendationRequest.execution_token)
        )
        return result.scalar_one_or_none()

    def _model_for(self, request_type: str):
        try:
            return self.REQUEST_MODELS[request_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported AI request type: {request_type}") from exc
