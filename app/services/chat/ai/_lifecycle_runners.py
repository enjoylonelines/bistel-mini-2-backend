from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text

from app.ai.nodes.chat.chat_nodes import _mark_recommendation_failed
from app.ai.nodes.chat.constants import (
    ELIGIBILITY_LIFECYCLE_TIMEOUT_SECONDS as _ELIGIBILITY_LIFECYCLE_TIMEOUT_SECONDS,
    ELIGIBILITY_LOCK_TIMEOUT as _ELIGIBILITY_LOCK_TIMEOUT,
    ELIGIBILITY_SOURCE_TYPE as _ELIGIBILITY_SOURCE_TYPE,
    ELIGIBILITY_STATEMENT_TIMEOUT as _ELIGIBILITY_STATEMENT_TIMEOUT,
    ELIGIBILITY_FALLBACK_ERROR as _ELIGIBILITY_FALLBACK_ERROR,
    COMPARE_FALLBACK_ERROR as _COMPARE_FALLBACK_ERROR,
    RECOMMEND_LIFECYCLE_TIMEOUT_SECONDS as _RECOMMEND_LIFECYCLE_TIMEOUT_SECONDS,
    RECOMMEND_LOCK_TIMEOUT as _RECOMMEND_LOCK_TIMEOUT,
    RECOMMEND_SOURCE_TYPE as _RECOMMEND_SOURCE_TYPE,
    RECOMMEND_STATEMENT_TIMEOUT as _RECOMMEND_STATEMENT_TIMEOUT,
)
from app.ai.nodes.chat.result_adapters import (
    _adapt_comparison_result,
    _adapt_eligibility_result,
)
from app.ai.nodes.chat.profile_helpers import _profile_to_selected_conditions
from app.ai.states.chat_state import ChatGraphState
from app.common.ai_status import RequestStatus
from app.db.session import AsyncSessionLocal
from app.schemas.ai_request_schema import AiRequestSnapshot
from app.services.chat.ai._graph_clients import (
    get_comparison_graph,
    get_eligibility_graph,
    get_lifecycle_service,
)
from app.services.ai_request_lifecycle_service import RequestExecutionOwnershipLost

logger = logging.getLogger(__name__)


async def run_recommendation_lifecycle(
    user_id: int,
    user_content: str,
    selected_conditions: dict[str, Any] | None = None,
    follow_up_resolved: bool = False,
) -> tuple[AiRequestSnapshot | None, str | None]:
    request_id: int | None = None
    execution_token: str | None = None
    try:
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(text(f"SET LOCAL lock_timeout = '{_RECOMMEND_LOCK_TIMEOUT}'"))
                await db.execute(text(f"SET LOCAL statement_timeout = '{_RECOMMEND_STATEMENT_TIMEOUT}'"))
                lifecycle = get_lifecycle_service()
                created = await lifecycle.create_request(
                    db=db,
                    user_id=user_id,
                    request_type="recommendation",
                    source_type=_RECOMMEND_SOURCE_TYPE,
                    raw_query=user_content,
                    selected_conditions=selected_conditions,
                    follow_up_resolved=follow_up_resolved,
                )
                request_id = int(created.request_id)
                await lifecycle.mark_processing(
                    db=db,
                    request_type="recommendation",
                    request_id=request_id,
                )
                await db.commit()

                execution_token = await lifecycle.claim_recommendation_execution(
                    db=db,
                    request_id=request_id,
                )
                if execution_token is None:
                    await db.commit()
                    raise RequestExecutionOwnershipLost()
                await lifecycle.record_recommendation_execution_event(
                    db,
                    request_id=request_id,
                    event_type="EXECUTION_CLAIMED",
                    stage="ADMISSION",
                    execution_token=execution_token,
                    outcome="CLAIMED",
                )
                await db.commit()

                await db.execute(text(f"SET LOCAL lock_timeout = '{_RECOMMEND_LOCK_TIMEOUT}'"))
                await db.execute(text(f"SET LOCAL statement_timeout = '{_RECOMMEND_STATEMENT_TIMEOUT}'"))
                snapshot = await asyncio.wait_for(
                    lifecycle.process_condition_request(
                        db=db,
                        request_type="recommendation",
                        request_id=request_id,
                        execution_token=execution_token,
                    ),
                    timeout=_RECOMMEND_LIFECYCLE_TIMEOUT_SECONDS,
                )
                summary = snapshot.result_json.get("summary") or {}
                fallback_used = bool(summary.get("llm_fallback_used"))
                await lifecycle.record_recommendation_execution_event(
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
                    details={
                        "llm_fallback_used": fallback_used,
                        "provider_call_count": int(
                            summary.get("llm_provider_call_count") or 0
                        ),
                        "provider_token_usage_available": bool(
                            summary.get("llm_provider_token_usage_available")
                        ),
                    },
                )
                await db.commit()
                return snapshot, None
            except Exception:
                await db.rollback()
                raise
    except RequestExecutionOwnershipLost:
        logger.info("chat branch_recommend lost execution ownership: %s", request_id)
        return None, "temporary_failure"
    except Exception as exc:
        logger.exception("chat branch_recommend lifecycle failed")
        if request_id is not None:
            await _mark_recommendation_failed(
                request_id,
                str(exc),
                execution_token=execution_token,
            )
        return None, "temporary_failure"


