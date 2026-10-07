from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.ai.states.chat_state import (
    ChatGraphState,
    ChatSlot,
    HistoryMessage,
    Intent,
    PendingState,
    ProfileSlot,
    RecentAssistantPolicy,
    SlotPolicy,
)
from app.ai.nodes.chat.slots import (
    CONFIRM_OPTIONS as _CONFIRM_OPTIONS,
    RECOMMEND_WIZARD_FIELDS,
    SLOT_LABELS as _SLOT_LABELS,
    SLOT_OPTIONS as _SLOT_OPTIONS,
)
from app.ai.nodes.chat.profile_helpers import (
    _build_slot_question,
    _filled_slots,
    _format_profile_context,
    _interpret_confirm,
    _merge_profile,
    _missing_required,
    _profile_summary_from_snapshot,
    _profile_to_selected_conditions,
)
from app.ai.nodes.chat.prompts import (
    APPLICATION_PERIOD_CONTEXT_RULES as _APPLICATION_PERIOD_CONTEXT_RULES,
    ASSERTIVE_PHRASES as _ASSERTIVE_PHRASES,
    BRANCH_SYSTEM_PROMPTS as _BRANCH_SYSTEM_PROMPTS,
    CLARIFICATION_PROMPTS as _CLARIFICATION_PROMPTS,
    COMMON_SAFETY_RULES as _COMMON_SAFETY_RULES,
)
from app.ai.nodes.chat.constants import (
    APPLY_CHECKLIST_PREVIEW as _APPLY_CHECKLIST_PREVIEW,
    APPLY_CLARIFICATION_FALLBACK as _APPLY_CLARIFICATION_FALLBACK,
    APPLY_LIFECYCLE_TIMEOUT_SECONDS as _APPLY_LIFECYCLE_TIMEOUT_SECONDS,
    APPLY_LOCK_TIMEOUT as _APPLY_LOCK_TIMEOUT,
    APPLY_MAX_RETRIES as _APPLY_MAX_RETRIES,
    APPLY_STATEMENT_TIMEOUT as _APPLY_STATEMENT_TIMEOUT,
    APPLY_TEMPORARY_FAILURE_FALLBACK as _APPLY_TEMPORARY_FAILURE_FALLBACK,
    COMPARE_CLARIFICATION_FALLBACK as _COMPARE_CLARIFICATION_FALLBACK,
    COMPARE_FALLBACK_ERROR as _COMPARE_FALLBACK_ERROR,
    ELIGIBILITY_CLARIFICATION_FALLBACK as _ELIGIBILITY_CLARIFICATION_FALLBACK,
    ELIGIBILITY_LIFECYCLE_TIMEOUT_SECONDS as _ELIGIBILITY_LIFECYCLE_TIMEOUT_SECONDS,
    ELIGIBILITY_LOCK_TIMEOUT as _ELIGIBILITY_LOCK_TIMEOUT,
    ELIGIBILITY_SOURCE_TYPE as _ELIGIBILITY_SOURCE_TYPE,
    ELIGIBILITY_STATEMENT_TIMEOUT as _ELIGIBILITY_STATEMENT_TIMEOUT,
    INTENT_TO_ACTION_TYPE as _INTENT_TO_ACTION_TYPE,
    INTENT_TO_API_ACTION as _INTENT_TO_API_ACTION,
    LLM_MODEL as _LLM_MODEL,
    RAG_TOP_K as _RAG_TOP_K,
    RECOMMEND_FALLBACK_ERROR as _RECOMMEND_FALLBACK_ERROR,
    RECOMMEND_FALLBACK_FOLLOW_UP as _RECOMMEND_FALLBACK_FOLLOW_UP,
    RECOMMEND_FOLLOW_UP_LIMIT_REACHED as _RECOMMEND_FOLLOW_UP_LIMIT_REACHED,
    RECOMMEND_LIFECYCLE_TIMEOUT_SECONDS as _RECOMMEND_LIFECYCLE_TIMEOUT_SECONDS,
    RECOMMEND_LOCK_TIMEOUT as _RECOMMEND_LOCK_TIMEOUT,
    RECOMMEND_MAX_RETRIES as _RECOMMEND_MAX_RETRIES,
    RECOMMEND_SOURCE_TYPE as _RECOMMEND_SOURCE_TYPE,
    RECOMMEND_STATEMENT_TIMEOUT as _RECOMMEND_STATEMENT_TIMEOUT,
    SNIPPET_LIMIT as _SNIPPET_LIMIT,
)
from app.ai.nodes.chat.result_adapters import (
    _adapt_comparison_result,
    _adapt_eligibility_result,
    _adapt_policy_summary_result,
    _adapt_recommendation_result,
    _evidence_chunk_to_chat_evidence,
    _policy_summary_fallback_content,
)
from app.common.exceptions import AppException, ErrorCode
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.repositories.family_profile_repository import FamilyProfileRepository
from app.repositories.policy_repository import PolicyRepository
from app.schemas.ai_request_schema import AiRequestSnapshot
from app.schemas.apply_schema import ApplyPreparationResponse
from app.services.apply_preparation_service import ApplyPreparationService
from app.services.policy_rag_service import PolicyRagService

