"""
E2E tests for the chat direct-routing pipeline (_run_chat).

These tests exercise the full chain:
  classify_intent → branch handler → build_assistant_payload
                 → extract_evidences → extract_policy_links

Only external I/O boundaries are mocked (LLM calls, graph runners, DB sessions).
The routing switch, payload builder, and extraction helpers run for real.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.nodes.chat import chat_nodes as _chat_nodes_module
from app.services import chat_handlers as _handlers_module
from app.services.chat.ai import _graph_clients, _lifecycle_runners
from app.services.chat.handlers import _handler_recommend
from app.services.chat_service import _run_chat


# ---------------------------------------------------------------------------
# Common fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeSession:
    """Async context-manager DB stub that accepts any SQL execution."""

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, *args: Any, **kwargs: Any) -> MagicMock:
        return MagicMock()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _fake_db() -> _FakeSession:
    return _FakeSession()


class _FakeRagResult:
    def __init__(self, results: list[Any]) -> None:
        self.results = results


class _FakeRagService:
    def __init__(self, chunks: list[Any] | None = None) -> None:
        self.search = AsyncMock(return_value=_FakeRagResult(chunks or []))


def _rag_chunk(*, chunk_id: int, policy_code: str, policy_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_id=chunk_id,
        policy_code=policy_code,
        policy_name=policy_name,
        source_url="https://example.com",
        chunk_text="정책 관련 내용",
    )


def _mock_llm(monkeypatch: pytest.MonkeyPatch, response: str) -> None:
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(return_value=SimpleNamespace(content=response))
    monkeypatch.setattr(_chat_nodes_module, "_llm", lambda: llm)


def _intent_state(intent: str, *, resolved_slug: str | None = None) -> dict[str, Any]:
    decision: dict[str, Any] = {"intent": intent, "raw": "{}"}
    if resolved_slug:
        decision["resolved_policy_slug"] = resolved_slug
    return {
        "user_id": 1,
        "user_content": "테스트 메시지",
        "history": [],
        "slot": {},
        "profile": {},
        "pending_intent": None,
        "awaiting_slots": [],
        "profile_confirm": None,
        "supervisor_decision": decision,
    }


async def _run(
    *,
    monkeypatch: pytest.MonkeyPatch,
    intent_state: dict[str, Any],
) -> dict[str, Any]:
    """
    Patch classify_intent to return a fixed state, then run _run_chat.
    """
    async def _fake_classify(**kwargs: Any) -> dict[str, Any]:
        return intent_state

    monkeypatch.setattr(_handlers_module, "classify_intent", _fake_classify)

    return await _run_chat(
        db=object(),
        user_id=1,
        user_content="테스트 메시지",
        history=[],
        slot={},
        recent_assistant_policy=None,
    )


# ---------------------------------------------------------------------------
# 1. unclear intent
# ---------------------------------------------------------------------------


def test_e2e_unclear_returns_payload_without_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_llm(monkeypatch, "무엇을 도와드릴까요?")
    monkeypatch.setattr(_graph_clients, "_RAG_SERVICE", _FakeRagService())

    result = asyncio.run(
        _run(monkeypatch=monkeypatch, intent_state=_intent_state("unclear"))
    )

    payload = result["assistant_payload"]
    assert payload["content"] == "무엇을 도와드릴까요?"
    assert payload["policies"] == []
    assert payload["disclaimer"] is False
    assert payload["actions"] == []
    assert result["evidences_to_save"] == []
    assert result["policy_links_to_save"] == []


# ---------------------------------------------------------------------------
# 2. recommend intent (full recommend → payload → evidence/link extraction)
# ---------------------------------------------------------------------------


def test_e2e_recommend_routes_and_builds_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_llm(monkeypatch, "맞춤 정책을 추천해드릴게요.")

    recommend_result = {
        "results": [
            {
                "policy_id": "100",
                "slug": "WLF1",
                "policy_name": "임신·출산 진료비",
                "summary": "임신부 대상 지원",
                "evidence": [
                    {
                        "chunk_id": 11,
                        "snippet": "임신부 신청 가능",
                        "source_title": "임신·출산 진료비",
                        "source_url": "https://example.com",
                        "evidence_role": "TARGET",
                    }
                ],
            }
        ]
    }

    from app.common.ai_status import RequestStatus

    fake_snapshot = SimpleNamespace(
        request_id="1",
        status=RequestStatus.COMPLETED,
        result_json=recommend_result,
        merged_condition_json={},
        questions=[],
    )

    async def _fake_lifecycle(*args: Any, **kwargs: Any) -> tuple:
        return fake_snapshot, None

    monkeypatch.setattr(_handler_recommend, "_run_recommendation_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(_graph_clients, "_RAG_SERVICE", _FakeRagService())

    result = asyncio.run(
        _run(
            monkeypatch=monkeypatch,
            intent_state=_intent_state("recommend"),
        )
    )

    payload = result["assistant_payload"]
    assert payload["content"] == "맞춤 정책을 추천해드릴게요."
    assert payload["policies"][0]["slug"] == "WLF1"
    assert payload["actions"] == ["recommend"]
    assert payload["disclaimer"] is False
    assert result["evidences_to_save"][0]["chunk_id"] == 11
    assert result["policy_links_to_save"][0] == {
        "policy_slug": "WLF1",
        "action_type": "RECOMMENDED",
    }


# ---------------------------------------------------------------------------
# 3. eligibility intent (resolved slug path)
# ---------------------------------------------------------------------------


def test_e2e_eligibility_resolved_slug_runs_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.common.ai_status import RequestStatus

    monkeypatch.setattr(_lifecycle_runners, "AsyncSessionLocal", _fake_db)

    eligibility_return = {
        "status": RequestStatus.COMPLETED.value,
        "slug": "WLF1",
        "policy_name": "임신·출산 진료비",
        "user_status": "eligible",
        "summary": "지원 대상입니다.",
        "request_id": 99,
        "follow_up_questions": [],
        "evidences": [
            {
                "chunk_id": 55,
                "snippet": "임신부 대상",
                "source_title": "임신·출산 진료비",
                "source_url": "https://example.com",
                "evidence_role": "TARGET",
            }
        ],
    }
    eligibility_graph = MagicMock()
    eligibility_graph.run = AsyncMock(return_value=eligibility_return)

    state = _intent_state("eligibility", resolved_slug="WLF1")
    state["slot"] = {
        "recent_policies": [
            {"slug": "WLF1", "policy_name": "임신·출산 진료비", "last_action": "RECOMMENDED"}
        ]
    }

    monkeypatch.setattr(_graph_clients, "_ELIGIBILITY_GRAPH", eligibility_graph)
    monkeypatch.setattr(_graph_clients, "_RAG_SERVICE", _FakeRagService())

    result = asyncio.run(
        _run(monkeypatch=monkeypatch, intent_state=state)
    )

    eligibility_graph.run.assert_awaited_once()
    payload = result["assistant_payload"]
    assert "지원 대상입니다" in payload["content"]
    assert payload["actions"] == ["eligibility"]
    assert payload["disclaimer"] is False
    assert result["evidences_to_save"][0]["chunk_id"] == 55
    assert result["policy_links_to_save"][0]["action_type"] == "ELIGIBILITY_TARGET"


# ---------------------------------------------------------------------------
# 4. compare intent
# ---------------------------------------------------------------------------


def test_e2e_compare_routes_and_builds_diff_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_lifecycle_runners, "AsyncSessionLocal", _fake_db)

    compare_return = {
        "policy_a": {"policy_id": "1", "slug": "WLF1", "name": "A 정책",
                      "summary": {"condition": "A 조건"}},
        "policy_b": {"policy_id": "2", "slug": "WLF2", "name": "B 정책",
                      "summary": {"condition": "B 조건"}},
        "diff_table": [{"field": "대상", "a": "임신부", "b": "영유아 부모"}],
        "selection_guide": "A가 임신 중에, B는 출산 후에 유리해요.",
        "related_policies": [],
    }
    comparison_graph = MagicMock()
    comparison_graph.run = AsyncMock(return_value=compare_return)

    state = _intent_state("compare")
    state["user_content"] = "이 둘 비교해줘"
    state["slot"] = {
        "recent_policies": [
            {"slug": "WLF1", "policy_name": "A 정책", "last_action": "RECOMMENDED"},
            {"slug": "WLF2", "policy_name": "B 정책", "last_action": "RECOMMENDED"},
        ]
    }

    monkeypatch.setattr(_graph_clients, "_COMPARISON_GRAPH", comparison_graph)
    monkeypatch.setattr(
        _graph_clients,
        "_RAG_SERVICE",
        _FakeRagService(
            [
                _rag_chunk(chunk_id=1, policy_code="WLF1", policy_name="A 정책"),
                _rag_chunk(chunk_id=2, policy_code="WLF2", policy_name="B 정책"),
            ]
        ),
    )

    result = asyncio.run(
        _run(monkeypatch=monkeypatch, intent_state=state)
    )

    comparison_graph.run.assert_awaited_once()
    payload = result["assistant_payload"]
    assert "근거를 확인하지 못해" in payload["content"]
    assert payload["policies"] == []
    assert payload["actions"] == ["compare"]
    assert payload["disclaimer"] is True
    assert payload["evidence_review"]["verdict"] == "BLOCKED"
    assert result["policy_links_to_save"] == []


# ---------------------------------------------------------------------------
# 5. collect_slots flow (awaiting_slots set → no branch executed)
# ---------------------------------------------------------------------------


def test_e2e_collect_slots_emits_slot_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _intent_state("recommend")
    state["awaiting_slots"] = ["child_age"]

    result = asyncio.run(
        _run(monkeypatch=monkeypatch, intent_state=state)
    )

    payload = result["assistant_payload"]
    assert payload["slot_request"] is not None
    assert payload["slot_request"]["flow_type"] == "recommend"
    assert payload["disclaimer"] is False
    assert payload["actions"] == []


# ---------------------------------------------------------------------------
# 6. profile_confirm flow
# ---------------------------------------------------------------------------


def test_e2e_profile_confirm_emits_confirm_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _intent_state("recommend")
    state["profile_confirm"] = {
        "summary": ["생애단계: 영유아", "소득: 하위 50%"],
        "options": [
            {"label": "네, 이 정보로 추천해줘", "value": "yes"},
            {"label": "아니요, 다시 입력할게요", "value": "no"},
        ],
    }

    result = asyncio.run(
        _run(monkeypatch=monkeypatch, intent_state=state)
    )

    payload = result["assistant_payload"]
    assert payload["profile_confirm"] is not None
    assert "생애단계: 영유아" in payload["content"]
    assert payload["disclaimer"] is False
    assert payload["actions"] == []


# ---------------------------------------------------------------------------
# 7. routing error → fallback payload
# ---------------------------------------------------------------------------


def test_e2e_exception_in_classify_returns_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _exploding_classify(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulate classify failure")

    monkeypatch.setattr(_handlers_module, "classify_intent", _exploding_classify)

    result = asyncio.run(
        _run_chat(
            db=object(),
            user_id=1,
            user_content="테스트",
            history=[],
            slot={},
            recent_assistant_policy=None,
        )
    )

    payload = result["assistant_payload"]
    assert "문제가 발생" in payload["content"]
    assert result["evidences_to_save"] == []
    assert result["policy_links_to_save"] == []


# ---------------------------------------------------------------------------
# 8. apply intent (no slug → clarification fallback)
# ---------------------------------------------------------------------------


def test_e2e_apply_no_slug_returns_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_graph_clients, "_RAG_SERVICE", _FakeRagService())

    state = _intent_state("apply")

    result = asyncio.run(
        _run(monkeypatch=monkeypatch, intent_state=state)
    )

    payload = result["assistant_payload"]
    assert payload["apply_card"] is None
    assert payload["policies"] == []
