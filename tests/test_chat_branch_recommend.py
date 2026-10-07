import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.nodes.chat.chat_nodes import (
    _adapt_recommendation_result,
)
from app.common.ai_status import RequestStatus
from app.schemas.ai_contract import ConditionResult, FollowUpCandidate
from app.services import chat_handlers
from app.services.ai_request_lifecycle_service import AiRequestLifecycleService
from app.services.chat_handlers import handle_recommend


def _result_json() -> dict[str, Any]:
    return {
        "results": [
            {
                "policy_id": "100",
                "policy_code": "WLF1",
                "slug": "WLF1",
                "policy_name": "임신·출산 진료비",
                "summary": "임신부 대상 진료비 지원",
                "benefit_summary": "최대 100만원 바우처",
                "match_score": 0.81,
                "reason": "대상연령 일치",
                "evidence": [
                    {
                        "chunk_id": 11,
                        "policy_id": "100",
                        "snippet": "임신부 누구나 신청할 수 있습니다.",
                        "source_title": "임신·출산 진료비",
                        "source_url": "https://example.com/100",
                        "evidence_role": "TARGET",
                    }
                ],
            },
            {
                "policy_id": "101",
                "policy_code": "WLF2",
                "slug": "WLF2",
                "policy_name": "산모·신생아 건강관리",
                "summary": "산후 도우미 비용 일부 지원",
                "benefit_summary": "본인부담금 차등",
                "match_score": 0.62,
                "reason": "소득 구간 일치",
                "evidence": [
                    {
                        "chunk_id": 22,
                        "policy_id": "101",
                        "snippet": "기준 중위소득 150% 이하",
                        "source_title": "산모·신생아 건강관리",
                        "source_url": "https://example.com/101",
                        "evidence_role": "TARGET",
                    }
                ],
            },
        ],
    }


def _request_namespace() -> SimpleNamespace:
    return SimpleNamespace(
        request_id=1,
        user_id=7,
        source_type="CHAT",
        source_ref_id=None,
        raw_query=None,
        parsed_query_json={},
        merged_condition_json={},
        profile_conflict_json=[],
        result_json={},
        request_status=RequestStatus.READY.value,
        error_message=None,
        execution_token=None,
        policy_id=None,
    )


class _FakeRepository:
    def __init__(self, request: SimpleNamespace) -> None:
        self.request = request

    async def ensure_request_schema(self, db: Any) -> None:
        return None

    async def create(
        self,
        db: Any,
        request_type: str,
        user_id: int,
        source_type: str,
        source_ref_id: str | None = None,
        raw_query: str | None = None,
        selected_conditions: dict[str, Any] | None = None,
        follow_up_resolved: bool = False,
        policy_id: int | None = None,
    ) -> SimpleNamespace:
        self.request.user_id = user_id
        self.request.source_type = source_type
        self.request.raw_query = raw_query
        if follow_up_resolved:
            self.request.parsed_query_json = {
                **(self.request.parsed_query_json or {}),
                "follow_up_resolved": True,
            }
        return self.request

    async def find_by_id(
        self, db: Any, request_type: str, request_id: int
    ) -> SimpleNamespace:
        return self.request

    async def update_status(
        self,
        db: Any,
        request: SimpleNamespace,
        status: RequestStatus,
        error_message: str | None = None,
        execution_token: str | None = None,
    ) -> SimpleNamespace:
        request.request_status = status.value
        if status == RequestStatus.PROCESSING:
            request.execution_token = None
        if error_message:
            request.error_message = error_message
        return request

    async def update_payload(
        self,
        db: Any,
        request: SimpleNamespace,
        parsed_query_json: dict[str, Any] | None = None,
        merged_condition_json: dict[str, Any] | None = None,
        profile_conflict_json: list[dict[str, Any]] | None = None,
        execution_token: str | None = None,
    ) -> SimpleNamespace:
        request.parsed_query_json = parsed_query_json
        request.merged_condition_json = merged_condition_json
        request.profile_conflict_json = profile_conflict_json
        return request

    async def update_result(
        self,
        db: Any,
        request: SimpleNamespace,
        result_json: dict[str, Any],
        execution_token: str | None = None,
    ) -> SimpleNamespace:
        request.result_json = result_json
        return request

    async def claim_recommendation_execution(
        self, db: Any, request_id: int
    ) -> str | None:
        if self.request.execution_token is not None:
            return None
        self.request.execution_token = "test-execution-token"
        return self.request.execution_token


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return MagicMock()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.fixture
def patched_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_handlers, "AsyncSessionLocal", lambda: _FakeSession())


@pytest.fixture
def patched_llm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from app.ai.nodes.chat import chat_nodes
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(
        return_value=SimpleNamespace(content="추천 정책을 안내해 드릴게요.")
    )
    monkeypatch.setattr(chat_nodes, "_llm", lambda: llm)
    return llm


@pytest.fixture
def patched_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_profile(self: Any, db: Any, user_id: int) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(
        AiRequestLifecycleService, "_profile_snapshot", _empty_profile
    )