async def run_eligibility_lifecycle(
    user_id: int,
    user_content: str,
    policy_slug: str,
    selected_conditions: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(text(f"SET LOCAL lock_timeout = '{_ELIGIBILITY_LOCK_TIMEOUT}'"))
                await db.execute(text(f"SET LOCAL statement_timeout = '{_ELIGIBILITY_STATEMENT_TIMEOUT}'"))
                result_json = await asyncio.wait_for(
                    get_eligibility_graph().run(
                        db=db,
                        user_id=user_id,
                        policy_identifier=policy_slug,
                        raw_query=user_content,
                        source_type=_ELIGIBILITY_SOURCE_TYPE,
                        selected_conditions=selected_conditions,
                    ),
                    timeout=_ELIGIBILITY_LIFECYCLE_TIMEOUT_SECONDS,
                )
                await db.commit()
                return result_json
            except Exception:
                await db.rollback()
                raise
    except Exception:
        logger.exception("chat branch_eligibility lifecycle failed")
        return None


async def run_eligibility_branch(
    *,
    state: ChatGraphState,
    policy_slug: str,
    policy_name: str | None,
    evidences: list[dict],
) -> ChatGraphState:
    result_json = await run_eligibility_lifecycle(
        user_id=state["user_id"],
        user_content=state["user_content"],
        policy_slug=policy_slug,
        selected_conditions=_profile_to_selected_conditions(state.get("profile")) or None,
    )
    if result_json is None:
        return {
            **state,
            "branch_content": _ELIGIBILITY_FALLBACK_ERROR,
            "branch_user_status": None,
            "branch_policies": [],
            "branch_evidences": evidences,
        }

    content, user_status, policies, result_evidences = _adapt_eligibility_result(
        result_json,
        fallback_slug=policy_slug,
        fallback_policy_name=policy_name,
    )
    result_status = result_json.get("status")
    if result_status == RequestStatus.FOLLOW_UP_REQUIRED.value:
        eligibility_slot_update: dict | None = {
            "slug": policy_slug,
            "eligibility_request_id": result_json.get("request_id"),
            "follow_up_questions": (
                result_json.get("follow_up_questions")
                or result_json.get("questions")
                or []
            ),
            "eligibility_status": RequestStatus.FOLLOW_UP_REQUIRED.value,
        }
    else:
        eligibility_slot_update = {
            "slug": policy_slug,
            "eligibility_request_id": result_json.get("request_id"),
            "follow_up_questions": [],
            "eligibility_status": result_status,
        }
    return {
        **state,
        "branch_content": content,
        "branch_user_status": user_status,
        "branch_policies": policies,
        "branch_evidences": result_evidences or evidences,
        "eligibility_slot_update": eligibility_slot_update,
        "branch_eligibility_result": {
            "status": result_status,
            "user_status": result_json.get("user_status"),
            "assessment_status": result_json.get("assessment_status"),
            "follow_up_questions": result_json.get("follow_up_questions") or [],
            "summary": result_json.get("summary"),
            "request_id": result_json.get("request_id"),
            "criteria": result_json.get("criteria") or result_json.get("criteria_results") or [],
        },
    }


async def run_comparison_branch(
    *,
    state: ChatGraphState,
    slug_a: str,
    slug_b: str,
    evidences: list[dict],
) -> ChatGraphState:
    try:
        async with AsyncSessionLocal() as db:
            try:
                result_json = await get_comparison_graph().run(
                    db,
                    slug_a=slug_a,
                    slug_b=slug_b,
                    user_id=state["user_id"],
                    raw_query=state["user_content"],
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    except Exception:
        logger.exception("chat branch_compare comparison graph failed")
        return {
            **state,
            "branch_content": _COMPARE_FALLBACK_ERROR,
            "branch_policies": [],
            "branch_evidences": evidences,
        }

    content, policies = _adapt_comparison_result(result_json)
    return {
        **state,
        "branch_content": content,
        "branch_policies": policies,
        "branch_evidences": evidences,
    }