if TYPE_CHECKING:
    from app.ai.graphs.comparison_graph import ComparisonGraphRunner
    from app.ai.graphs.eligibility_graph import EligibilityGraphRunner
    from app.ai.graphs.policy_summary_graph import PolicySummaryGraphRunner
    from app.services.ai_request_lifecycle_service import AiRequestLifecycleService


def _lifecycle_service_class() -> type["AiRequestLifecycleService"]:
    from app.services.ai_request_lifecycle_service import AiRequestLifecycleService

    return AiRequestLifecycleService


logger = logging.getLogger(__name__)


def _recommend_follow_up_already_asked(slot: ChatSlot | None) -> bool:
    pending = (slot or {}).get("pending") or {}
    return pending.get("intent") == "recommend" and pending.get("kind") == "slot"


async def _load_db_profile_summary(user_id: int) -> list[str] | None:
    """저장된 회원 프로필을 사람이 읽는 요약 리스트로. 의미있는 값이 없으면 None."""
    try:
        async with AsyncSessionLocal() as db:
            profile = await FamilyProfileRepository.find_profile_by_user_id(db, user_id)
    except Exception:
        logger.exception("failed to load user profile for confirm prompt")
        return None
    if profile is None:
        return None
    snap = dict(profile.profile_json or {})
    return _profile_summary_from_snapshot(
        snap,
        income_bracket=profile.income_bracket,
        region_code=profile.region_code,
        household_type=profile.household_type,
        pregnancy_status=getattr(profile, "pregnancy_status", False),
    )


def _detect_assertive_phrases(content: str) -> list[str]:
    return [phrase for phrase in _ASSERTIVE_PHRASES if phrase in content]


class _ExtractedProfile(BaseModel):
    stage: str | None = None
    child_age: str | None = None
    income: str | None = None
    region: str | None = None
    special: list[str] | None = None


class _IntentDecision(BaseModel):
    intent: Intent = Field(description="사용자 메시지의 의도 분류")
    secondary_intents: list[Intent] = Field(
        default_factory=list,
        description=(
            "복합 의도 시 primary intent 외 추가 의도. 최대 2개. "
            "예: '자격 확인하고 신청 방법도 알려줘' → [apply]. 빈 배열이 기본."
        ),
    )
    resolved_policy_slug: str | None = Field(
        default=None,
        description="사용자가 직전 거론 정책을 지시어로 가리키는 경우 그 정책의 slug. 그렇지 않으면 null.",
    )
    similar_policy_requested: bool = Field(
        default=False,
        description=(
            "사용자가 특정 정책과 '비슷한/유사한/대체' 정책을 소개해 달라고 "
            "명시적으로 요청하면 true. 그 외에는 false."
        ),
    )
    extracted_profile: _ExtractedProfile | None = Field(
        default=None,
        description="이번 메시지에서 새로 드러난 사용자 조건. 없으면 null.",
    )
    is_context_dependent: bool = Field(
        default=False,
        description=(
            "'이 정책', '그거', '방금 거', '해당 정책' 같은 지시어를 사용하거나 "
            "직전 정책 맥락의 후속 질문이면 true."
        ),
    )
    confidence: float = Field(
        default=0.8,
        description=(
            "의도 분류 확신도 0.0~1.0. 명확하면 0.9 이상, "
            "약간 모호하면 0.6~0.8, 매우 모호하면 0.5 미만."
        ),
    )
    ambiguity_reason: str | None = Field(
        default=None,
        description="confidence < 0.7이거나 모호한 경우 한 문장 이유. 명확하면 null.",
    )


