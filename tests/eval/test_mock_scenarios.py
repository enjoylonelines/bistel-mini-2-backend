"""
Mock 기반 결정론적 통합 테스트 35개.

검증 계층:
  Layer 1 (classify_intent): LLM 출력만 고정 → 의도 분류·pending 상태 머신 검증
  Layer 2 (_run_chat):       classify_intent 결과까지 고정 → 핸들러 디스패치·payload 검증
  Layer 3 (follow_up):       classify_follow_up_intent 검증

외부 의존:
  - LLM: monkeypatch로 _intent_classifier._llm / _follow_up._FOLLOW_UP_LLM 교체
  - 생명주기 Runner: monkeypatch로 lifecycle 함수 교체
  - DB: object() stub (handle_eligibility/apply/recommend 모두 내부에서 del db)
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.nodes.chat.chat_nodes import _ExtractedProfile, _IntentDecision
from app.services.chat._routing import run_chat as _run_chat
from app.services.chat.handlers._intent_classifier import classify_intent


# ─── 공통 헬퍼 ────────────────────────────────────────────────────────────────

def _intent_decision(intent: str, **kwargs: Any) -> _IntentDecision:
    return _IntentDecision(intent=intent, **kwargs)


def _mock_classifier_llm(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    **kwargs: Any,
) -> None:
    from app.services.chat.handlers import _intent_classifier as _m

    structured = AsyncMock()
    structured.ainvoke = AsyncMock(return_value=_intent_decision(intent, **kwargs))
    base = MagicMock()
    base.with_structured_output = MagicMock(return_value=structured)
    monkeypatch.setattr(_m, "_llm", lambda: base)


def _mock_no_db_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.chat.handlers import _intent_classifier as _m

    monkeypatch.setattr(_m, "_load_db_profile_summary", AsyncMock(return_value=None))


def _mock_db_profile(monkeypatch: pytest.MonkeyPatch, summary: list[str]) -> None:
    from app.services.chat.handlers import _intent_classifier as _m

    monkeypatch.setattr(_m, "_load_db_profile_summary", AsyncMock(return_value=summary))


def _fake_branch_result(
    state: dict[str, Any],
    *,
    content: str = "테스트 응답",
    policies: list[dict] | None = None,
    apply_card: dict | None = None,
    eligibility_result: dict | None = None,
    candidates: list[dict] | None = None,
    pending: dict | None = None,
) -> dict[str, Any]:
    requires_evidence = bool(policies or apply_card or eligibility_result)
    result: dict[str, Any] = {
        **state,
        "branch_content": content,
        "branch_policies": policies or [],
        "branch_evidences": (
            [
                {
                    "chunk_id": 990001,
                    "snippet": "평가용 정책 근거",
                    "source_url": "fixture://mock-scenarios/policy-001",
                }
            ]
            if requires_evidence
            else []
        ),
    }
    if apply_card is not None:
        result["branch_apply_card"] = apply_card
    if eligibility_result is not None:
        result["branch_eligibility_result"] = eligibility_result
        result["branch_user_status"] = "eligible"
    if candidates is not None:
        result["branch_policy_candidates"] = candidates
        result["pending"] = pending or {
            "intent": "eligibility",
            "kind": "clarification",
            "awaiting": [],
            "asked": [],
        }
    return result


def _intent_state(
    intent: str,
    *,
    slot: dict | None = None,
    profile: dict | None = None,
    awaiting: list[str] | None = None,
    profile_confirm: dict | None = None,
    secondary_intents: list[str] | None = None,
    is_context_dependent: bool = False,
    resolved_slug: str | None = None,
) -> dict[str, Any]:
    decision: dict[str, Any] = {
        "intent": intent,
        "raw": "{}",
        "secondary_intents": secondary_intents or [],
        "is_context_dependent": is_context_dependent,
        "resolved_policy_slug": resolved_slug,
        "confidence": 0.9,
        "ambiguity_reason": None,
    }
    return {
        "user_id": 1,
        "user_content": "테스트 메시지",
        "history": [],
        "slot": slot or {},
        "profile": profile or {},
        "pending_intent": None,
        "awaiting_slots": awaiting or [],
        "profile_confirm": profile_confirm,
        "branch_suggested_actions": (
            [si for si in (secondary_intents or []) if si in ("recommend", "eligibility", "compare", "apply")]
        ),
        "supervisor_decision": decision,
        "recent_assistant_policy": None,
    }


async def _run_with_patched_classify(
    monkeypatch: pytest.MonkeyPatch,
    fixed_state: dict[str, Any],
) -> dict[str, Any]:
    import app.services.chat.chat_handlers as _ch

    async def _fake_classify(**_: Any) -> dict[str, Any]:
        return fixed_state

    monkeypatch.setattr(_ch, "classify_intent", _fake_classify)
    return await _run_chat(
        db=object(),
        user_id=1,
        user_content=fixed_state.get("user_content", "테스트"),
        history=[],
        slot={},
        recent_assistant_policy=None,
    )


# ─── Layer 1: classify_intent 테스트 (M01~M03, M06, M11, M15, M19~M20, M22~M27, M34~M35) ─

class TestClassifyIntentRecommend:
    def test_m01_no_slots_awaits_child_age(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M01: 슬롯 없음·DB 프로필 없음 → awaiting_slots=[child_age]"""
        _mock_classifier_llm(monkeypatch, "recommend", confidence=0.95)
        _mock_no_db_profile(monkeypatch)

        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="정책 추천해줘",
            history=[],
            slot={},
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "recommend"
        awaiting = result.get("awaiting_slots") or []
        assert "child_age" in awaiting, f"expected child_age in awaiting, got {awaiting}"
        assert not result.get("profile_confirm")

    def test_m02_all_slots_filled_no_awaiting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M02: 모든 슬롯 채워진 상태 → awaiting_slots=[]"""
        _mock_classifier_llm(monkeypatch, "recommend", confidence=0.95)
        _mock_no_db_profile(monkeypatch)

        slot = {
            "profile": {
                "child_age": "0",
                "income": "mid1",
                "region": "seoul",
                "special": [],
                "db_profile_confirmed": False,
            }
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="정책 추천해줘",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "recommend"
        assert not result.get("awaiting_slots")
        assert not result.get("profile_confirm")

    def test_m03_db_profile_available_profile_confirm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M03: DB 프로필 존재 → profile_confirm 프롬프트 반환"""
        _mock_classifier_llm(monkeypatch, "recommend", confidence=0.95)
        _mock_db_profile(monkeypatch, ["만 0세 아이", "서울 거주"])

        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="정책 추천해줘",
            history=[],
            slot={},
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "recommend"
        assert result.get("profile_confirm") is not None
        assert not result.get("awaiting_slots")


