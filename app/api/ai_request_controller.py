import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text

from app.ai.graphs.eligibility_graph import eligibility_graph_runner
from app.ai.utils.progress import get_node_label, reset_progress_callback, set_progress_callback
from app.common.response import success_response
from app.core.dependencies import CurrentUserDep, DbSessionDep
from app.db.session import AsyncSessionLocal
from app.schemas.ai_request_schema import (
    AiRequestSnapshot,
    EligibilityResultResponse,
    EligibilityRequestCreate,
    RecommendationAnswerSubmit,
    RecommendationHistoryResponse,
    RecommendationPollingResponse,
    RecommendationRequestCreate,
)
from app.schemas.ai_contract import RequestStatus
from app.services.ai_request_lifecycle_service import (
    AiRequestLifecycleService,
    RequestExecutionOwnershipLost,
)
from app.services.chat.chat_service import ChatService


# AI 단계(파싱·판정·리랭크)가 충분히 생각할 수 있도록 넉넉하게 둔다.
# 각 LLM 단계 타임아웃 합(파싱 60 + 판정 90 + 근거검색 20 + 리랭크 180)보다 크게.
AI_BACKGROUND_TIMEOUT_SECONDS = 360
AI_REQUEST_USER_ERROR_MESSAGE = (
    "분석 처리 중 일시적인 문제가 발생했어요. 잠시 후 다시 시도해 주세요."
)
logger = logging.getLogger(__name__)

recommendation_router = APIRouter(
    prefix="/api/v1/recommendations",
    tags=["AI Recommendations"],
)
eligibility_router = APIRouter(
    prefix="/api/v1/eligibility",
    tags=["AI Eligibility"],
)


def _request_meta(snapshot: AiRequestSnapshot) -> dict[str, object]:
    return {
        "request_id": snapshot.request_id,
        "follow_up_required": snapshot.status.value == "FOLLOW_UP_REQUIRED",
    }


def _recommendation_polling_meta(
    response: RecommendationPollingResponse,
) -> dict[str, object]:
    return {
        "request_id": response.request_id,
        "follow_up_required": bool(response.follow_up_questions),
    }


def _eligibility_result_meta(
    response: EligibilityResultResponse,
) -> dict[str, object]:
    return {
        "request_id": response.request_id,
        "follow_up_required": bool(response.follow_up_questions),
    }


def _eligibility_source_ref(
    chat_session_id: int | None,
    source_ref_id: str | None,
) -> str | None:
    if not chat_session_id:
        return source_ref_id
    chat_ref = f"chat_session:{chat_session_id}"
    if source_ref_id:
        return f"{chat_ref};source:{source_ref_id}"
    return chat_ref


async def process_ai_condition_request(request_type: str, request_id: int) -> None:
    service = AiRequestLifecycleService()
    execution_token: str | None = None
    try:
        async with AsyncSessionLocal() as db:
            try:
                logger.info(
                    "AI background task started: request_type=%s request_id=%s",
                    request_type,
                    request_id,
                )
                await db.execute(text("SET LOCAL lock_timeout = '5s'"))
                await db.execute(text("SET LOCAL statement_timeout = '60s'"))
                if request_type == "recommendation":
                    execution_token = await service.claim_recommendation_execution(
                        db=db,
                        request_id=request_id,
                    )
                    if execution_token is None:
                        await db.commit()
                        logger.info(
                            "AI background task skipped without execution claim: request_id=%s",
                            request_id,
                        )
                        return
                    await service.record_recommendation_execution_event(
                        db,
                        request_id=request_id,
                        event_type="EXECUTION_CLAIMED",
                        stage="ADMISSION",
                        execution_token=execution_token,
                        outcome="CLAIMED",
                    )
                    # Commit the claim and its observation before any slow or
                    # external work; a competing task must not wait on this lock.
                    await db.commit()
                    # SET LOCAL is transaction-scoped, so restore the DB
                    # safeguards after committing the short ownership claim.
                    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
                    await db.execute(text("SET LOCAL statement_timeout = '60s'"))
                snapshot = await asyncio.wait_for(
                    service.process_condition_request(
                        db=db,
                        request_type=request_type,
                        request_id=request_id,
                        execution_token=execution_token,
                    ),
                    timeout=AI_BACKGROUND_TIMEOUT_SECONDS,
                )
                if request_type == "recommendation":
                    summary = snapshot.result_json.get("summary") or {}
                    fallback_used = bool(summary.get("llm_fallback_used"))
                    await service.record_recommendation_execution_event(
                        db,
                        request_id=request_id,
                        event_type="EXECUTION_TERMINAL",
                        stage="RERANK",
                        execution_token=execution_token,
                        outcome="FALLBACK" if fallback_used else "AUGMENTED",
                        error_type=(
                            str(summary.get("llm_error") or "").split(":", 1)[0]
                            or None
                        ),
                        details={"llm_fallback_used": fallback_used},
                    )
                await db.commit()
                logger.info(
                    "AI background task completed: request_type=%s request_id=%s",
                    request_type,
                    request_id,
                )
            except RequestExecutionOwnershipLost:
                await db.rollback()
                logger.info(
                    "AI background task lost execution ownership: request_type=%s request_id=%s",
                    request_type,
                    request_id,
                )
                if request_type == "recommendation":
                    await _record_recommendation_execution_event(
                        request_id=request_id,
                        execution_token=execution_token,
                        event_type="LATE_WRITE_BLOCKED",
                        stage="TERMINAL",
                        outcome="DROPPED",
                    )
                return
            except Exception:
                await db.rollback()
                raise
    except TimeoutError:
        logger.exception(
            "AI background task timed out: request_type=%s request_id=%s",
            request_type,
            request_id,
        )
        await _mark_ai_request_failed(
            request_type,
            request_id,
            AI_REQUEST_USER_ERROR_MESSAGE,
            execution_token=execution_token,
        )
    except Exception:
        logger.exception(
            "AI background task failed: request_type=%s request_id=%s",
            request_type,
            request_id,
        )
        await _mark_ai_request_failed(
            request_type,
            request_id,
            AI_REQUEST_USER_ERROR_MESSAGE,
            execution_token=execution_token,
        )