BRANCH_LLM_TAG = "chat_branch_llm"
_BRANCH_LLM_CONFIG = {"tags": [BRANCH_LLM_TAG]}
_BRANCH_TOKEN_CALLBACK: ContextVar[
    Callable[[str], Awaitable[None]] | None
] = ContextVar("chat_branch_token_callback", default=None)


def set_branch_token_callback(
    callback: Callable[[str], Awaitable[None]] | None,
):
    return _BRANCH_TOKEN_CALLBACK.set(callback)


def reset_branch_token_callback(token) -> None:
    _BRANCH_TOKEN_CALLBACK.reset(token)


def _llm() -> ChatOpenAI:
    kwargs: dict = {"model": _LLM_MODEL, "temperature": 0.2}
    if settings.openai_api_key:
        kwargs["api_key"] = settings.openai_api_key
    return ChatOpenAI(**kwargs)


async def _mark_recommendation_failed(
    request_id: int,
    error_message: str,
    execution_token: str | None = None,
) -> None:
    async with AsyncSessionLocal() as db:
        try:
            await _lifecycle_service_class()().mark_failed(
                db=db,
                request_type="recommendation",
                request_id=request_id,
                error_message=error_message,
                execution_token=execution_token,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception(
                "failed to mark recommendation request as failed: %s", request_id
            )


def _pick_compare_targets(
    policies: list[dict[str, Any]],
    *,
    slot: ChatSlot | None,
    user_content: str,
) -> tuple[tuple[str, str | None] | None, tuple[str, str | None] | None]:
    candidates: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def append(slug: Any, policy_name: Any = None) -> None:
        if not slug:
            return
        key = str(slug)
        if key in seen:
            return
        seen.add(key)
        candidates.append((key, str(policy_name) if policy_name else None))

    mentioned_slot_policies: list[SlotPolicy] = []
    for policy in (slot or {}).get("recent_policies") or []:
        if _user_mentions_policy_name(user_content, policy.get("policy_name")):
            mentioned_slot_policies.append(policy)  # type: ignore[arg-type]
    for policy in mentioned_slot_policies:
        append(policy.get("slug"), policy.get("policy_name"))

    if not candidates and _is_context_dependent_compare_question(user_content):
        for policy in (slot or {}).get("recent_policies") or []:
            append(policy.get("slug"), policy.get("policy_name"))
            if len(candidates) >= 2:
                break

    for policy in policies:
        if not _user_mentions_policy_name(user_content, policy.get("policy_name")):
            continue
        append(policy.get("slug"), policy.get("policy_name"))
        if len(candidates) >= 2:
            break

    if len(candidates) < 2:
        return None, None
    return candidates[0], candidates[1]


def _attach_recommendation_context(
    policies: list[dict[str, Any]],
    *,
    request_id: str,
    selected_conditions: dict[str, Any],
    merged_condition_json: dict[str, Any],
) -> list[dict[str, Any]]:
    if not policies:
        return policies
    return [
        {
            **policy,
            "recommendation_request_id": request_id,
            "source_ref_id": request_id,
            "selected_conditions": selected_conditions,
            "merged_condition_json": merged_condition_json,
        }
        for policy in policies
    ]


def _pick_apply_target(
    policies: list[dict[str, Any]],
    *,
    user_content: str | None = None,
    require_policy_name_mention: bool = False,
) -> tuple[str | None, str | None]:
    for policy in policies:
        slug = policy.get("slug")
        if not slug:
            continue
        policy_name = policy.get("policy_name") or None
        if require_policy_name_mention and not _user_mentions_policy_name(
            user_content or "", policy_name
        ):
            continue
        return str(slug), policy_name
    return None, None


def _normalize_policy_mention_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", value).lower()


_POLICY_NAME_GENERIC_TOKENS = {
    "지원",
    "사업",
    "서비스",
    "정책",
    "제도",
    "안내",
    "대상",
    "신청",
    "급여",
}
_POLICY_NAME_GENERIC_SUFFIXES = (
    "지원사업",
    "지원서비스",
    "지원제도",
    "지원",
    "사업",
    "서비스",
    "제도",
)


def _policy_mention_terms(policy_name: str | None) -> set[str]:
    normalized_name = _normalize_policy_mention_text(policy_name)
    if not normalized_name:
        return set()

    terms: set[str] = {normalized_name}
    for suffix in _POLICY_NAME_GENERIC_SUFFIXES:
        normalized_suffix = _normalize_policy_mention_text(suffix)
        if normalized_name.endswith(normalized_suffix):
            trimmed = normalized_name[: -len(normalized_suffix)]
            if len(trimmed) >= 4:
                terms.add(trimmed)

    tokens = [
        _normalize_policy_mention_text(token)
        for token in re.findall(r"[0-9A-Za-z가-힣]+", policy_name or "")
    ]
    tokens = [
        token
        for token in tokens
        if token and token not in _POLICY_NAME_GENERIC_TOKENS
    ]
    for token in tokens:
        if len(token) >= 5:
            terms.add(token)

    for start in range(len(tokens)):
        combined = ""
        for token in tokens[start : start + 4]:
            combined += token
            if len(combined) >= 5:
                terms.add(combined)

    return terms


def _user_mentions_policy_name(user_content: str, policy_name: str | None) -> bool:
    mention_terms = _policy_mention_terms(policy_name)
    if not mention_terms:
        return False
    normalized_user_content = _normalize_policy_mention_text(user_content)
    if len(normalized_user_content) < 2:
        return False
    return any(term in normalized_user_content for term in mention_terms)


_CONTEXT_DEPENDENT_APPLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(이거|그거|그 정책|방금|위 정책|앞(?:에서)? 말한|아까|해당 정책)"),
    re.compile(r"(신청|서류|준비|기간|어디서|어떻게|방법|절차|문의).*[?？]?$"),
)