class TestClassifyIntentOtherIntents:
    def test_m06_eligibility_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M06: '부모급여 자격 돼?' → intent=eligibility"""
        _mock_classifier_llm(monkeypatch, "eligibility", confidence=0.92)
        _mock_no_db_profile(monkeypatch)

        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="부모급여 자격 돼?",
            history=[],
            slot={},
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "eligibility"

    def test_m11_compare_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M11: 비교 요청 → intent=compare"""
        _mock_classifier_llm(monkeypatch, "compare", confidence=0.90)

        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="부모급여랑 첫만남이용권 중에 어느 게 나아?",
            history=[],
            slot={},
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "compare"

    def test_m15_apply_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M15: 신청 방법 요청 → intent=apply"""
        _mock_classifier_llm(monkeypatch, "apply", confidence=0.93)

        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="부모급여 신청하는 방법 알려줘",
            history=[],
            slot={},
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "apply"

    def test_m19_policy_summary_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M19: '부모급여가 뭐야?' → intent=policy_summary"""
        _mock_classifier_llm(monkeypatch, "policy_summary", confidence=0.91)

        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="부모급여가 뭐야?",
            history=[],
            slot={},
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "policy_summary"

    def test_m20_summary_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M20: '요약해줘' → intent=summary"""
        _mock_classifier_llm(monkeypatch, "summary", confidence=0.87)

        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="지금까지 내용 요약해줘",
            history=[
                {"role": "user", "content": "부모급여 알려줘"},
                {"role": "assistant", "content": "부모급여는 ..."},
            ],
            slot={},
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "summary"


class TestClassifyIntentProfileConfirm:
    def test_m22_confirm_yes_fast_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M22: profile_confirm 상태 + '네' → LLM 생략, db_profile_confirmed=True"""
        from app.services.chat.handlers import _intent_classifier as _m

        llm_mock = MagicMock()
        llm_mock.with_structured_output = MagicMock()
        monkeypatch.setattr(_m, "_llm", lambda: llm_mock)

        slot = {
            "pending": {"intent": "recommend", "kind": "confirm", "awaiting": [], "asked": []},
            "profile": {"stage": "infant", "child_age": "0"},
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="네, 이대로 해줘",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        llm_mock.with_structured_output.assert_not_called()
        assert result["supervisor_decision"]["intent"] == "recommend"
        assert result["supervisor_decision"]["confidence"] == 1.0
        profile = result.get("profile") or {}
        assert profile.get("db_profile_confirmed") is True
        assert not result.get("awaiting_slots")

    def test_m23_confirm_no_fast_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M23: profile_confirm 상태 + '아니요' → LLM 생략, awaiting=[child_age]"""
        from app.services.chat.handlers import _intent_classifier as _m

        llm_mock = MagicMock()
        llm_mock.with_structured_output = MagicMock()
        monkeypatch.setattr(_m, "_llm", lambda: llm_mock)

        slot = {
            "pending": {"intent": "recommend", "kind": "confirm", "awaiting": [], "asked": []},
            "profile": {},
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="아니요, 다시 입력할게요",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        llm_mock.with_structured_output.assert_not_called()
        assert result["supervisor_decision"]["intent"] == "recommend"
        awaiting = result.get("awaiting_slots") or []
        assert len(awaiting) > 0, "아니요 응답 시 awaiting_slots가 채워져야 한다"

    def test_m24_confirm_ambiguous_llm_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M24: profile_confirm 상태 + 불명확 응답 → LLM 통과 후 confirm 재개"""
        _mock_classifier_llm(monkeypatch, "unclear", confidence=0.55)
        _mock_no_db_profile(monkeypatch)

        slot = {
            "pending": {"intent": "recommend", "kind": "confirm", "awaiting": [], "asked": []},
            "profile": {},
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="어떤 거지?",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        # 불명확 응답이지만 confirm 상태이므로 intent=recommend로 재개되어야 함
        assert result["supervisor_decision"]["intent"] == "recommend"


class TestClassifyIntentAwaitingSlots:
    def test_m25_child_age_slot_answered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M25: 슬롯 수집 중 '6개월' → child_age 추출, intent=recommend 유지"""
        _mock_classifier_llm(
            monkeypatch,
            "unclear",
            confidence=0.65,
            extracted_profile=_ExtractedProfile(child_age="0"),
        )

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
            user_content="6개월 됐어요",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "recommend"
        profile = result.get("profile") or {}
        assert profile.get("child_age") == "0", f"child_age not extracted: {profile}"

    def test_m26_region_slot_answered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M26: 지역 슬롯 수집 중 '경기도' → region=gyeonggi 추출"""
        _mock_classifier_llm(
            monkeypatch,
            "unclear",
            confidence=0.70,
            extracted_profile=_ExtractedProfile(region="gyeonggi"),
        )

        slot = {
            "pending": {
                "intent": "recommend",
                "kind": "slot",
                "awaiting": ["region"],
                "asked": ["region"],
            },
            "profile": {"child_age": "0", "income": "mid1"},
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="경기도에 살아요",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "recommend"
        profile = result.get("profile") or {}
        assert profile.get("region") == "gyeonggi", f"region not extracted: {profile}"

    def test_m27_topic_switch_during_slot_filling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M27: 슬롯 수집 도중 확신도 높은 새 의도 → topic switch, pending 해제"""
        _mock_classifier_llm(monkeypatch, "apply", confidence=0.90)

        slot = {
            "pending": {
                "intent": "recommend",
                "kind": "slot",
                "awaiting": ["child_age"],
                "asked": ["child_age"],
            },
            "profile": {},
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="그건 됐고 부모급여 신청 방법 알려줘",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "apply"
        # topic switch 시 awaiting_slots가 비어야 한다
        awaiting = result.get("awaiting_slots") or []
        assert not awaiting, f"topic switch 후 awaiting_slots가 남아있음: {awaiting}"


class TestClassifyIntentPolicyAmbiguity:
    def test_m31_two_policies_context_dependent_candidates_in_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M31: 슬롯 2개 + is_context_dependent=True → 분류 결과 확인"""
        _mock_classifier_llm(
            monkeypatch,
            "eligibility",
            confidence=0.82,
            is_context_dependent=True,
        )

        slot = {
            "recent_policies": [
                {"slug": "bomo-gupyeo", "policy_name": "부모급여"},
                {"slug": "cheot-mannam", "policy_name": "첫만남이용권"},
            ]
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="이 정책 자격 되는지 알려줘",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "eligibility"
        assert result["supervisor_decision"]["is_context_dependent"] is True

    def test_m32_resolved_slug_validated_against_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M32: resolved_policy_slug + 슬롯에 해당 정책 존재 → slug 검증 통과"""
        _mock_classifier_llm(
            monkeypatch,
            "apply",
            confidence=0.88,
            is_context_dependent=True,
            resolved_policy_slug="bomo-gupyeo",
        )

        slot = {
            "recent_policies": [
                {
                    "slug": "bomo-gupyeo",
                    "policy_name": "부모급여",
                    "last_action": "ELIGIBILITY",
                }
            ]
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="그거 신청해줘",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["resolved_policy_slug"] == "bomo-gupyeo"

    def test_m33_resolved_slug_not_in_slot_cleared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M33: resolved_policy_slug 있지만 슬롯에 없음 → slug 검증 실패로 None"""
        _mock_classifier_llm(
            monkeypatch,
            "apply",
            confidence=0.78,
            is_context_dependent=True,
            resolved_policy_slug="bomo-gupyeo",
        )

        # 슬롯에 해당 정책 없음
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="이 정책 신청해줘",
            history=[],
            slot={},
            recent_assistant_policy=None,
        ))

        # slug 검증 실패 → resolved_policy_slug=None
        assert result["supervisor_decision"]["resolved_policy_slug"] is None


class TestClassifyIntentMultiIntentAndUnclear:
    def test_m34_secondary_intents_to_suggested_actions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M34: secondary_intents=[apply] → branch_suggested_actions에 apply 포함"""
        _mock_classifier_llm(
            monkeypatch,
            "eligibility",
            confidence=0.90,
            secondary_intents=["apply"],
        )

        slot = {
            "profile": {
                "child_age": "0",
                "income": "mid1",
                "region": "seoul",
                "special": [],
            },
            "recent_policies": [{"slug": "bomo-gupyeo", "policy_name": "부모급여"}],
        }
        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="부모급여 자격 확인하고 신청 방법도 알려줘",
            history=[],
            slot=slot,
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "eligibility"
        suggested = result.get("branch_suggested_actions") or []
        assert "apply" in suggested, f"apply not in suggested_actions: {suggested}"

    def test_m35_very_low_confidence_no_pending_unclear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M35: confidence=0.3 + pending 없음 → _validate_llm_decision → unclear"""
        _mock_classifier_llm(monkeypatch, "recommend", confidence=0.30)

        result = asyncio.run(classify_intent(
            user_id=1,
            user_content="음...",
            history=[],
            slot={},
            recent_assistant_policy=None,
        ))

        assert result["supervisor_decision"]["intent"] == "unclear"


# ─── Layer 2: _run_chat 핸들러 디스패치·payload 검증 ───────────────────────────

class TestRunChatRecommend:
    def test_m04_recommend_returns_policy_list_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M04: recommend → branch_policies 있음 → payload.policies 채워짐"""
        import app.services.chat.chat_handlers as _ch

        profile = {"child_age": "0", "income": "mid1", "region": "seoul", "special": []}
        fixed_state = _intent_state("recommend", profile=profile)

        fake_policies = [
            {
                "policy_id": 1,
                "slug": "bomo-gupyeo",
                "policy_name": "부모급여",
                "source_url": "https://example.com",
                "amount": "100만원",
                "period": "0-23개월",
                "target": "부모",
            }
        ]

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(state, content="맞춤 정책이에요.", policies=fake_policies)

        monkeypatch.setattr(_ch, "handle_recommend", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert len(payload["policies"]) > 0
        assert payload["content"] == "맞춤 정책이에요."
        assert payload["policies"][0]["slug"] == "bomo-gupyeo"

    def test_m05_recommend_empty_policies_quality_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M05: handle_recommend 빈 정책 목록·빈 content → 품질 검증 fallback content"""
        import app.services.chat.chat_handlers as _ch

        profile = {"child_age": "0", "income": "mid1", "region": "seoul", "special": []}
        fixed_state = _intent_state("recommend", profile=profile)

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(state, content="", policies=[])

        monkeypatch.setattr(_ch, "handle_recommend", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["content"], "fallback content가 설정되어야 한다"
        assert "문제" in payload["content"] or "죄송" in payload["content"]


class TestRunChatEligibility:
    def test_m07_eligibility_resolves_policy_from_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M07: 슬롯 1개·resolved_slug → handle_eligibility → eligibility_result payload"""
        import app.services.chat.chat_handlers as _ch

        slot = {"recent_policies": [{"slug": "bomo-gupyeo", "policy_name": "부모급여", "last_action": "ELIGIBILITY"}]}
        fixed_state = _intent_state(
            "eligibility",
            slot=slot,
            is_context_dependent=True,
            resolved_slug="bomo-gupyeo",
        )

        elig_result = {
            "status": "ELIGIBLE",
            "user_status": "eligible",
            "assessment_status": "COMPLETED",
            "follow_up_questions": [],
            "summary": "자격 조건을 충족합니다.",
            "request_id": 99,
            "criteria": [],
        }

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(
                state,
                content="부모급여 자격이 확인되었어요.",
                policies=[{"slug": "bomo-gupyeo", "policy_name": "부모급여"}],
                eligibility_result=elig_result,
            )

        monkeypatch.setattr(_ch, "handle_eligibility", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["eligibility_result"] is not None
        assert payload["eligibility_result"]["status"] == "ELIGIBLE"

    def test_m08_eligibility_follow_up_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M08: eligibility → FOLLOW_UP_REQUIRED → eligibility_result에 follow_up_questions 포함"""
        import app.services.chat.chat_handlers as _ch

        slot = {"recent_policies": [{"slug": "bomo-gupyeo", "policy_name": "부모급여", "last_action": "ELIGIBILITY"}]}
        fixed_state = _intent_state("eligibility", slot=slot)

        elig_result = {
            "status": "FOLLOW_UP_REQUIRED",
            "user_status": None,
            "assessment_status": "FOLLOW_UP_REQUIRED",
            "follow_up_questions": [{"question": "child_age", "label": "자녀 나이"}],
            "summary": None,
            "request_id": 42,
            "criteria": [],
        }

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(
                state,
                content="추가 정보가 필요해요.",
                eligibility_result=elig_result,
            )

        monkeypatch.setattr(_ch, "handle_eligibility", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["eligibility_result"]["status"] == "FOLLOW_UP_REQUIRED"
        assert len(payload["eligibility_result"]["follow_up_questions"]) > 0

    def test_m09_eligibility_no_policy_clarification_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M09: 정책 미지정·슬롯 없음 → handle_eligibility → clarification 텍스트"""
        import app.services.chat.chat_handlers as _ch

        fixed_state = _intent_state("eligibility")

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(state, content="어떤 정책을 자격 확인하고 싶으신가요?")

        monkeypatch.setattr(_ch, "handle_eligibility", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["eligibility_result"] is None
        assert payload["content"]

    def test_m10_eligibility_two_policies_policy_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M10: 슬롯 2개 + is_context_dependent=True → policy_selection 응답"""
        import app.services.chat.chat_handlers as _ch

        slot = {
            "recent_policies": [
                {"slug": "bomo-gupyeo", "policy_name": "부모급여"},
                {"slug": "cheot-mannam", "policy_name": "첫만남이용권"},
            ]
        }
        fixed_state = _intent_state("eligibility", slot=slot, is_context_dependent=True)

        candidates = [
            {"slug": "bomo-gupyeo", "policy_name": "부모급여"},
            {"slug": "cheot-mannam", "policy_name": "첫만남이용권"},
        ]

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(
                state,
                content="어떤 정책을 말씀하시는 건가요?",
                candidates=candidates,
                pending={"intent": "eligibility", "kind": "clarification", "awaiting": [], "asked": []},
            )

        monkeypatch.setattr(_ch, "handle_eligibility", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["policy_selection"] is not None
        assert len(payload["policy_selection"]["candidates"]) == 2


class TestRunChatCompare:
    def test_m12_compare_two_policies_runs_comparison(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M12: 슬롯 2개 → handle_compare → text 응답"""
        import app.services.chat.chat_handlers as _ch

        slot = {
            "recent_policies": [
                {"slug": "bomo-gupyeo", "policy_name": "부모급여"},
                {"slug": "cheot-mannam", "policy_name": "첫만남이용권"},
            ]
        }
        fixed_state = _intent_state("compare", slot=slot, is_context_dependent=True)

        async def _fake_handle(state: dict) -> dict:
            return _fake_branch_result(state, content="두 정책을 비교하면...")

        monkeypatch.setattr(_ch, "handle_compare", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert "비교" in payload["content"] or payload["content"]

    def test_m13_compare_one_policy_policy_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M13: 슬롯 1개만 → policy_selection 반환"""
        import app.services.chat.chat_handlers as _ch

        slot = {"recent_policies": [{"slug": "bomo-gupyeo", "policy_name": "부모급여"}]}
        fixed_state = _intent_state("compare", slot=slot, is_context_dependent=True)

        candidates = [{"slug": "bomo-gupyeo", "policy_name": "부모급여"}]

        async def _fake_handle(state: dict) -> dict:
            return _fake_branch_result(
                state,
                content="비교할 정책을 선택해주세요.",
                candidates=candidates,
                pending={"intent": "compare", "kind": "clarification", "awaiting": [], "asked": []},
            )

        monkeypatch.setattr(_ch, "handle_compare", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["policy_selection"] is not None

    def test_m14_compare_no_policies_clarification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M14: 슬롯 없음 → clarification 텍스트"""
        import app.services.chat.chat_handlers as _ch

        fixed_state = _intent_state("compare")

        async def _fake_handle(state: dict) -> dict:
            return _fake_branch_result(state, content="어떤 정책들을 비교할까요?")

        monkeypatch.setattr(_ch, "handle_compare", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["content"]
        assert not payload["policy_selection"]


class TestRunChatApply:
    def test_m16_apply_with_policy_returns_apply_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M16: 슬롯 1개·resolved_slug → apply_card 포함 payload"""
        import app.services.chat.chat_handlers as _ch

        slot = {"recent_policies": [{"slug": "bomo-gupyeo", "policy_name": "부모급여", "last_action": "ELIGIBILITY"}]}
        fixed_state = _intent_state("apply", slot=slot, resolved_slug="bomo-gupyeo")

        fake_card = {
            "policy_name": "부모급여",
            "apply_url": "https://bokjiro.go.kr",
            "method": "온라인",
            "required_docs": ["출생증명서"],
        }

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(
                state,
                content="부모급여 신청 방법을 안내해드릴게요.",
                apply_card=fake_card,
            )

        monkeypatch.setattr(_ch, "handle_apply", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["apply_card"] is not None
        assert payload["apply_card"]["apply_url"] == "https://bokjiro.go.kr"

    def test_m17_apply_no_policy_clarification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M17: 정책 미지정·슬롯 없음 → clarification 텍스트"""
        import app.services.chat.chat_handlers as _ch

        fixed_state = _intent_state("apply")

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(state, content="어떤 정책을 신청하고 싶으신가요?")

        monkeypatch.setattr(_ch, "handle_apply", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["apply_card"] is None
        assert payload["content"]

    def test_m18_apply_content_without_card_is_valid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M18: apply_card 없이 content만 있는 경우 → 품질 검증 통과(text 응답)"""
        import app.services.chat.chat_handlers as _ch

        slot = {"recent_policies": [{"slug": "bomo-gupyeo", "policy_name": "부모급여"}]}
        fixed_state = _intent_state("apply", slot=slot)

        async def _fake_handle(state: dict, db: Any = None) -> dict:
            return _fake_branch_result(
                state,
                content="부모급여는 복지로 사이트에서 신청할 수 있어요.",
            )

        monkeypatch.setattr(_ch, "handle_apply", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["content"]
        assert payload["apply_card"] is None
        # content가 있으면 품질 검증 통과 → fallback 없음
        assert "죄송" not in payload["content"]


class TestRunChatPolicySummary:
    def test_m21_policy_summary_no_policy_clarification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M21: policy_summary + 지시어 + 슬롯 없음 → clarification"""
        import app.services.chat.chat_handlers as _ch

        fixed_state = _intent_state("policy_summary", is_context_dependent=True)

        async def _fake_handle(state: dict) -> dict:
            return _fake_branch_result(state, content="어떤 정책에 대해 설명해드릴까요?")

        monkeypatch.setattr(_ch, "handle_policy_summary", _fake_handle)

        result = asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        payload = result["assistant_payload"]
        assert payload["content"]


class TestRunChatHandleConfirmAndSlot:
    def test_m04_profile_confirm_triggers_handle_confirm_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """profile_confirm 있는 상태 → handle_confirm_profile 호출"""
        import app.services.chat.chat_handlers as _ch

        confirm_data = {
            "summary": ["만 0세 아이", "서울 거주"],
            "options": [{"label": "네, 이대로", "value": "yes"}, {"label": "아니요", "value": "no"}],
        }
        fixed_state = _intent_state(
            "recommend",
            profile_confirm=confirm_data,
        )

        handle_confirm_called = []

        async def _fake_confirm(state: dict) -> dict:
            handle_confirm_called.append(True)
            result = {**state}
            result["slot_request"] = None
            result["profile_confirm"] = confirm_data
            result["branch_content"] = "프로필이 맞나요?"
            result["branch_policies"] = []
            result["branch_evidences"] = []
            return result

        monkeypatch.setattr(_ch, "handle_confirm_profile", _fake_confirm)

        asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        assert handle_confirm_called, "handle_confirm_profile이 호출되어야 한다"

    def test_m25_awaiting_slots_triggers_handle_collect_slots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """awaiting_slots 있는 상태 → handle_collect_slots 호출"""
        import app.services.chat.chat_handlers as _ch

        fixed_state = _intent_state("recommend", awaiting=["child_age", "income"])

        collect_slots_called = []

        async def _fake_collect(state: dict) -> dict:
            collect_slots_called.append(True)
            return {
                **state,
                "slot_request": {"field": "child_age", "question": "자녀 나이를 알려주세요."},
                "branch_content": "자녀 나이를 알려주세요.",
                "branch_policies": [],
                "branch_evidences": [],
            }

        monkeypatch.setattr(_ch, "handle_collect_slots", _fake_collect)

        asyncio.run(_run_with_patched_classify(monkeypatch, fixed_state))

        assert collect_slots_called, "handle_collect_slots가 호출되어야 한다"


# ─── Layer 3: Follow-Up 분류 테스트 ─────────────────────────────────────────

class TestFollowUpIntentClassification:
    def _mock_follow_up_llm(
        self, monkeypatch: pytest.MonkeyPatch, intent: str
    ) -> None:
        from app.services.chat.ai import _follow_up as _fu

        structured = AsyncMock()
        structured.ainvoke = AsyncMock(
            return_value=SimpleNamespace(intent=intent)
        )
        base = MagicMock()
        base.with_structured_output = MagicMock(return_value=structured)
        monkeypatch.setattr(_fu, "_FOLLOW_UP_LLM", base)

    def test_m28_follow_up_general(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M28: 실제 답변 → classify_follow_up_intent=general"""
        from app.services.chat.ai._follow_up import classify_follow_up_intent

        self._mock_follow_up_llm(monkeypatch, "general")

        follow_up_policy = {
            "policy_name": "부모급여",
            "follow_up_questions": [{"question": "child_age", "label": "자녀 나이"}],
            "eligibility_status": "FOLLOW_UP_REQUIRED",
        }
        result = asyncio.run(classify_follow_up_intent(
            follow_up_policy=follow_up_policy,
            history=[{"role": "assistant", "content": "추가 정보가 필요해요."}],
            user_content="아이가 3개월이에요",
        ))

        assert result == "general"

    def test_m29_follow_up_eligibility_clarification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M29: '뭐가 부족해?' → classify_follow_up_intent=eligibility_clarification"""
        from app.services.chat.ai._follow_up import classify_follow_up_intent

        self._mock_follow_up_llm(monkeypatch, "eligibility_clarification")

        follow_up_policy = {
            "policy_name": "부모급여",
            "follow_up_questions": [{"question": "child_age", "label": "자녀 나이"}],
        }
        result = asyncio.run(classify_follow_up_intent(
            follow_up_policy=follow_up_policy,
            history=[],
            user_content="뭐가 부족하다는 거야?",
        ))

        assert result == "eligibility_clarification"

    def test_m30_follow_up_recommendation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M30: '다른 거 추천해줘' → classify_follow_up_intent=recommendation"""
        from app.services.chat.ai._follow_up import classify_follow_up_intent

        self._mock_follow_up_llm(monkeypatch, "recommendation")

        follow_up_policy = {
            "policy_name": "부모급여",
            "follow_up_questions": [],
        }
        result = asyncio.run(classify_follow_up_intent(
            follow_up_policy=follow_up_policy,
            history=[],
            user_content="그 정책 말고 다른 거 추천해줘",
        ))

        assert result == "recommendation"


# ─── 경계 조건: validate_branch_result 직접 검증 ─────────────────────────────

class TestQualityValidator:
    def test_empty_content_triggers_fallback(self) -> None:
        """빈 content → _SAFE_FALLBACK_CONTENT로 대체"""
        from app.services.chat.handlers._quality_validator import validate_branch_result

        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "recommend"},
            "branch_content": "",
            "branch_policies": [],
            "slot_request": None,
            "profile_confirm": None,
            "branch_suggested_actions": [],
        }
        result = validate_branch_result(state, "recommend")

        assert result is not None
        assert "empty_content" in result["_validation_issues"]
        assert result["branch_content"]

    def test_assertive_phrase_in_eligibility_without_result_issues_warning(self) -> None:
        """eligibility + 확정 표현 + eligibility_result 없음 → 경고 issue 추가"""
        from app.services.chat.handlers._quality_validator import validate_branch_result

        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "eligibility"},
            "branch_content": "부모급여를 받을 수 있습니다.",
            "branch_policies": [{"slug": "bomo"}],
            "branch_eligibility_result": None,
            "branch_policy_candidates": None,
            "slot_request": None,
            "profile_confirm": None,
            "branch_suggested_actions": [],
        }
        result = validate_branch_result(state, "eligibility")

        assert result is not None
        assert "assertive_without_eligibility_result" in result["_validation_issues"]

    def test_duplicate_suggested_action_removed(self) -> None:
        """suggested_actions에 primary intent와 동일 항목 → 제거"""
        from app.services.chat.handlers._quality_validator import validate_branch_result

        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "eligibility"},
            "branch_content": "자격 확인 결과입니다.",
            "branch_policies": [],
            "branch_eligibility_result": {"status": "ELIGIBLE"},
            "branch_evidences": [
                {
                    "chunk_id": 990001,
                    "snippet": "평가용 자격 근거",
                    "source_url": "fixture://mock-scenarios/eligibility-001",
                }
            ],
            "slot_request": None,
            "profile_confirm": None,
            "branch_suggested_actions": ["eligibility", "apply"],
        }
        result = validate_branch_result(state, "eligibility")

        assert result is not None
        assert "eligibility" not in result["branch_suggested_actions"]
        assert "apply" in result["branch_suggested_actions"]

    def test_no_issues_returns_none(self) -> None:
        """문제 없으면 None 반환"""
        from app.services.chat.handlers._quality_validator import validate_branch_result

        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "policy_summary"},
            "branch_content": "부모급여는 만 0~23개월 아이를 키우는 부모에게 지원됩니다.",
            "branch_policies": [],
            "slot_request": None,
            "profile_confirm": None,
            "branch_suggested_actions": [],
        }
        result = validate_branch_result(state, "policy_summary")

        assert result is None

    def test_slot_request_skips_validation(self) -> None:
        """slot_request 있으면 검증 생략 → None 반환"""
        from app.services.chat.handlers._quality_validator import validate_branch_result

        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "recommend"},
            "branch_content": "",
            "slot_request": {"field": "child_age", "question": "나이 알려주세요"},
            "profile_confirm": None,
            "branch_suggested_actions": [],
        }
        result = validate_branch_result(state, "recommend")

        assert result is None


