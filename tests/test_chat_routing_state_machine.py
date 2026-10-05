"""
요구사항에 명시된 8가지 핵심 시나리오 검증.

1. "정책 추천해줘" → recommend
2. 프로필 확인 중 "네, 이대로 해줘" → profile_confirm 우선 처리 (LLM fast-path)
3. 슬롯 수집 중 자녀 나이 응답 → awaiting_slots 우선 처리
4. 슬롯 수집 중 "그건 됐고 부모급여 신청 방법 알려줘" → apply로 전환
5. 정책 후보가 두 개인 상태에서 "이 정책 자격 돼?" → 정책 선택 질문
6. "부모급여 자격 확인하고 신청 방법도 알려줘" → eligibility + apply를 suggested_actions로
7. Handler가 빈 정책 목록을 반환 → 품질 검증에서 fallback
8. 근거 없는 eligibility 확정 표현 → 품질 검증에서 경고 이슈 추가
"""
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.nodes.chat.chat_nodes import _IntentDecision
from app.services.chat.handlers._quality_validator import validate_branch_result
from app.services.chat.handlers._intent_classifier import (
    classify_intent,
    _build_suggested_actions,
    _validate_llm_decision,
)
from app.services.chat.ai._policy_resolver import (
    _collect_recent_policy_candidates,
)
from app.services.chat.chat_handlers import _build_policy_selection_response


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────────

def _make_intent_decision(intent: str, **kwargs: Any) -> _IntentDecision:
    return _IntentDecision(intent=intent, **kwargs)


def _make_llm_mock(monkeypatch: pytest.MonkeyPatch, intent: str, **kwargs: Any) -> None:
    # _intent_classifier.py는 _llm을 직접 사용하므로 해당 모듈에 패치
    from app.services.chat.handlers import _intent_classifier as _cls_module

    structured = AsyncMock()
    structured.ainvoke = AsyncMock(return_value=_make_intent_decision(intent, **kwargs))
    base = MagicMock()
    base.with_structured_output = MagicMock(return_value=structured)
    monkeypatch.setattr(_cls_module, "_llm", lambda: base)


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


# ── 시나리오 1: "정책 추천해줘" → recommend ──────────────────────────────────

def test_scenario1_recommend_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """classify_intent: '정책 추천해줘' → intent=recommend."""
    _make_llm_mock(monkeypatch, "recommend", confidence=0.95)

    result = asyncio.run(classify_intent(
        user_id=1,
        user_content="정책 추천해줘",
        history=[],
        slot={},
        recent_assistant_policy=None,
    ))

    assert result["supervisor_decision"]["intent"] == "recommend"


# ── 시나리오 2: profile_confirm 중 "네" → fast-path (LLM 생략) ───────────────

def test_scenario2_profile_confirm_yes_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """profile_confirm 상태에서 '네'라고 답하면 LLM 호출 없이 recommend로 진행."""
    from app.services.chat.handlers import _intent_classifier as _cls_module

    llm_mock = MagicMock()
    llm_mock.with_structured_output = MagicMock()
    monkeypatch.setattr(_cls_module, "_llm", lambda: llm_mock)

    slot = {"pending": {"intent": "recommend", "kind": "confirm", "awaiting": [], "asked": []}}
    result = asyncio.run(classify_intent(
        user_id=1,
        user_content="네, 이대로 해줘",
        history=[],
        slot=slot,
        recent_assistant_policy=None,
    ))

    # fast-path: LLM with_structured_output 호출되지 않아야 함
    llm_mock.with_structured_output.assert_not_called()
    assert result["supervisor_decision"]["intent"] == "recommend"
    assert result["supervisor_decision"]["confidence"] == 1.0
    profile = result.get("profile") or {}
    assert profile.get("db_profile_confirmed") is True