_CONTEXT_DEPENDENT_COMPARE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(둘|두 정책|두개|2개|서로|비교|차이|뭐가 더|어느 쪽|어떤 게)"),
)


def _is_context_dependent_apply_question(user_content: str) -> bool:
    content = user_content.strip()
    if not content:
        return False
    return any(pattern.search(content) for pattern in _CONTEXT_DEPENDENT_APPLY_PATTERNS)


def _is_context_dependent_compare_question(user_content: str) -> bool:
    content = user_content.strip()
    if not content:
        return False
    return any(
        pattern.search(content) for pattern in _CONTEXT_DEPENDENT_COMPARE_PATTERNS
    )


def _summary_mode(user_content: str | None) -> str:
    content = user_content or ""
    if any(token in content for token in ("내 상황", "나 기준", "맞춤", "개인")):
        return "personalized"
    if any(token in content for token in ("체크", "준비", "할 일")):
        return "checklist"
    if any(token in content for token in ("짧게", "간단", "한줄", "핵심만")):
        return "short"
    return "plain"


def _summary_target_type(
    user_content: str | None,
    slot: ChatSlot | None,
    resolved_slug: str | None = None,
) -> str | None:
    content = user_content or ""
    if resolved_slug:
        return "policy"
    if any(token in content for token in ("추천", "방금 추천", "추천한")):
        return "recommendation_result"
    if any(token in content for token in ("지원가능성", "지원 가능성", "가능성 분석", "자격")):
        return "eligibility_result"
    if any(token in content for token in ("신청", "서류", "준비")):
        return "application_guide"
    if any(token in content for token in ("비교", "차이")):
        return "comparison_result"
    if any(token in content for token in ("이 정책", "그 정책", "정책 요약", "정책")):
        return "policy"
    recent = (slot or {}).get("recent_policies") or []
    if len(recent) == 1:
        return "policy"
    return None


def _summary_target_id(
    slot: ChatSlot | None,
    resolved_slug: str | None = None,
) -> str | None:
    if resolved_slug:
        return resolved_slug
    recent = (slot or {}).get("recent_policies") or []
    if len(recent) == 1 and recent[0].get("slug"):
        return str(recent[0]["slug"])
    return None


def _recent_assistant_policy_target(
    policy: RecentAssistantPolicy | None,
) -> tuple[str | None, str | None]:
    if not policy:
        return None, None
    slug = policy.get("slug")
    if not slug:
        return None, None
    return str(slug), policy.get("policy_name") or None


def _build_apply_card(
    apply_response: ApplyPreparationResponse, policy_name: str | None
) -> dict[str, Any]:
    checklist = [
        {"id": item.id, "label": item.label, "done": item.done}
        for item in apply_response.checklist[:_APPLY_CHECKLIST_PREVIEW]
    ]
    return {
        "policy_id": apply_response.policy_id,
        "policy_name": policy_name or apply_response.policy_id,
        "how_to_apply": apply_response.how_to_apply,
        "contact": apply_response.contact,
        "official_url": apply_response.official_url,
        "checklist": checklist,
        "caution": apply_response.caution,
    }