async def _mark_ai_request_failed(
    request_type: str,
    request_id: int,
    error_message: str,
    execution_token: str | None = None,
) -> None:
    async with AsyncSessionLocal() as db:
        service = AiRequestLifecycleService()
        try:
            snapshot = await service.mark_failed(
                db=db,
                request_type=request_type,
                request_id=request_id,
                error_message=error_message,
                execution_token=execution_token,
            )
            if request_type == "recommendation":
                await service.record_recommendation_execution_event(
                    db,
                    request_id=request_id,
                    event_type="EXECUTION_TERMINAL",
                    stage="REQUEST",
                    execution_token=execution_token,
                    outcome="FAILED",
                    error_type="BACKGROUND_FAILURE",
                )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception(
                "Failed to mark AI request as failed: request_type=%s request_id=%s",
                request_type,
                request_id,
            )


async def _record_recommendation_execution_event(
    *,
    request_id: int,
    execution_token: str | None,
    event_type: str,
    stage: str,
    outcome: str,
) -> None:
    """Record a post-rollback fact without reviving a cancelled request."""
    async with AsyncSessionLocal() as db:
        service = AiRequestLifecycleService()
        try:
            await service.record_recommendation_execution_event(
                db,
                request_id=request_id,
                execution_token=execution_token,
                event_type=event_type,
                stage=stage,
                outcome=outcome,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception(
                "Failed to record recommendation execution event: request_id=%s event=%s",
                request_id,
                event_type,
            )


@recommendation_router.post("/requests", status_code=status.HTTP_202_ACCEPTED)
async def create_recommendation_request(
    payload: RecommendationRequestCreate,
    background_tasks: BackgroundTasks,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> JSONResponse:
    service = AiRequestLifecycleService()
    snapshot = await service.create_request(
        db=db,
        user_id=current_user.user_id,
        request_type="recommendation",
        source_type=payload.source_type,
        source_ref_id=payload.source_ref_id,
        raw_query=payload.raw_query,
        selected_conditions=payload.selected_conditions,
    )
    snapshot = await service.mark_processing(
        db=db,
        request_type="recommendation",
        request_id=int(snapshot.request_id),
    )
    await db.commit()
    background_tasks.add_task(
        process_ai_condition_request,
        "recommendation",
        int(snapshot.request_id),
    )
    return success_response(
        data=snapshot,
        status_code=status.HTTP_202_ACCEPTED,
        meta=_request_meta(snapshot),
    )


@recommendation_router.post(
    "/requests/{request_id}/answers",
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_recommendation_answers(
    request_id: int,
    payload: RecommendationAnswerSubmit,
    background_tasks: BackgroundTasks,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> JSONResponse:
    # 추가질문 답변(또는 건너뛰기) → 조건 반영 후 추천 재실행.
    service = AiRequestLifecycleService()
    snapshot = await service.submit_recommendation_answers(
        db=db,
        request_id=request_id,
        user_id=current_user.user_id,
        answers=[answer.model_dump() for answer in payload.answers],
    )
    await db.commit()
    background_tasks.add_task(
        process_ai_condition_request,
        "recommendation",
        int(snapshot.request_id),
    )
    return success_response(
        data=snapshot,
        status_code=status.HTTP_202_ACCEPTED,
        meta=_request_meta(snapshot),
    )


@recommendation_router.get("/requests")
async def list_recommendation_history(
    db: DbSessionDep,
    current_user: CurrentUserDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    # 로그인 사용자의 완료된 추천 이력(최신순).
    service = AiRequestLifecycleService()
    response = await service.get_recommendation_history(
        db=db,
        user_id=current_user.user_id,
        limit=limit,
    )
    return success_response(data=response)


@recommendation_router.get("/requests/{request_id}")
async def get_recommendation_request(
    request_id: int,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> JSONResponse:
    service = AiRequestLifecycleService()
    response = await service.get_recommendation_polling_result(
        db=db,
        request_id=request_id,
        user_id=current_user.user_id,
    )
    return success_response(data=response, meta=_recommendation_polling_meta(response))


@recommendation_router.post("/requests/{request_id}/cancel")
async def cancel_recommendation_request(
    request_id: int,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> JSONResponse:
    service = AiRequestLifecycleService()
    snapshot = await service.cancel_recommendation_request(
        db=db,
        request_id=request_id,
        user_id=current_user.user_id,
    )
    await service.record_recommendation_execution_event(
        db,
        request_id=request_id,
        event_type="REQUEST_CANCELLED",
        stage="TERMINAL",
        outcome="CANCELLED",
    )
    await db.commit()
    return success_response(data=snapshot, meta=_request_meta(snapshot))


@eligibility_router.post("/requests", status_code=status.HTTP_202_ACCEPTED)
async def create_eligibility_request(
    payload: EligibilityRequestCreate,
    background_tasks: BackgroundTasks,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> JSONResponse:
    service = AiRequestLifecycleService()
    if payload.chat_session_id is not None:
        await ChatService.ensure_owned_session(
            db,
            user_id=current_user.user_id,
            chat_session_id=payload.chat_session_id,
        )
    snapshot = await service.create_eligibility_request(
        db=db,
        user_id=current_user.user_id,
        policy_identifier=payload.policy_id,
        source_type=payload.source_type,
        source_ref_id=_eligibility_source_ref(
            payload.chat_session_id,
            payload.source_ref_id,
        ),
        raw_query=payload.raw_query,
        selected_conditions=payload.selected_conditions,
    )
    snapshot = await service.mark_processing(
        db=db,
        request_type="eligibility",
        request_id=int(snapshot.request_id),
    )
    await db.commit()
    background_tasks.add_task(
        process_ai_condition_request,
        "eligibility",
        int(snapshot.request_id),
    )
    return success_response(
        data=snapshot,
        status_code=status.HTTP_202_ACCEPTED,
        meta=_request_meta(snapshot),
    )


@eligibility_router.get("/requests/{request_id}")
async def get_eligibility_request(
    request_id: int,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> JSONResponse:
    service = AiRequestLifecycleService()
    response = await service.get_eligibility_result(
        db=db,
        request_id=request_id,
        user_id=current_user.user_id,
    )
    if response.status not in {RequestStatus.READY, RequestStatus.PROCESSING}:
        try:
            request = await service.repository.find_by_id(
                db,
                request_type="eligibility",
                request_id=request_id,
            )
            if request is not None:
                await ChatService.persist_eligibility_result_message(
                    db,
                    user_id=current_user.user_id,
                    request=request,
                    result_json=response.model_dump(mode="json"),
                )
                await db.commit()
        except Exception:
            if hasattr(db, "rollback"):
                await db.rollback()
            logger.exception(
                "Failed to persist eligibility result to chat session: request_id=%s",
                request_id,
            )
    return success_response(data=response, meta=_eligibility_result_meta(response))


# ---------------------------------------------------------------------------
# SSE 공통 헬퍼
# ---------------------------------------------------------------------------

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# 추천 SSE 엔드포인트
# ---------------------------------------------------------------------------

@recommendation_router.post("/requests/stream")
async def stream_recommendation_request(
    payload: RecommendationRequestCreate,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> StreamingResponse:
    return StreamingResponse(
        _recommendation_sse_stream(db, current_user.user_id, payload),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def _recommendation_sse_stream(
    db: Any,
    user_id: int,
    payload: RecommendationRequestCreate,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def _on_progress(flow: str, node: str, status_: str, step: int, total: int) -> None:
        await queue.put({
            "type": "progress",
            "flow": flow,
            "node": node,
            "status": status_,
            "step": step,
            "total_steps": total,
            "label": get_node_label(flow, node, status_),
        })

    async def _run() -> None:
        service = AiRequestLifecycleService()
        progress_token = set_progress_callback(_on_progress)
        try:
            async with AsyncSessionLocal() as inner_db:
                try:
                    snapshot = await service.create_request(
                        db=inner_db,
                        user_id=user_id,
                        request_type="recommendation",
                        source_type=payload.source_type,
                        source_ref_id=payload.source_ref_id,
                        raw_query=payload.raw_query,
                        selected_conditions=payload.selected_conditions,
                    )
                    snapshot = await service.mark_processing(
                        db=inner_db,
                        request_type="recommendation",
                        request_id=int(snapshot.request_id),
                    )
                    await inner_db.commit()
                    execution_token = await service.claim_recommendation_execution(
                        db=inner_db,
                        request_id=int(snapshot.request_id),
                    )
                    if execution_token is None:
                        await inner_db.commit()
                        raise RequestExecutionOwnershipLost()
                    await service.record_recommendation_execution_event(
                        inner_db,
                        request_id=int(snapshot.request_id),
                        event_type="EXECUTION_CLAIMED",
                        stage="ADMISSION",
                        execution_token=execution_token,
                        outcome="CLAIMED",
                    )
                    await inner_db.commit()
                    completed = await asyncio.wait_for(
                        service.process_condition_request(
                            db=inner_db,
                            request_type="recommendation",
                            request_id=int(snapshot.request_id),
                            execution_token=execution_token,
                        ),
                        timeout=AI_BACKGROUND_TIMEOUT_SECONDS,
                    )
                    summary = completed.result_json.get("summary") or {}
                    fallback_used = bool(summary.get("llm_fallback_used"))
                    await service.record_recommendation_execution_event(
                        inner_db,
                        request_id=int(snapshot.request_id),
                        event_type="EXECUTION_TERMINAL",
                        stage="RERANK",
                        execution_token=execution_token,
                        outcome="FALLBACK" if fallback_used else "AUGMENTED",
                        error_type=(
                            str(summary.get("llm_error") or "").split(":", 1)[0]
                            or None
                        ),
                        details={"llm_fallback_used": fallback_used},
                    )
                    await inner_db.commit()
                    result = await service.get_recommendation_polling_result(
                        db=inner_db,
                        request_id=int(snapshot.request_id),
                        user_id=user_id,
                    )
                    await queue.put({"type": "done", "payload": result.model_dump(mode="json")})
                except RequestExecutionOwnershipLost:
                    await inner_db.rollback()
                    await queue.put({"type": "error", "message": "요청이 취소되었어요."})
                except Exception as exc:
                    await inner_db.rollback()
                    logger.exception("Recommendation SSE stream failed")
                    await queue.put({"type": "error", "message": str(exc)})
        finally:
            reset_progress_callback(progress_token)
            await queue.put(None)

    task = asyncio.create_task(_run())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield _sse_event(item)
        await task
    except Exception:
        task.cancel()
        yield _sse_event({"type": "error", "message": "스트리밍 오류가 발생했어요."})


# ---------------------------------------------------------------------------
# 지원가능성 SSE 엔드포인트
# ---------------------------------------------------------------------------

@eligibility_router.post("/requests/stream")
async def stream_eligibility_request(
    payload: EligibilityRequestCreate,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> StreamingResponse:
    if payload.chat_session_id is not None:
        await ChatService.ensure_owned_session(
            db,
            user_id=current_user.user_id,
            chat_session_id=payload.chat_session_id,
        )
    return StreamingResponse(
        _eligibility_sse_stream(db, current_user.user_id, payload),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def _eligibility_sse_stream(
    db: Any,
    user_id: int,
    payload: EligibilityRequestCreate,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def _on_progress(flow: str, node: str, status_: str, step: int, total: int) -> None:
        await queue.put({
            "type": "progress",
            "flow": flow,
            "node": node,
            "status": status_,
            "step": step,
            "total_steps": total,
            "label": get_node_label(flow, node, status_),
        })

    async def _run() -> None:
        progress_token = set_progress_callback(_on_progress)
        try:
            async with AsyncSessionLocal() as inner_db:
                try:
                    result = await asyncio.wait_for(
                        eligibility_graph_runner.run(
                            db=inner_db,
                            user_id=user_id,
                            policy_identifier=payload.policy_id,
                            raw_query=payload.raw_query,
                            selected_conditions=payload.selected_conditions,
                            source_type=payload.source_type,
                            source_ref_id=_eligibility_source_ref(
                                payload.chat_session_id,
                                payload.source_ref_id,
                            ),
                        ),
                        timeout=AI_BACKGROUND_TIMEOUT_SECONDS,
                    )
                    await inner_db.commit()
                    await queue.put({"type": "done", "payload": result})
                except Exception as exc:
                    await inner_db.rollback()
                    logger.exception("Eligibility SSE stream failed")
                    await queue.put({"type": "error", "message": str(exc)})
        finally:
            reset_progress_callback(progress_token)
            await queue.put(None)

    task = asyncio.create_task(_run())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield _sse_event(item)
        await task
    except Exception:
        task.cancel()
        yield _sse_event({"type": "error", "message": "스트리밍 오류가 발생했어요."})