def _build_lifecycle(
    *,
    runner: MagicMock,
    follow_up: bool = False,
) -> tuple[AiRequestLifecycleService, _FakeRepository, MagicMock]:
    request = _request_namespace()
    repository = _FakeRepository(request)
    condition_agent = MagicMock()
    follow_up_candidates = (
        [FollowUpCandidate(field_name="region", question_text="어디 사세요?")]
        if follow_up
        else []
    )
    condition_agent.analyze = AsyncMock(
        return_value=ConditionResult(
            parsed_query_json={"selected_conditions": None},
            merged_condition_json={"stage": "PREGNANCY"},
            input_issues=[],
            profile_conflicts=[],
            follow_up_candidates=follow_up_candidates,
        )
    )
    service = AiRequestLifecycleService(
        repository=repository,
        condition_agent=condition_agent,
        recommendation_graph=runner,
    )
    return service, repository, condition_agent


def _state() -> dict[str, Any]:
    return {
        "user_id": 7,
        "user_content": "맞벌이 가구에 맞는 정책 추천해줘",
        "history": [],
    }


def test_adapt_recommendation_result_picks_slugs_and_chunks() -> None:
    policies, evidences = _adapt_recommendation_result(_result_json())

    assert [p["slug"] for p in policies] == ["WLF1", "WLF2"]
    assert [p["policy_name"] for p in policies] == [
        "임신·출산 진료비",
        "산모·신생아 건강관리",
    ]
    assert [e["chunk_id"] for e in evidences] == [11, 22]
    assert evidences[0]["source_title"] == "임신·출산 진료비"


def test_adapt_recommendation_result_deduplicates_chunks() -> None:
    duplicate = {
        "results": [
            {
                "policy_id": "100",
                "slug": "WLF1",
                "policy_name": "A",
                "summary": "a",
                "evidence": [
                    {"chunk_id": 1, "snippet": "x", "source_title": "A"},
                    {"chunk_id": 1, "snippet": "x", "source_title": "A"},
                ],
            }
        ]
    }

    _, evidences = _adapt_recommendation_result(duplicate)
    assert [e["chunk_id"] for e in evidences] == [1]


def test_branch_recommend_invokes_graph_and_adapts_payload(
    patched_session: None,
    patched_llm: AsyncMock,
    patched_profile: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_result_json())
    lifecycle, _, condition_agent = _build_lifecycle(runner=runner)
    monkeypatch.setattr(chat_handlers, "_LIFECYCLE_SERVICE", lifecycle)

    result = asyncio.run(handle_recommend(_state()))

    runner.run.assert_awaited_once()
    condition_agent.analyze.assert_awaited_once()
    call_kwargs = runner.run.await_args.kwargs
    assert call_kwargs["merged_condition_json"] == {"stage": "PREGNANCY"}

    assert result["branch_content"] == "추천 정책을 안내해 드릴게요."
    assert [p["slug"] for p in result["branch_policies"]] == ["WLF1", "WLF2"]
    assert [e["chunk_id"] for e in result["branch_evidences"]] == [11, 22]


def test_branch_recommend_follow_up_returns_fallback(
    patched_session: None,
    patched_llm: AsyncMock,
    patched_profile: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_result_json())
    lifecycle, _, _ = _build_lifecycle(runner=runner, follow_up=True)
    monkeypatch.setattr(chat_handlers, "_LIFECYCLE_SERVICE", lifecycle)

    result = asyncio.run(handle_recommend(_state()))

    runner.run.assert_not_awaited()
    assert result["branch_content"] == "맞춤 추천을 위해 정보가 조금 더 필요해요."
    assert result["slot_request"]["flow_type"] == "recommend"
    assert result["slot_request"]["fields"][0]["key"] == "region"
    assert result["pending"]["intent"] == "recommend"
    assert result["branch_policies"] == []
    assert result["branch_evidences"] == []


def test_branch_recommend_graph_failure_returns_fallback(
    patched_session: None,
    patched_llm: AsyncMock,
    patched_profile: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.run = AsyncMock(side_effect=RuntimeError("graph blew up"))
    lifecycle, _, _ = _build_lifecycle(runner=runner)
    monkeypatch.setattr(chat_handlers, "_LIFECYCLE_SERVICE", lifecycle)

    mark_failed_calls: list[tuple[int, str]] = []

    async def _capture_mark_failed(
        request_id: int,
        error_message: str,
        execution_token: str | None = None,
    ) -> None:
        mark_failed_calls.append((request_id, error_message))

    monkeypatch.setattr(
        chat_handlers, "_mark_recommendation_failed", _capture_mark_failed
    )

    result = asyncio.run(handle_recommend(_state()))

    assert runner.run.await_count == 2  # initial attempt + 1 retry
    assert mark_failed_calls and mark_failed_calls[0][0] == 1
    assert "잠시 후 다시" in result["branch_content"]
    assert result["branch_policies"] == []
    assert result["branch_evidences"] == []