def _format_apply_card_context(apply_card: dict[str, Any]) -> str:
    parts: list[str] = []
    name = apply_card.get("policy_name")
    if name:
        parts.append(f"- 정책명: {name}")
    if apply_card.get("how_to_apply"):
        parts.append(f"- 신청 방법: {apply_card['how_to_apply']}")
    if apply_card.get("contact"):
        parts.append(f"- 문의처: {apply_card['contact']}")
    if apply_card.get("official_url"):
        parts.append(f"- 공식 안내: {apply_card['official_url']}")
    checklist = apply_card.get("checklist") or []
    if checklist:
        items = "; ".join(item["label"] for item in checklist if item.get("label"))
        if items:
            parts.append(f"- 체크리스트: {items}")
    if apply_card.get("caution"):
        parts.append(f"- 주의사항: {apply_card['caution']}")
    return "\n".join(parts)


def _format_application_period_context(context: dict[str, Any] | None) -> str:
    if not context:
        return ""

    parts: list[str] = []
    field_labels = {
        "application_status": "신청 상태",
        "application_period_text": "신청 기간 텍스트",
        "application_start_date": "신청 시작일",
        "application_end_date": "신청 종료일",
        "deadline": "마감일",
        "source_text": "조건/원문 source_text",
        "source_fields": "source_fields",
    }
    for field, label in field_labels.items():
        value = context.get(field)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value if item)
        parts.append(f"- {label}: {value}")

    return "\n".join(parts)


def _format_slot_context(slot: ChatSlot | None) -> str:
    if not slot:
        return "(없음)"
    recent = slot.get("recent_policies") or []
    if not recent:
        return "(없음)"
    return "\n".join(
        f"- {p.get('policy_name') or '(이름 없음)'} "
        f"(slug={p.get('slug')}, action={p.get('last_action')})"
        for p in recent
    )


def _find_slot_policy_by_slug(
    slot: ChatSlot | None, slug: str
) -> SlotPolicy | None:
    if not slot or not slug:
        return None
    for p in slot.get("recent_policies") or []:
        if p.get("slug") == slug:
            return p  # type: ignore[return-value]
    return None


def _history_to_lc_messages(history: list[HistoryMessage]) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for entry in history:
        content = entry.get("content")
        if not content:
            continue
        if entry.get("role") == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages


async def _generate_branch_answer(
    intent: Intent,
    state: ChatGraphState,
    evidences: list[dict],
) -> str:
    system = _BRANCH_SYSTEM_PROMPTS[intent]
    if evidences and intent != "unclear":
        context = "\n\n".join(
            f"[{evidence.get('source_title') or '정책'}] {evidence.get('snippet', '')}"
            for evidence in evidences
        )
        system = f"{system}\n\n참고 자료:\n{context}"

    messages: list[BaseMessage] = [SystemMessage(content=system)]
    messages.extend(_history_to_lc_messages(state["history"]))
    messages.append(HumanMessage(content=state["user_content"]))

    try:
        token_callback = _BRANCH_TOKEN_CALLBACK.get()
        if token_callback is not None:
            parts: list[str] = []
            async for chunk in _llm().astream(messages, config=_BRANCH_LLM_CONFIG):
                content = chunk.content
                if isinstance(content, str):
                    delta = content
                elif isinstance(content, list):
                    delta_parts: list[str] = []
                    for item in content:
                        if isinstance(item, str):
                            delta_parts.append(item)
                        elif isinstance(item, dict) and isinstance(item.get("text"), str):
                            delta_parts.append(item["text"])
                    delta = "".join(delta_parts)
                else:
                    delta = ""
                if delta:
                    parts.append(delta)
                    await token_callback(delta)
            return "".join(parts)
        response = await _llm().ainvoke(messages, config=_BRANCH_LLM_CONFIG)
        content = response.content
        return content if isinstance(content, str) else str(content)
    except Exception:
        logger.exception("Branch answer generation failed; using fallback")
        return "죄송합니다. 답변을 생성하는 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요."