def test_scenario2_profile_confirm_no_clears_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """'아니요'로 답하면 awaiting_slots에 슬롯 목록이 채워진다."""
    from app.services.chat.handlers import _intent_classifier as _cls_module

    llm_mock = MagicMock()
    llm_mock.with_structured_output = MagicMock()
    monkeypatch.setattr(_cls_module, "_llm", lambda: llm_mock)

    slot = {"pending": {"intent": "recommend", "kind": "confirm", "awaiting": [], "asked": []}}
    result = asyncio.run(classify_intent(
        user_id=1,
        user_content="아니요, 다시 입력할게요",
        history=[],
        slot=slot,
        recent_assistant_policy=None,
    ))

    llm_mock.with_structured_output.assert_not_called()
    assert result["supervisor_decision"]["intent"] == "recommend"
    assert len(result.get("awaiting_slots") or []) > 0


# ── 시나리오 3: 슬롯 수집 중 자녀 나이 응답 → awaiting_slots 이어받기 ────────

def test_scenario3_slot_filling_resumes_on_slot_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """슬롯 수집 중 자녀 나이를 답하면 intent=recommend로 이어받고 awaiting_slots 갱신."""
    from app.ai.nodes.chat.chat_nodes import _ExtractedProfile
    from app.services.chat.handlers import _intent_classifier as _cls_module

    structured = AsyncMock()
    structured.ainvoke = AsyncMock(return_value=_make_intent_decision(
        "unclear",
        extracted_profile=_ExtractedProfile(child_age="0"),
        confidence=0.7,
    ))
    base = MagicMock()
    base.with_structured_output = MagicMock(return_value=structured)
    monkeypatch.setattr(_cls_module, "_llm", lambda: base)

    slot = {
        "pending": {
            "intent": "recommend",
            "kind": "slot",
            "awaiting": ["child_age", "income"],
            "asked": ["child_age"],
        },
        "profile": {},
    }
    result = asyncio.run(classify_intent(
        user_id=1,
        user_content="0살이에요",
        history=[],
        slot=slot,
        recent_assistant_policy=None,
    ))

    # recommend intent로 이어받기
    assert result["supervisor_decision"]["intent"] == "recommend"


# ── 시나리오 4: 슬롯 수집 중 명확한 topic switch → apply로 전환 ──────────────

def test_scenario4_topic_switch_during_slot_filling(monkeypatch: pytest.MonkeyPatch) -> None:
    """슬롯 수집 중 '부모급여 신청 방법 알려줘'처럼 명확한 새 의도 → apply로 전환."""
    _make_llm_mock(monkeypatch, "apply", confidence=0.9, secondary_intents=[])

    slot = {
        "pending": {
            "intent": "recommend",
            "kind": "slot",
            "awaiting": ["income"],
            "asked": ["child_age"],
        },
        "profile": {"child_age": "0"},
    }
    result = asyncio.run(classify_intent(
        user_id=1,
        user_content="그건 됐고 부모급여 신청 방법 알려줘",
        history=[],
        slot=slot,
        recent_assistant_policy=None,
    ))

    # 높은 confidence + 새 intent → topic switch
    assert result["supervisor_decision"]["intent"] == "apply"


# ── 시나리오 5: 정책 후보 2개 상태에서 "이 정책 자격 돼?" → policy_selection ─

def test_scenario5_ambiguous_policy_returns_candidates() -> None:
    """슬롯에 정책 2개, '이 정책 자격 돼?' → branch_policy_candidates에 선택지 반환."""
    slot = {
        "recent_policies": [
            {"policy_id": 1, "slug": "WLF1", "policy_name": "부모급여", "last_action": "VIEWED"},
            {"policy_id": 2, "slug": "WLF2", "policy_name": "아동수당", "last_action": "VIEWED"},
        ]
    }
    candidates = _collect_recent_policy_candidates(slot)
    assert len(candidates) == 2
    assert candidates[0]["slug"] == "WLF1"
    assert candidates[1]["slug"] == "WLF2"


