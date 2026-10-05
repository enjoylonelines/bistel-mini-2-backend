"""
공통 품질 검증 모듈.

Handler 결과가 build_assistant_payload()를 거치기 전에 결정론적 Python 규칙으로 검증한다.
LLM 검증은 이 인터페이스를 통해 선택적으로 추가할 수 있도록 분리되어 있다.
"""
from __future__ import annotations

import logging
from typing import Any

from app.ai.states.chat_state import Intent
from app.services.chat.handlers._evidence_review import review_evidence_completeness

logger = logging.getLogger(__name__)

_ASSERTIVE_ELIGIBILITY_PHRASES = (
    "받을 수 있습니다",
    "받을 수 있어요",
    "받으실 수 있습니다",
    "신청 가능합니다",
    "신청하실 수 있습니다",
    "지원받을 수 있습니다",
    "지원받으실 수 있습니다",
    "대상입니다",
    "해당됩니다",
)

_SAFE_FALLBACK_CONTENT = (
    "죄송합니다. 답변을 생성하는 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요."
)

_EVIDENCE_BLOCKED_CONTENT = (
    "정책 근거를 확인하지 못해 답변을 제공할 수 없어요. "
    "공식 안내를 확인한 뒤 다시 시도해 주세요."
)

_VALID_RESPONSE_TYPES: frozenset[str] = frozenset({
    "slot_request",
    "profile_confirm",
    "policy_selection",
    "eligibility_result",
    "apply_card",
    "policy_list",
    "text",
})


def _detect_response_type(state: dict[str, Any]) -> str:
    if state.get("slot_request"):
        return "slot_request"
    if state.get("profile_confirm"):
        return "profile_confirm"
    if state.get("branch_policy_candidates"):
        return "policy_selection"
    if state.get("branch_eligibility_result"):
        return "eligibility_result"
    if state.get("branch_apply_card"):
        return "apply_card"
    if state.get("branch_policies"):
        return "policy_list"
    return "text"


def validate_branch_result(
    state: dict[str, Any],
    intent: Intent,
) -> dict[str, Any] | None:
    """
    결정론적 품질 검증. 문제 발견 시 보정된 state를 반환하고, 문제 없으면 None을 반환한다.

    검증 항목:
    - 빈 응답 텍스트
    - 구조화된 eligibility 결과 없이 자격 확정 표현 사용
    - 후속 행동이 현재 의도와 동일 (중복 제거)
    """
    slot_request = state.get("slot_request")
    profile_confirm = state.get("profile_confirm")

    if slot_request or profile_confirm:
        return None

    content: str = state.get("branch_content") or ""
    issues: list[str] = []
    corrections: dict[str, Any] = {}

    # 1. 빈 응답 텍스트 → 안전한 fallback
    if not content.strip():
        issues.append("empty_content")
        corrections["branch_content"] = _SAFE_FALLBACK_CONTENT
        logger.warning(
            "quality_validation: empty content for intent=%s; using fallback",
            intent,
        )

    # 2. eligibility: 구조화된 결과 없이 확정 표현 → 경고만 (disclaimer는 payload 단계에서 처리)
    if (
        intent == "eligibility"
        and not state.get("branch_eligibility_result")
        and not state.get("branch_policy_candidates")
        and content.strip()
    ):
        for phrase in _ASSERTIVE_ELIGIBILITY_PHRASES:
            if phrase in content:
                issues.append("assertive_without_eligibility_result")
                logger.warning(
                    "quality_validation: assertive phrase '%s' used without eligibility result",
                    phrase,
                )
                break

    # 3. suggested_actions에서 primary intent와 동일한 항목 제거 (중복 방지)
    suggested = list(state.get("branch_suggested_actions") or [])
    cleaned_suggested = [a for a in suggested if a != intent]
    if len(cleaned_suggested) != len(suggested):
        issues.append("duplicate_suggested_action_removed")
        corrections["branch_suggested_actions"] = cleaned_suggested

    # 4. 정책성 결과의 근거 완결성. 문장 의미를 추론하지 않고, 정책 결과가
    #    retrievable provenance와 함께 나오는지만 확인한다.
    evidence_review = review_evidence_completeness(
        {**state, **corrections}, intent=intent
    )
    corrections["evidence_review"] = evidence_review
    verdict = evidence_review["verdict"]
    if verdict == "REVIEW_REQUIRED":
        issues.append("evidence_review_required")
    elif verdict == "BLOCKED":
        issues.append("evidence_missing_blocked")
        corrections.update(
            {
                "branch_content": _EVIDENCE_BLOCKED_CONTENT,
                "branch_user_status": None,
                "branch_policies": [],
                "branch_evidences": [],
                "branch_apply_card": None,
                "branch_easy_summary": None,
                "branch_key_points": [],
                "branch_eligibility_result": None,
                "branch_suggested_actions": [],
            }
        )

    if not issues:
        return None

    result: dict[str, Any] = {**state, **corrections, "_validation_issues": issues}
    return result


async def validate_branch_result_with_llm(
    state: dict[str, Any],
    intent: Intent,
    *,
    llm_check_enabled: bool = False,
) -> dict[str, Any] | None:
    """
    LLM 기반 주관적 품질 검증 인터페이스 (선택적).
    llm_check_enabled=False이면 결정론적 검증만 실행한다.
    """
    result = validate_branch_result(state, intent)
    if not llm_check_enabled:
        return result
    # TODO: LLM 품질 검증이 필요할 경우 여기에 구현
    return result