async def _generate_clarification_answer(intent: str, state: ChatGraphState) -> str:
    system = _CLARIFICATION_PROMPTS.get(intent, "")
    if not system:
        return "조금 더 구체적으로 말씀해 주시겠어요?"
    messages: list[BaseMessage] = [SystemMessage(content=system)]
    messages.extend(_history_to_lc_messages(state["history"]))
    messages.append(HumanMessage(content=state["user_content"]))
    try:
        response = await _llm().ainvoke(messages, config=_BRANCH_LLM_CONFIG)
        content = response.content
        return content if isinstance(content, str) else str(content)
    except Exception:
        logger.exception("Clarification answer generation failed")
        return "조금 더 구체적으로 말씀해 주시겠어요?"


async def _generate_apply_answer(
    state: ChatGraphState,
    evidences: list[dict],
    apply_card: dict,
    application_period_context: dict[str, Any] | None = None,
) -> str:
    system = _BRANCH_SYSTEM_PROMPTS["apply"]
    apply_context = _format_apply_card_context(apply_card)
    if apply_context:
        system = f"{system}\n\n신청 정보:\n{apply_context}"
    period_context = _format_application_period_context(application_period_context)
    if period_context:
        system = (
            f"{system}\n\n{_APPLICATION_PERIOD_CONTEXT_RULES}"
            f"\n\n신청 기간 내부 참고 정보(사용자 카드에는 표시하지 않음):\n"
            f"{period_context}"
        )
    if evidences:
        rag_context = "\n\n".join(
            f"[{evidence.get('source_title') or '정책'}] {evidence.get('snippet', '')}"
            for evidence in evidences
        )
        system = f"{system}\n\n참고 자료:\n{rag_context}"

    messages: list[BaseMessage] = [SystemMessage(content=system)]
    messages.extend(_history_to_lc_messages(state["history"]))
    messages.append(HumanMessage(content=state["user_content"]))
    try:
        response = await _llm().ainvoke(messages, config=_BRANCH_LLM_CONFIG)
        content = response.content
        return content if isinstance(content, str) else str(content)
    except Exception:
        logger.exception("Apply branch answer generation failed; using fallback")
        return "죄송합니다. 답변을 생성하는 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요."


async def _load_policy_detail(policy_slug: str) -> dict[str, Any] | None:
    try:
        async with AsyncSessionLocal() as db:
            return await PolicyRepository.find_policy_detail(
                db,
                policy_slug=policy_slug,
            )
    except Exception:
        logger.exception("chat policy summary detail lookup failed")
        return None


async def _run_apply_preparation(
    user_id: int,
    policy_slug: str,
) -> tuple[ApplyPreparationResponse | None, str | None]:
    try:
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(
                    text(f"SET LOCAL lock_timeout = '{_APPLY_LOCK_TIMEOUT}'")
                )
                await db.execute(
                    text(
                        f"SET LOCAL statement_timeout = "
                        f"'{_APPLY_STATEMENT_TIMEOUT}'"
                    )
                )
                response = await asyncio.wait_for(
                    ApplyPreparationService.get(
                        db=db,
                        user_id=user_id,
                        policy_slug=policy_slug,
                    ),
                    timeout=_APPLY_LIFECYCLE_TIMEOUT_SECONDS,
                )
                await db.commit()
                return response, None
            except Exception:
                await db.rollback()
                raise
    except AppException as exc:
        if exc.code != ErrorCode.POLICY_NOT_FOUND:
            raise
        logger.info("chat branch_apply preview skipped: %s", exc.message)
        return None, "policy_not_found"
    except Exception:
        logger.exception("chat branch_apply preview failed")
        return None, "temporary_failure"


async def _load_application_period_context(
    policy_slug: str,
) -> dict[str, Any] | None:
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text(
                    """
                    SELECT
                        p.application_status,
                        p.application_start_date,
                        p.application_end_date,
                        p.application_end_date AS deadline,
                        pd.application_period_text,
                        cp.source_text,
                        cp.source_fields
                    FROM policy p
                    LEFT JOIN policy_detail pd ON pd.policy_id = p.policy_id
                    LEFT JOIN policy_condition_profile cp
                        ON cp.policy_id = p.policy_id
                    WHERE p.policy_code = :policy_slug
                      AND p.is_active = TRUE
                    """
                ),
                {"policy_slug": policy_slug},
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return {
                "application_status": row.get("application_status"),
                "application_start_date": row.get("application_start_date"),
                "application_end_date": row.get("application_end_date"),
                "deadline": row.get("deadline"),
                "application_period_text": row.get("application_period_text"),
                "source_text": row.get("source_text"),
                "source_fields": row.get("source_fields"),
            }
    except Exception:
        logger.exception("chat application period context lookup failed")
        return None