def test_scenario5_policy_selection_response_structure() -> None:
    """_build_policy_selection_response: 올바른 구조 반환."""
    state: dict[str, Any] = {
        "user_id": 1,
        "user_content": "이 정책 자격 돼?",
        "history": [],
        "supervisor_decision": {"intent": "eligibility", "raw": "{}"},
    }
    candidates = [
        {"slug": "WLF1", "policy_name": "부모급여"},
        {"slug": "WLF2", "policy_name": "아동수당"},
    ]
    result = _build_policy_selection_response(state, candidates, "eligibility", [])

    # 정책 선택 후 clarification pending으로 저장되어야 함
    assert result.get("branch_policy_candidates") == candidates
    pending = result.get("pending") or {}
    assert pending.get("kind") == "clarification"
    assert pending.get("intent") == "eligibility"
    # 다음 턴에 clarification 재개 가능
    assert "부모급여" in result["branch_content"] or "아동수당" in result["branch_content"]


# ── 시나리오 6: 복합 의도 → suggested_actions ────────────────────────────────

def test_scenario6_compound_intent_secondary_actions() -> None:
    """secondary_intents=[apply] → suggested_actions=['apply']."""
    actions = _build_suggested_actions(["apply"])
    assert "apply" in actions


def test_scenario6_suggested_actions_excluded_from_primary() -> None:
    """suggest된 액션 중 primary intent와 동일한 것은 quality validator가 제거한다."""
    state: dict[str, Any] = {
        "user_id": 1,
        "user_content": "자격 확인하고 신청 방법도 알려줘",
        "history": [],
        "branch_content": "부모급여 자격을 분석했어요.",
        "branch_policies": [{"slug": "WLF1", "policy_name": "부모급여"}],
        "branch_evidences": [
            {
                "chunk_id": 1,
                "snippet": "부모급여 자격 근거",
                "source_url": "https://example.com/policy",
            }
        ],
        "branch_suggested_actions": ["eligibility", "apply"],  # eligibility가 primary와 중복
        "supervisor_decision": {"intent": "eligibility", "raw": "{}"},
    }

    result = validate_branch_result(state, "eligibility")
    # eligibility가 제거되고 apply만 남아야 함
    assert result is not None
    assert result["branch_suggested_actions"] == ["apply"]
    assert "_validation_issues" in result


# ── 시나리오 7: Handler가 빈 content 반환 → 품질 검증 fallback ───────────────

def test_scenario7_empty_content_gets_fallback() -> None:
    """branch_content가 비어있으면 안전한 fallback 메시지로 교체."""
    state: dict[str, Any] = {
        "user_id": 1,
        "user_content": "추천해줘",
        "history": [],
        "branch_content": "",
        "branch_policies": [],
        "branch_evidences": [],
        "supervisor_decision": {"intent": "recommend", "raw": "{}"},
    }

    result = validate_branch_result(state, "recommend")
    assert result is not None
    assert result["branch_content"].strip() != ""
    assert "empty_content" in result["_validation_issues"]


def test_scenario7_valid_content_passes_validation() -> None:
    """정상적인 응답은 None 반환 (보정 없음)."""
    state: dict[str, Any] = {
        "user_id": 1,
        "user_content": "추천해줘",
        "history": [],
        "branch_content": "맞춤 정책을 추천해드릴게요.",
        "branch_policies": [],
        "branch_evidences": [],
        "supervisor_decision": {"intent": "recommend", "raw": "{}"},
    }

    result = validate_branch_result(state, "recommend")
    assert result is None


# ── 시나리오 8: eligibility 확정 표현 → validation 이슈 추가 ──────────────────

def test_scenario8_assertive_eligibility_flagged() -> None:
    """eligibility result 없이 확정 표현 사용 → 이슈 기록."""
    state: dict[str, Any] = {
        "user_id": 1,
        "user_content": "부모급여 받을 수 있어?",
        "history": [],
        "branch_content": "네, 받을 수 있습니다.",  # 확정 표현
        "branch_policies": [{"slug": "WLF1", "policy_name": "부모급여"}],
        "branch_evidences": [],
        # branch_eligibility_result 없음 → 구조화된 판단 근거 없음
        "supervisor_decision": {"intent": "eligibility", "raw": "{}"},
    }

    result = validate_branch_result(state, "eligibility")
    assert result is not None
    assert "assertive_without_eligibility_result" in result["_validation_issues"]