# ─── payload builder 직접 검증 ───────────────────────────────────────────────

class TestPayloadBuilder:
    def test_policy_selection_built_when_candidates_present(self) -> None:
        """branch_policy_candidates 있으면 policy_selection 구조 생성"""
        from app.services.chat.handlers._payload_builders import build_assistant_payload

        candidates = [
            {"slug": "bomo-gupyeo", "policy_name": "부모급여"},
            {"slug": "cheot-mannam", "policy_name": "첫만남이용권"},
        ]
        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "eligibility"},
            "branch_content": "어떤 정책을 말씀하시는 건가요?",
            "branch_policies": [],
            "branch_evidences": [],
            "branch_policy_candidates": candidates,
            "branch_suggested_actions": [],
            "slot_request": None,
            "profile_confirm": None,
        }
        result = asyncio.run(build_assistant_payload(state))

        ps = result["assistant_payload"]["policy_selection"]
        assert ps is not None
        assert len(ps["candidates"]) == 2
        assert ps["intent"] == "eligibility"

    def test_apply_card_in_payload(self) -> None:
        """branch_apply_card 있으면 payload.apply_card 설정"""
        from app.services.chat.handlers._payload_builders import build_assistant_payload

        card = {"policy_name": "부모급여", "apply_url": "https://example.com"}
        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "apply"},
            "branch_content": "신청 방법입니다.",
            "branch_policies": [],
            "branch_evidences": [],
            "branch_apply_card": card,
            "branch_suggested_actions": [],
            "slot_request": None,
            "profile_confirm": None,
        }
        result = asyncio.run(build_assistant_payload(state))

        assert result["assistant_payload"]["apply_card"] == card

    def test_suggested_actions_from_state(self) -> None:
        """branch_suggested_actions → payload.suggested_actions 전달"""
        from app.services.chat.handlers._payload_builders import build_assistant_payload

        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "eligibility"},
            "branch_content": "자격 확인 결과입니다.",
            "branch_policies": [],
            "branch_evidences": [],
            "branch_eligibility_result": {"status": "ELIGIBLE"},
            "branch_suggested_actions": ["apply"],
            "slot_request": None,
            "profile_confirm": None,
        }
        result = asyncio.run(build_assistant_payload(state))

        assert "apply" in result["assistant_payload"]["suggested_actions"]

    def test_disclaimer_set_on_assertive_eligibility_phrase(self) -> None:
        """자격 확정 표현 + eligibility_result 없음 → disclaimer=True"""
        from app.services.chat.handlers._payload_builders import build_assistant_payload

        state: dict[str, Any] = {
            "supervisor_decision": {"intent": "eligibility"},
            "branch_content": "부모급여를 받을 수 있습니다.",
            "branch_policies": [],
            "branch_evidences": [],
            "branch_eligibility_result": None,
            "branch_suggested_actions": [],
            "slot_request": None,
            "profile_confirm": None,
        }
        result = asyncio.run(build_assistant_payload(state))

        assert result["assistant_payload"]["disclaimer"] is True