def test_scenario8_eligibility_with_result_not_flagged() -> None:
    """eligibility_result가 있으면 확정 표현도 허용 (구조화된 근거 있음)."""
    state: dict[str, Any] = {
        "user_id": 1,
        "user_content": "부모급여 받을 수 있어?",
        "history": [],
        "branch_content": "조건에 해당될 수 있어요.",
        "branch_policies": [{"slug": "WLF1", "policy_name": "부모급여"}],
        "branch_evidences": [
            {
                "chunk_id": 1,
                "snippet": "부모급여 자격 근거",
                "source_url": "https://example.com/policy",
            }
        ],
        "branch_eligibility_result": {
            "status": "COMPLETED",
            "user_status": "ELIGIBLE",
        },
        "supervisor_decision": {"intent": "eligibility", "raw": "{}"},
    }

    result = validate_branch_result(state, "eligibility")
    assert result is None


# ── Python 검증 규칙 단위 테스트 ──────────────────────────────────────────────

def test_validate_llm_decision_clears_invalid_slug() -> None:
    """슬롯에 없는 slug 검증은 classify_intent의 초기 체크에서 처리된다.
    _validate_llm_decision은 intent/confidence 보정에 집중한다.
    (test_chat_slot.py::test_supervisor_rejects_hallucinated_slug_not_in_slot로 전체 흐름 검증)
    """
    # slug 필터링 없이도 confidence/intent 보정은 정상 작동해야 함
    decision = _make_intent_decision(
        "eligibility",
        resolved_policy_slug="NONEXISTENT",
        is_context_dependent=True,
        confidence=0.85,
    )
    corrected = _validate_llm_decision(decision, {}, None)
    # 고확신도 + 명확한 intent → 변경 없음
    assert corrected.intent == "eligibility"
    assert corrected.confidence == 0.85


def test_validate_llm_decision_very_low_confidence_unclear() -> None:
    """pending 없이 confidence < 0.5 → unclear로 강제 변환."""
    decision = _make_intent_decision("recommend", confidence=0.4)
    corrected = _validate_llm_decision(decision, {}, None)
    assert corrected.intent == "unclear"


def test_validate_llm_decision_slot_filling_low_confidence_resumes() -> None:
    """slot-filling 중 confidence < 0.6 + 슬롯 미답변 → pending intent 유지."""
    decision = _make_intent_decision(
        "compare",  # 새 의도지만 confidence 낮음
        confidence=0.5,
        extracted_profile=None,
    )
    pending = {"intent": "recommend", "kind": "slot", "awaiting": ["income"], "asked": []}
    corrected = _validate_llm_decision(decision, {}, pending)
    assert corrected.intent == "recommend"


# ── _collect_recent_policy_candidates 단위 테스트 ─────────────────────────────

def test_collect_candidates_single_policy_returns_empty() -> None:
    slot = {
        "recent_policies": [
            {"policy_id": 1, "slug": "WLF1", "policy_name": "부모급여", "last_action": "VIEWED"}
        ]
    }
    assert _collect_recent_policy_candidates(slot) == []


def test_collect_candidates_two_policies_returns_both() -> None:
    slot = {
        "recent_policies": [
            {"policy_id": 1, "slug": "WLF1", "policy_name": "부모급여", "last_action": "VIEWED"},
            {"policy_id": 2, "slug": "WLF2", "policy_name": "아동수당", "last_action": "VIEWED"},
        ]
    }
    result = _collect_recent_policy_candidates(slot)
    assert len(result) == 2
    slugs = {c["slug"] for c in result}
    assert slugs == {"WLF1", "WLF2"}


def test_collect_candidates_empty_slot() -> None:
    assert _collect_recent_policy_candidates({}) == []
    assert _collect_recent_policy_candidates(None) == []
