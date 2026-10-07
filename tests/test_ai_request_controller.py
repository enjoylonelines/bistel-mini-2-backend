from collections.abc import AsyncGenerator
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import app.api.ai_request_controller as ai_request_controller
from app.ai.utils.progress import emit_progress
from app.api.ai_request_controller import eligibility_router
from app.common.exceptions import register_exception_handlers
from app.core.dependencies import get_current_user
from app.db.session import get_db_session
from app.schemas.ai_contract import ConditionResult, EvidenceChunk, RequestStatus
from app.schemas.ai_request_schema import EligibilityResultResponse
from app.services.ai_request_lifecycle_service import (
    AiRequestLifecycleService,
    RequestExecutionOwnershipLost,
)


def test_recommendation_runner_rejects_stale_execution_token() -> None:
    request = SimpleNamespace(
        request_id=123,
        request_status=RequestStatus.CANCELLED.value,
        execution_token="current-owner",
    )

    class FakeRepository:
        async def find_by_id(self, db, request_type, request_id):
            return request

    service = AiRequestLifecycleService(repository=FakeRepository())

    import asyncio

    with pytest.raises(RequestExecutionOwnershipLost):
        asyncio.run(
            service.process_condition_request(
                db=SimpleNamespace(),
                request_type="recommendation",
                request_id=123,
                execution_token="stale-owner",
            )
        )


def test_cancel_recommendation_marks_durable_terminal_status() -> None:
    request = SimpleNamespace(
        request_id=123,
        request_status=RequestStatus.PROCESSING.value,
        user_id=7,
        source_type="FORM",
        source_ref_id=None,
        parsed_query_json={},
        merged_condition_json={},
        profile_conflict_json=[],
        result_json=None,
        error_message=None,
    )

    class FakeRepository:
        async def find_by_id(self, db, request_type, request_id):
            return request

        async def update_status(self, db, request, status, error_message=None):
            request.request_status = status.value
            request.error_message = error_message
            return request

    service = AiRequestLifecycleService(repository=FakeRepository())

    import asyncio

    snapshot = asyncio.run(
        service.cancel_recommendation_request(
            db=SimpleNamespace(),
            request_id=123,
            user_id=7,
        )
    )

    assert snapshot.status == RequestStatus.CANCELLED


def test_create_eligibility_request_uses_common_lifecycle(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_db() -> AsyncGenerator[object, None]:
        db = SimpleNamespace(commit=lambda: None)

        async def commit():
            captured["committed"] = True

        db.commit = commit
        yield db

    async def fake_current_user() -> object:
        return SimpleNamespace(user_id=7)

    async def fake_create_eligibility_request(
        self,
        db,
        *,
        user_id,
        policy_identifier,
        source_type,
        source_ref_id,
        raw_query,
        selected_conditions,
    ):
        captured["create"] = {
            "user_id": user_id,
            "policy_identifier": policy_identifier,
            "source_type": source_type,
            "source_ref_id": source_ref_id,
            "raw_query": raw_query,
            "selected_conditions": selected_conditions,
        }
        return SimpleNamespace(
            request_id="123",
            status=SimpleNamespace(value="READY"),
        )

    async def fake_mark_processing(self, db, request_type, request_id):
        captured["mark_processing"] = {
            "request_type": request_type,
            "request_id": request_id,
        }
        return SimpleNamespace(
            request_id=str(request_id),
            status=SimpleNamespace(value="PROCESSING"),
        )

    async def fake_process_ai_condition_request(
        request_type: str,
        request_id: int,
    ) -> None:
        captured["background"] = {
            "request_type": request_type,
            "request_id": request_id,
        }

    monkeypatch.setattr(
        AiRequestLifecycleService,
        "create_eligibility_request",
        fake_create_eligibility_request,
    )
    monkeypatch.setattr(
        AiRequestLifecycleService,
        "mark_processing",
        fake_mark_processing,
    )
    monkeypatch.setattr(
        ai_request_controller,
        "process_ai_condition_request",
        fake_process_ai_condition_request,
    )

    app = FastAPI()
    app.include_router(eligibility_router)
    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_current_user
    register_exception_handlers(app)

    user_conditions = {
        "stage": "newborn",
        "child_age": "0",
        "income": "mid1",
        "region": "seoul",
        "special": ["many"],
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/eligibility/requests",
            json={
                "policy_id": "WLF00000024",
                "user_conditions": user_conditions,
                "source_ref_id": "WLF00000024",
            },
        )

    assert response.status_code == 202
    body = response.json()
    assert body["success"] is True
    assert body["data"]["request_id"] == "123"
    assert body["data"]["status"]["value"] == "PROCESSING"
    assert body["meta"] == {
        "request_id": "123",
        "follow_up_required": False,
    }
    assert captured["create"] == {
        "user_id": 7,
        "policy_identifier": "WLF00000024",
        "source_type": "POLICY_DETAIL",
        "source_ref_id": "WLF00000024",
        "raw_query": None,
        "selected_conditions": user_conditions,
    }
    assert captured["mark_processing"] == {
        "request_type": "eligibility",
        "request_id": 123,
    }
    assert captured["committed"] is True
    assert captured["background"] == {
        "request_type": "eligibility",
        "request_id": 123,
    }


def test_create_eligibility_request_accepts_manual_confirmations(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_db() -> AsyncGenerator[object, None]:
        db = SimpleNamespace()

        async def commit():
            captured["committed"] = True

        db.commit = commit
        yield db

    async def fake_current_user() -> object:
        return SimpleNamespace(user_id=7)

    async def fake_create_eligibility_request(
        self,
        db,
        *,
        user_id,
        policy_identifier,
        source_type,
        source_ref_id,
        raw_query,
        selected_conditions,
    ):
        captured["selected_conditions"] = selected_conditions
        return SimpleNamespace(
            request_id="123",
            status=SimpleNamespace(value="READY"),
        )

    async def fake_mark_processing(self, db, request_type, request_id):
        return SimpleNamespace(
            request_id=str(request_id),
            status=SimpleNamespace(value="PROCESSING"),
        )

    async def fake_process_ai_condition_request(
        request_type: str,
        request_id: int,
    ) -> None:
        return None

    monkeypatch.setattr(
        AiRequestLifecycleService,
        "create_eligibility_request",
        fake_create_eligibility_request,
    )
    monkeypatch.setattr(
        AiRequestLifecycleService,
        "mark_processing",
        fake_mark_processing,
    )
    monkeypatch.setattr(
        ai_request_controller,
        "process_ai_condition_request",
        fake_process_ai_condition_request,
    )

    app = FastAPI()
    app.include_router(eligibility_router)
    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_current_user
    register_exception_handlers(app)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/eligibility/requests",
            json={
                "policy_id": "WLF00000024",
                "user_conditions": {"income": "low"},
                "manual_confirmations": [
                    {
                        "question": "채무 상황 조건 확인이 필요해요.",
                        "answer": "yes",
                        "note": "챗봇 추가 답변",
                    }
                ],
            },
        )

    assert response.status_code == 202
    assert captured["selected_conditions"] == {
        "income": "low",
        "manual_confirmations": [
            {
                "question": "채무 상황 조건 확인이 필요해요.",
                "answer": "yes",
                "note": "챗봇 추가 답변",
                "source": None,
            }
        ],
    }


def test_create_eligibility_request_preserves_chat_session_source(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_db() -> AsyncGenerator[object, None]:
        db = SimpleNamespace()

        async def commit():
            captured["committed"] = True

        db.commit = commit
        yield db

    async def fake_current_user() -> object:
        return SimpleNamespace(user_id=7)

    async def fake_ensure_owned_session(db, *, user_id, chat_session_id):
        captured["ensure_session"] = {
            "user_id": user_id,
            "chat_session_id": chat_session_id,
        }

    async def fake_create_eligibility_request(
        self,
        db,
        *,
        user_id,
        policy_identifier,
        source_type,
        source_ref_id,
        raw_query,
        selected_conditions,
    ):
        captured["source_ref_id"] = source_ref_id
        return SimpleNamespace(
            request_id="123",
            status=SimpleNamespace(value="READY"),
        )

    async def fake_mark_processing(self, db, request_type, request_id):
        return SimpleNamespace(
            request_id=str(request_id),
            status=SimpleNamespace(value="PROCESSING"),
        )

    async def fake_process_ai_condition_request(
        request_type: str,
        request_id: int,
    ) -> None:
        return None

    monkeypatch.setattr(
        ai_request_controller.ChatService,
        "ensure_owned_session",
        fake_ensure_owned_session,
    )
    monkeypatch.setattr(
        AiRequestLifecycleService,
        "create_eligibility_request",
        fake_create_eligibility_request,
    )
    monkeypatch.setattr(
        AiRequestLifecycleService,
        "mark_processing",
        fake_mark_processing,
    )
    monkeypatch.setattr(
        ai_request_controller,
        "process_ai_condition_request",
        fake_process_ai_condition_request,
    )

    app = FastAPI()
    app.include_router(eligibility_router)
    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_current_user
    register_exception_handlers(app)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/eligibility/requests",
            json={
                "policy_id": "WLF00000024",
                "chat_session_id": "82",
                "source_ref_id": "recommendation:123",
                "user_conditions": {"income": "low"},
            },
        )

    assert response.status_code == 202
    assert captured["ensure_session"] == {
        "user_id": 7,
        "chat_session_id": 82,
    }
    assert captured["source_ref_id"] == "chat_session:82;source:recommendation:123"


def test_create_eligibility_request_rejects_non_numeric_chat_session_id() -> None:
    async def fake_db() -> AsyncGenerator[object, None]:
        yield SimpleNamespace()

    async def fake_current_user() -> object:
        return SimpleNamespace(user_id=7)

    app = FastAPI()
    app.include_router(eligibility_router)
    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_current_user
    register_exception_handlers(app)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/eligibility/requests",
            json={
                "policy_id": "WLF00000024",
                "chat_session_id": "abc",
                "user_conditions": {"income": "low"},
            },
        )

    assert response.status_code == 422


def test_stream_eligibility_request_checks_chat_session_ownership(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_db() -> AsyncGenerator[object, None]:
        yield SimpleNamespace()

    async def fake_current_user() -> object:
        return SimpleNamespace(user_id=7)

    async def fake_ensure_owned_session(db, *, user_id, chat_session_id):
        captured["ensure_session"] = {
            "user_id": user_id,
            "chat_session_id": chat_session_id,
        }

    async def fake_stream(db, user_id, payload):
        yield ai_request_controller._sse_event(
            {"type": "done", "payload": {"request_id": "123"}}
        )

    monkeypatch.setattr(
        ai_request_controller.ChatService,
        "ensure_owned_session",
        fake_ensure_owned_session,
    )
    monkeypatch.setattr(
        ai_request_controller,
        "_eligibility_sse_stream",
        fake_stream,
    )

    app = FastAPI()
    app.include_router(eligibility_router)
    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_current_user
    register_exception_handlers(app)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/eligibility/requests/stream",
            json={
                "policy_id": "WLF00000024",
                "chat_session_id": "82",
                "source_ref_id": "recommendation:123",
                "user_conditions": {"income": "low"},
            },
        )

    assert response.status_code == 200
    assert captured["ensure_session"] == {
        "user_id": 7,
        "chat_session_id": 82,
    }


def test_stream_eligibility_request_emits_progress_and_done(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    async def fake_db() -> AsyncGenerator[object, None]:
        yield SimpleNamespace()

    async def fake_current_user() -> object:
        return SimpleNamespace(user_id=7)

    class FakeInnerDb:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    class FakeSessionContext:
        def __init__(self) -> None:
            self.db = FakeInnerDb()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run(
        *,
        db,
        user_id,
        policy_identifier,
        raw_query=None,
        selected_conditions=None,
        source_type="CHAT",
        source_ref_id=None,
        follow_up_resolved=False,
    ):
        await emit_progress("eligibility", "create_request", "started", 1, 4)
        await emit_progress("eligibility", "create_request", "completed", 1, 4)
        await emit_progress("eligibility", "assess_policy", "started", 3, 4)
        return {
            "request_id": "123",
            "status": "COMPLETED",
            "policy_id": str(policy_identifier),
            "user_status": "LIKELY_MATCH",
        }

    monkeypatch.setattr(ai_request_controller, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(
        ai_request_controller.eligibility_graph_runner,
        "run",
        fake_run,
    )

    app = FastAPI()
    app.include_router(eligibility_router)
    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_current_user
    register_exception_handlers(app)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/v1/eligibility/requests/stream",
            json={
                "policy_id": "24",
                "source_ref_id": "WLF00000024",
                "user_conditions": {"income": "low"},
            },
        ) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    assert [event["type"] for event in events] == [
        "progress",
        "progress",
        "progress",
        "done",
    ]
    assert events[0]["flow"] == "eligibility"
    assert events[0]["node"] == "create_request"
    assert events[-1]["payload"]["request_id"] == "123"


def test_get_eligibility_request_returns_result_response(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_db() -> AsyncGenerator[object, None]:
        yield SimpleNamespace()

    async def fake_current_user() -> object:
        return SimpleNamespace(user_id=7)

    async def fake_get_eligibility_result(
        self,
        db,
        *,
        request_id,
        user_id,
    ):
        captured["get"] = {
            "request_id": request_id,
            "user_id": user_id,
        }
        return EligibilityResultResponse(
            request_id=str(request_id),
            status=RequestStatus.COMPLETED,
            policy_id="24",
            slug="WLF00000024",
            policy_name="테스트 정책",
            user_status="RECOMMENDABLE",
            banner_level="high",
            summary="지원 가능성이 높습니다.",
            matched_conditions=["region"],
            evidences=[],
        )

    monkeypatch.setattr(
        AiRequestLifecycleService,
        "get_eligibility_result",
        fake_get_eligibility_result,
    )

    app = FastAPI()
    app.include_router(eligibility_router)
    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_current_user
    register_exception_handlers(app)

    with TestClient(app) as client:
        response = client.get("/api/v1/eligibility/requests/123")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["request_id"] == "123"
    assert body["data"]["status"] == "COMPLETED"
    assert body["data"]["slug"] == "WLF00000024"
    assert body["data"]["user_status"] == "RECOMMENDABLE"
    assert body["meta"] == {
        "request_id": "123",
        "follow_up_required": False,
    }
    assert captured["get"] == {
        "request_id": 123,
        "user_id": 7,
    }


def test_get_eligibility_request_persists_terminal_chat_result(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_db() -> AsyncGenerator[object, None]:
        db = SimpleNamespace()

        async def commit():
            captured["committed"] = True

        async def rollback():
            captured["rolled_back"] = True

        db.commit = commit
        db.rollback = rollback
        yield db

    async def fake_current_user() -> object:
        return SimpleNamespace(user_id=7)

    def fake_init(self):
        async def find_by_id(db, request_type, request_id):
            captured["find_request"] = {
                "request_type": request_type,
                "request_id": request_id,
            }
            return SimpleNamespace(
                request_id=request_id,
                user_id=7,
                policy_id=24,
                source_ref_id="chat_session:82;source:recommendation:123",
            )

        self.repository = SimpleNamespace(find_by_id=find_by_id)

    async def fake_get_eligibility_result(
        self,
        db,
        *,
        request_id,
        user_id,
    ):
        return EligibilityResultResponse(
            request_id=str(request_id),
            status=RequestStatus.COMPLETED,
            policy_id="24",
            slug="WLF00000024",
            policy_name="테스트 정책",
            user_status="RECOMMENDABLE",
            banner_level="high",
            summary="지원 가능성이 높습니다.",
            matched_conditions=["region"],
            evidences=[],
        )

    async def fake_persist_eligibility_result_message(
        db,
        *,
        user_id,
        request,
        result_json,
    ):
        captured["persist"] = {
            "user_id": user_id,
            "request_id": request.request_id,
            "source_ref_id": request.source_ref_id,
            "result_request_id": result_json["request_id"],
        }

    monkeypatch.setattr(AiRequestLifecycleService, "__init__", fake_init)
    monkeypatch.setattr(
        AiRequestLifecycleService,
        "get_eligibility_result",
        fake_get_eligibility_result,
    )
    monkeypatch.setattr(
        ai_request_controller.ChatService,
        "persist_eligibility_result_message",
        fake_persist_eligibility_result_message,
    )

    app = FastAPI()
    app.include_router(eligibility_router)
    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_current_user
    register_exception_handlers(app)

    with TestClient(app) as client:
        response = client.get("/api/v1/eligibility/requests/123")

    assert response.status_code == 200
    assert captured["find_request"] == {
        "request_type": "eligibility",
        "request_id": 123,
    }
    assert captured["persist"] == {
        "user_id": 7,
        "request_id": 123,
        "source_ref_id": "chat_session:82;source:recommendation:123",
        "result_request_id": "123",
    }
    assert captured["committed"] is True
    assert "rolled_back" not in captured


def test_eligibility_result_response_maps_assessment_to_user_response() -> None:
    service = AiRequestLifecycleService()
    request = SimpleNamespace(
        request_id=123,
        request_status=RequestStatus.COMPLETED.value,
        policy_id=24,
        parsed_query_json={"selected_conditions": {"region": "seoul"}},
        merged_condition_json={},
        error_message=None,
    )
    response = service.to_eligibility_result_response(
        request=request,
        policy={
            "policy_code": "WLF00000024",
            "policy_name": "테스트 정책",
        },
        assessment={
            "assessment_status": "LIKELY_MATCH",
            "reason_summary": "지원 가능성이 높습니다.",
            "matched_conditions_json": ["region"],
            "missing_conditions_json": [],
            "conflicting_conditions_json": [],
            "manual_check_points_json": [],
            "evidences": [
                {
                    "chunk_id": 10,
                    "evidence_policy_id": "24",
                    "snippet": "지원 대상 근거",
                    "source_title": "정책 상세",
                    "source_url": "https://example.com",
                    "similarity_score": 0.12,
                    "evidence_role": "TARGET",
                }
            ],
        },
    )

    assert response.user_status == "RECOMMENDABLE"
    assert response.banner_level == "high"
    assert response.criteria[0].status == "ok"
    assert response.evidences[0].evidence_role == "target"
    assert response.input_summary == {"region": "seoul"}


def test_eligibility_result_response_masks_error_message() -> None:
    service = AiRequestLifecycleService()
    request = SimpleNamespace(
        request_id=123,
        request_status=RequestStatus.FAILED.value,
        policy_id=24,
        parsed_query_json={},
        merged_condition_json={},
        error_message=(
            "consuming input failed: server closed the connection unexpectedly"
        ),
    )

    response = service.to_eligibility_result_response(
        request=request,
        policy={
            "policy_code": "WLF00000024",
            "policy_name": "테스트 정책",
        },
        assessment=None,
    )

    assert response.error_message == (
        "분석 처리 중 일시적인 문제가 발생했어요. 잠시 후 다시 시도해 주세요."
    )


def test_eligibility_manual_check_points_become_follow_up_questions() -> None:
    service = AiRequestLifecycleService()
    request = SimpleNamespace(
        request_id=123,
        request_status=RequestStatus.COMPLETED.value,
        policy_id=24,
        parsed_query_json={"selected_conditions": {"region": "seoul"}},
        merged_condition_json={},
        error_message=None,
    )

    response = service.to_eligibility_result_response(
        request=request,
        policy={
            "policy_code": "WLF00000024",
            "policy_name": "테스트 정책",
        },
        assessment={
            "assessment_status": "NEEDS_MORE_INFO",
            "reason_summary": "추가 확인이 필요합니다.",
            "matched_conditions_json": [],
            "missing_conditions_json": [],
            "conflicting_conditions_json": [],
            "manual_check_points_json": [
                "환경오염 피해로 인한 생명, 신체 및 재산 피해가 발생되었다고 의심되는 경우",
                "SERVICE_FIELD_NOT_SUPPORTED",
            ],
            "evidences": [],
        },
    )

    assert response.criteria == []
    assert len(response.follow_up_questions) == 1
    question = response.follow_up_questions[0]
    assert question.field_name == "manual_confirmation"
    assert question.question_text.startswith("환경오염 피해")
    assert question.question_text.endswith("해당하시나요?")
    assert question.options == [
        {"value": "yes", "label": "예, 해당돼요"},
        {"value": "no", "label": "아니요, 해당되지 않아요"},
        {"value": "unknown", "label": "잘 모르겠어요"},
    ]


def test_eligibility_manual_check_internal_codes_become_user_questions() -> None:
    service = AiRequestLifecycleService()
    request = SimpleNamespace(
        request_id=123,
        request_status=RequestStatus.COMPLETED.value,
        policy_id=24,
        parsed_query_json={"selected_conditions": {"region": "seoul"}},
        merged_condition_json={},
        error_message=None,
    )

    response = service.to_eligibility_result_response(
        request=request,
        policy={
            "policy_code": "WLF00000024",
            "policy_name": "테스트 정책",
        },
        assessment={
            "assessment_status": "NEEDS_MORE_INFO",
            "reason_summary": "추가 확인이 필요합니다.",
            "matched_conditions_json": [],
            "missing_conditions_json": [],
            "conflicting_conditions_json": [],
            "manual_check_points_json": [
                "OVERSEAS_STAY_90_DAYS_PAYMENT_SUSPENDED",
                "REFUGEE_APPLICATION_PENDING_EXCLUDED",
                "SERVICE_FIELD_NOT_SUPPORTED",
            ],
            "evidences": [],
        },
    )

    question_texts = [
        question.question_text for question in response.follow_up_questions
    ]
    assert question_texts == [
        "최근 90일 이상 해외에 체류하여 급여 지급이 정지된 상태인가요?",
        "현재 난민 인정 심사 중인 상태인가요?",
    ]
    assert response.manual_check_points == []


def test_eligibility_evidence_response_has_display_text() -> None:
    service = AiRequestLifecycleService()
    request = SimpleNamespace(
        request_id=123,
        request_status=RequestStatus.COMPLETED.value,
        policy_id=24,
        parsed_query_json={"selected_conditions": {"region": "seoul"}},
        merged_condition_json={},
        error_message=None,
    )

    response = service.to_eligibility_result_response(
        request=request,
        policy={
            "policy_code": "WLF00000024",
            "policy_name": "테스트 정책",
        },
        assessment={
            "assessment_status": "LIKELY_MATCH",
            "reason_summary": "지원 가능성이 높습니다.",
            "matched_conditions_json": [],
            "missing_conditions_json": [],
            "conflicting_conditions_json": [],
            "manual_check_points_json": [],
            "evidences": [
                {
                    "chunk_id": 10,
                    "evidence_policy_id": "24",
                    "snippet": (
                        "정책명: 개인회생 파산 종합지원(지원센터) "
                        "섹션: 조건 구조 내용: - field: debt_status, operator: IN"
                    ),
                    "source_title": "개인회생 파산 종합지원(지원센터)",
                    "source_url": "https://example.com",
                    "similarity_score": 0.12,
                    "evidence_role": "TARGET",
                }
            ],
        },
    )

    assert response.evidences[0].display_text == (
        "개인회생 파산 종합지원(지원센터)에서는 채무 상황 조건을 정해 두고 있어요. "
        "현재 입력하신 정보만으로는 이 기준에 어긋나는 부분이 없어, "
        "지원 가능성이 높다고 판단했어요."
    )


def test_manual_confirmations_are_applied_to_assessment_condition() -> None:
    service = AiRequestLifecycleService()

    condition = service._apply_manual_confirmations(
        {
            "manual_check_points": ["A 조건", "B 조건", "SERVICE_FIELD_NOT_SUPPORTED"],
            "matched_conditions": [],
            "rule_failures": [],
        },
        {
            "manual_confirmations": [
                {"question": "A 조건", "answer": "yes"},
                {"question": "B 조건", "answer": "no"},
                {"question": "SERVICE_FIELD_NOT_SUPPORTED", "answer": "yes"},
            ]
        },
    )

    assert condition["matched_conditions"] == ["A 조건"]
    assert condition["rule_failures"] == ["B 조건"]
    assert condition["manual_check_points"] == ["SERVICE_FIELD_NOT_SUPPORTED"]


def test_manual_confirmation_matches_rewritten_question_text() -> None:
    service = AiRequestLifecycleService()

    condition = service._apply_manual_confirmations(
        {
            "manual_check_points": ["OVERSEAS_STAY_90_DAYS_PAYMENT_SUSPENDED"],
            "matched_conditions": [],
            "rule_failures": [],
        },
        {
            "manual_confirmations": [
                {
                    "question": (
                        "최근 90일 이상 해외에 체류하여 급여 지급이 정지된 상태인가요?"
                    ),
                    "answer": "yes",
                },
            ]
        },
    )

    # 제외형 조건(해당 시 지원에서 빠짐)이므로 yes는 충족이 아니라 미충족으로 반영된다.
    assert condition["matched_conditions"] == []
    assert condition["rule_failures"] == [
        "최근 90일 이상 해외에 체류하여 급여 지급이 정지된 상태인가요?"
    ]
    assert condition["manual_check_points"] == []


def test_policy_detail_reanalysis_keeps_profile_condition_and_applies_confirmation() -> None:
    service = AiRequestLifecycleService()

    condition = service._apply_manual_confirmations(
        {
            "stage": "newborn",
            "childAge": "0",
            "income": "mid1",
            "region": "seoul",
            "manual_check_points": ["감당하기 어려운 채무가 있는 사람 확인 필요"],
            "missing_conditions": ["감당하기 어려운 채무가 있는 사람 확인 필요"],
            "matched_conditions": ["저장 프로필 기준"],
            "rule_failures": [],
        },
        {
            "manual_confirmations": [
                {
                    "question": "감당하기 어려운 채무가 있는 사람에 해당하시나요?",
                    "answer": "yes",
                    "source": "감당하기 어려운 채무가 있는 사람 확인 필요",
                }
            ]
        },
    )

    assert condition["stage"] == "newborn"
    assert condition["childAge"] == "0"
    assert condition["income"] == "mid1"
    assert condition["region"] == "seoul"
    assert condition["manual_check_points"] == []
    assert condition["missing_conditions"] == []
    assert condition["matched_conditions"] == [
        "저장 프로필 기준",
        "감당하기 어려운 채무가 있는 사람에 해당하시나요?",
    ]
    assert condition["rule_failures"] == []


def test_manual_confirmations_are_parsed_for_follow_up_resolution() -> None:
    service = AiRequestLifecycleService()

    confirmations = service._manual_confirmations(
        {
            "manual_confirmations": [
                {"question": "A 조건", "answer": "yes"},
                {"question": "B 조건", "answer": "unknown"},
                {"question": "C 조건", "answer": "invalid"},
                {"question": "", "answer": "yes"},
            ]
        }
    )

    assert confirmations == [
        {"question": "A 조건", "answer": "yes"},
        {"question": "B 조건", "answer": "unknown"},
    ]


def test_process_eligibility_request_saves_policy_assessment(monkeypatch) -> None:
    captured: dict[str, object] = {}
    request = SimpleNamespace(
        request_id=123,
        request_status=RequestStatus.PROCESSING.value,
        user_id=7,
        policy_id=24,
        source_type="POLICY_DETAIL",
        source_ref_id="WLF00000024",
        raw_query=None,
        parsed_query_json={"selected_conditions": {"region": "seoul"}},
        merged_condition_json={},
        profile_conflict_json=[],
        result_json=None,
        error_message=None,
    )

    class FakeRepository:
        async def find_by_id(self, db, request_type, request_id):
            captured["find"] = {
                "request_type": request_type,
                "request_id": request_id,
            }
            return request

        async def update_payload(
            self,
            db,
            request,
            parsed_query_json=None,
            merged_condition_json=None,
            profile_conflict_json=None,
        ):
            captured["payload"] = {
                "parsed_query_json": parsed_query_json,
                "merged_condition_json": merged_condition_json,
                "profile_conflict_json": profile_conflict_json,
            }
            request.parsed_query_json = parsed_query_json
            request.merged_condition_json = merged_condition_json
            request.profile_conflict_json = profile_conflict_json
            return request

        async def update_status(self, db, request, status, error_message=None):
            captured["status"] = status
            request.request_status = status.value
            request.error_message = error_message
            return request

        async def update_result(self, db, request, result_json):
            captured["result_json"] = result_json
            request.result_json = result_json
            return request

    class FakeConditionAgent:
        async def analyze(self, condition_input):
            captured["condition_input"] = condition_input
            return ConditionResult(
                parsed_query_json={
                    "selected_conditions": {"region": "seoul"},
                },
                merged_condition_json={
                    "region": "seoul",
                    "matched_conditions": ["region"],
                },
            )

    class FakeAssessmentRepository:
        async def find_policy_evidence_chunks(self, db, policy_id, limit=8):
            captured["evidence_chunks"] = {
                "policy_id": policy_id,
                "limit": limit,
            }
            return []

    async def fake_chunk_searcher(**kwargs):
        captured["rag_search"] = kwargs
        return []

    class FakeConnection:
        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def connection(self):
            return FakeConnection()

    async def fake_save_assessment(
        conn,
        result,
        assessment_type,
        recommendation_request_id=None,
        eligibility_request_id=None,
        confidence_score=None,
        selected_for_result=False,
    ):
        captured["saved_assessment"] = {
            "conn": conn,
            "policy_id": result.policy_id,
            "assessment_status": result.assessment_status.value,
            "assessment_type": assessment_type,
            "eligibility_request_id": eligibility_request_id,
            "matched_conditions": result.matched_conditions,
        }
        return 999

    monkeypatch.setattr(
        "app.services.ai_request_lifecycle_service.psycopg_pool",
        FakePool(),
    )
    monkeypatch.setattr(
        "app.services.ai_request_lifecycle_service.PolicyAssessmentRepository.save_assessment",
        fake_save_assessment,
    )

    async def fake_profile_snapshot(self, db, user_id):
        return None

    monkeypatch.setattr(
        AiRequestLifecycleService,
        "_profile_snapshot",
        fake_profile_snapshot,
    )

    service = AiRequestLifecycleService(
        repository=FakeRepository(),
        assessment_repository=FakeAssessmentRepository(),
        condition_agent=FakeConditionAgent(),
        policy_chunk_searcher=fake_chunk_searcher,
    )

    import asyncio

    asyncio.run(
        service.process_condition_request(
            db=SimpleNamespace(),
            request_type="eligibility",
            request_id=123,
        )
    )

    assert captured["rag_search"]["policy_ids"] == [24]
    assert "충족 조건: region" in captured["rag_search"]["query"]
    assert captured["evidence_chunks"] == {
        "policy_id": 24,
        "limit": 8,
    }
    assert captured["status"] == RequestStatus.COMPLETED
    assert captured["saved_assessment"] == {
        "conn": "conn",
        "policy_id": 24,
        "assessment_status": "LIKELY_MATCH",
        "assessment_type": "eligibility_detail",
        "eligibility_request_id": 123,
        "matched_conditions": ["region"],
    }


def test_policy_detail_eligibility_uses_saved_profile_without_selected_conditions(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    request = SimpleNamespace(
        request_id=125,
        request_status=RequestStatus.PROCESSING.value,
        user_id=7,
        policy_id=24,
        source_type="POLICY_DETAIL",
        source_ref_id="WLF00000024",
        raw_query=None,
        parsed_query_json=None,
        merged_condition_json={},
        profile_conflict_json=[],
        result_json=None,
        error_message=None,
    )

    class FakeRepository:
        async def find_by_id(self, db, request_type, request_id):
            return request

        async def update_payload(
            self,
            db,
            request,
            parsed_query_json=None,
            merged_condition_json=None,
            profile_conflict_json=None,
        ):
            captured["payload"] = {
                "parsed_query_json": parsed_query_json,
                "merged_condition_json": merged_condition_json,
                "profile_conflict_json": profile_conflict_json,
            }
            request.parsed_query_json = parsed_query_json
            request.merged_condition_json = merged_condition_json
            request.profile_conflict_json = profile_conflict_json
            return request

        async def update_status(self, db, request, status, error_message=None):
            captured["status"] = status
            request.request_status = status.value
            request.error_message = error_message
            return request

        async def update_result(self, db, request, result_json):
            request.result_json = result_json
            return request

    class FakeConditionAgent:
        async def analyze(self, condition_input):
            captured["selected_conditions"] = condition_input.selected_conditions
            captured["profile_snapshot"] = condition_input.profile_snapshot
            return ConditionResult(
                parsed_query_json={
                    "selected_conditions": condition_input.selected_conditions,
                },
                merged_condition_json={
                    **condition_input.profile_snapshot,
                    "matched_conditions": ["저장 프로필 기준"],
                },
                profile_conflicts=[],
            )

    class FakeAssessmentRepository:
        async def find_policy_evidence_chunks(self, db, policy_id, limit=8):
            return []

    async def fake_chunk_searcher(**kwargs):
        captured["rag_search"] = kwargs
        return []

    class FakeConnection:
        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def connection(self):
            return FakeConnection()

    async def fake_save_assessment(*args, **kwargs):
        return 999

    async def fake_profile_snapshot(self, db, user_id):
        captured["profile_snapshot_called"] = True
        return {
            "stage": "newborn",
            "childAge": "0",
            "income": "mid1",
            "region": "seoul",
        }

    monkeypatch.setattr(
        "app.services.ai_request_lifecycle_service.psycopg_pool",
        FakePool(),
    )
    monkeypatch.setattr(
        "app.services.ai_request_lifecycle_service.PolicyAssessmentRepository.save_assessment",
        fake_save_assessment,
    )
    monkeypatch.setattr(
        AiRequestLifecycleService,
        "_profile_snapshot",
        fake_profile_snapshot,
    )

    service = AiRequestLifecycleService(
        repository=FakeRepository(),
        assessment_repository=FakeAssessmentRepository(),
        condition_agent=FakeConditionAgent(),
        policy_chunk_searcher=fake_chunk_searcher,
    )

    import asyncio

    asyncio.run(
        service.process_condition_request(
            db=SimpleNamespace(),
            request_type="eligibility",
            request_id=125,
        )
    )

    assert captured["profile_snapshot_called"] is True
    assert captured["selected_conditions"] is None
    assert captured["profile_snapshot"] == {
        "stage": "newborn",
        "childAge": "0",
        "income": "mid1",
        "region": "seoul",
    }
    assert captured["payload"]["merged_condition_json"]["stage"] == "newborn"
    assert captured["payload"]["merged_condition_json"]["income"] == "mid1"
    assert captured["status"] == RequestStatus.COMPLETED


def test_policy_detail_eligibility_prefers_saved_profile_over_stale_selected_conditions(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    request = SimpleNamespace(
        request_id=126,
        request_status=RequestStatus.PROCESSING.value,
        user_id=7,
        policy_id=24,
        source_type="POLICY_DETAIL",
        source_ref_id="WLF00000024",
        raw_query=None,
        parsed_query_json={
            "selected_conditions": {
                "stage": "teen",
                "childAge": "13+",
                "income": "high",
                "region": "busan",
                "manual_confirmations": [
                    {
                        "question": "추가 확인 조건에 해당하시나요?",
                        "answer": "yes",
                        "source": "추가 확인 조건 확인 필요",
                    }
                ],
            }
        },
        merged_condition_json={},
        profile_conflict_json=[],
        result_json=None,
        error_message=None,
    )

    class FakeRepository:
        async def find_by_id(self, db, request_type, request_id):
            return request

        async def update_payload(
            self,
            db,
            request,
            parsed_query_json=None,
            merged_condition_json=None,
            profile_conflict_json=None,
        ):
            captured["payload"] = {
                "parsed_query_json": parsed_query_json,
                "merged_condition_json": merged_condition_json,
                "profile_conflict_json": profile_conflict_json,
            }
            request.parsed_query_json = parsed_query_json
            request.merged_condition_json = merged_condition_json
            request.profile_conflict_json = profile_conflict_json
            return request

        async def update_status(self, db, request, status, error_message=None):
            captured["status"] = status
            request.request_status = status.value
            request.error_message = error_message
            return request

        async def update_result(self, db, request, result_json):
            request.result_json = result_json
            return request

    class FakeConditionAgent:
        async def analyze(self, condition_input):
            captured["selected_conditions"] = condition_input.selected_conditions
            captured["profile_snapshot"] = condition_input.profile_snapshot
            return ConditionResult(
                parsed_query_json={
                    "selected_conditions": condition_input.selected_conditions,
                },
                merged_condition_json={
                    **condition_input.profile_snapshot,
                    "manual_check_points": ["추가 확인 조건 확인 필요"],
                    "matched_conditions": ["저장 프로필 기준"],
                },
                profile_conflicts=[],
            )

    class FakeAssessmentRepository:
        async def find_policy_evidence_chunks(self, db, policy_id, limit=8):
            return []

    async def fake_chunk_searcher(**kwargs):
        return []

    class FakeConnection:
        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def connection(self):
            return FakeConnection()

    async def fake_save_assessment(*args, **kwargs):
        return 999

    async def fake_profile_snapshot(self, db, user_id):
        return {
            "stage": "newborn",
            "childAge": "0",
            "income": "mid1",
            "region": "seoul",
        }

    monkeypatch.setattr(
        "app.services.ai_request_lifecycle_service.psycopg_pool",
        FakePool(),
    )
    monkeypatch.setattr(
        "app.services.ai_request_lifecycle_service.PolicyAssessmentRepository.save_assessment",
        fake_save_assessment,
    )
    monkeypatch.setattr(
        AiRequestLifecycleService,
        "_profile_snapshot",
        fake_profile_snapshot,
    )

    service = AiRequestLifecycleService(
        repository=FakeRepository(),
        assessment_repository=FakeAssessmentRepository(),
        condition_agent=FakeConditionAgent(),
        policy_chunk_searcher=fake_chunk_searcher,
    )

    import asyncio

    asyncio.run(
        service.process_condition_request(
            db=SimpleNamespace(),
            request_type="eligibility",
            request_id=126,
        )
    )

    assert captured["selected_conditions"] == {
        "manual_confirmations": [
            {
                "question": "추가 확인 조건에 해당하시나요?",
                "answer": "yes",
                "source": "추가 확인 조건 확인 필요",
            }
        ]
    }
    assert captured["profile_snapshot"] == {
        "stage": "newborn",
        "childAge": "0",
        "income": "mid1",
        "region": "seoul",
    }
    assert captured["payload"]["merged_condition_json"]["stage"] == "newborn"
    assert captured["payload"]["merged_condition_json"]["income"] == "mid1"
    assert captured["payload"]["profile_conflict_json"] == []
    assert captured["status"] == RequestStatus.COMPLETED


def test_recommendation_result_eligibility_skips_saved_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}
    request = SimpleNamespace(
        request_id=124,
        request_status=RequestStatus.PROCESSING.value,
        user_id=7,
        policy_id=24,
        source_type="RECOMMENDATION_RESULT",
        source_ref_id="recommendation-1",
        raw_query=None,
        parsed_query_json={
            "selected_conditions": {
                "region": "seoul",
                "stage": "newborn",
                "income": "mid1",
            }
        },
        merged_condition_json={},
        profile_conflict_json=[],
        error_message=None,
    )

    class FakeRepository:
        async def find_by_id(self, db, request_type, request_id):
            return request

        async def update_payload(
            self,
            db,
            request,
            parsed_query_json=None,
            merged_condition_json=None,
            profile_conflict_json=None,
        ):
            captured["payload"] = {
                "merged_condition_json": merged_condition_json,
                "profile_conflict_json": profile_conflict_json,
            }
            request.parsed_query_json = parsed_query_json
            request.merged_condition_json = merged_condition_json
            request.profile_conflict_json = profile_conflict_json
            return request

        async def update_status(self, db, request, status, error_message=None):
            captured["status"] = status
            request.request_status = status.value
            request.error_message = error_message
            return request

        async def update_result(self, db, request, result_json):
            captured["result_json"] = result_json
            request.result_json = result_json
            return request

    class FakeConditionAgent:
        async def analyze(self, condition_input):
            captured["condition_profile_snapshot"] = condition_input.profile_snapshot
            return ConditionResult(
                parsed_query_json={
                    "selected_conditions": condition_input.selected_conditions,
                },
                merged_condition_json={
                    **condition_input.selected_conditions,
                    "matched_conditions": ["region"],
                },
                profile_conflicts=[],
            )

    class FakeAssessmentRepository:
        async def find_policy_evidence_chunks(self, db, policy_id, limit=8):
            return []

    async def fake_chunk_searcher(**kwargs):
        captured["rag_search"] = kwargs
        return []

    class FakeConnection:
        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def connection(self):
            return FakeConnection()

    async def fake_save_assessment(
        conn,
        result,
        assessment_type,
        recommendation_request_id=None,
        eligibility_request_id=None,
        confidence_score=None,
        selected_for_result=False,
    ):
        captured["saved_status"] = result.assessment_status.value
        captured["saved_conflicts"] = result.conflicting_conditions
        return 999

    async def fake_profile_snapshot(self, db, user_id):
        captured["profile_snapshot_called"] = True
        return {
            "region": "busan",
            "stage": "teen",
            "income": "high",
        }

    monkeypatch.setattr(
        "app.services.ai_request_lifecycle_service.psycopg_pool",
        FakePool(),
    )
    monkeypatch.setattr(
        "app.services.ai_request_lifecycle_service.PolicyAssessmentRepository.save_assessment",
        fake_save_assessment,
    )
    monkeypatch.setattr(
        AiRequestLifecycleService,
        "_profile_snapshot",
        fake_profile_snapshot,
    )

    service = AiRequestLifecycleService(
        repository=FakeRepository(),
        assessment_repository=FakeAssessmentRepository(),
        condition_agent=FakeConditionAgent(),
        policy_chunk_searcher=fake_chunk_searcher,
    )

    import asyncio

    asyncio.run(
        service.process_condition_request(
            db=SimpleNamespace(),
            request_type="eligibility",
            request_id=124,
        )
    )

    assert captured.get("profile_snapshot_called") is None
    assert captured["condition_profile_snapshot"] is None
    assert captured["rag_search"]["policy_ids"] == [24]
    assert captured["payload"] == {
        "merged_condition_json": {
            "region": "seoul",
            "stage": "newborn",
            "income": "mid1",
            "matched_conditions": ["region"],
        },
        "profile_conflict_json": [],
    }
    assert captured["status"] == RequestStatus.COMPLETED
    assert captured["saved_status"] == "LIKELY_MATCH"
    assert captured["saved_conflicts"] == []


def test_eligibility_evidence_uses_condition_based_rag_before_fallback() -> None:
    captured: dict[str, object] = {}

    class FakeAssessmentRepository:
        async def find_policy_evidence_chunks(self, db, policy_id, limit=8):
            captured["fallback_called"] = True
            return []

    async def fake_chunk_searcher(**kwargs):
        captured["rag_search"] = kwargs
        return [
            EvidenceChunk(
                chunk_id=9,
                policy_id=24,
                snippet="서울 거주 임산부 대상 지원 근거",
                source_title="테스트 정책 - 지원 대상",
                source_url="",
                score=0.91,
                evidence_role="TARGET",
            )
        ]

    service = AiRequestLifecycleService(
        assessment_repository=FakeAssessmentRepository(),
        policy_chunk_searcher=fake_chunk_searcher,
    )
    request = SimpleNamespace(
        request_id=123,
        raw_query=None,
        parsed_query_json={"selected_conditions": {"region": "seoul", "stage": "pregnant"}},
    )

    import asyncio

    chunks = asyncio.run(
        service._find_eligibility_evidence_chunks(
            db=SimpleNamespace(),
            policy_id=24,
            condition={
                "region": "seoul",
                "stage": "pregnant",
                "matched_conditions": ["서울 거주"],
                "manual_check_points": ["임신 여부 확인"],
            },
            request=request,
        )
    )

    assert captured.get("fallback_called") is None
    assert captured["rag_search"]["policy_ids"] == [24]
    assert "사용자 입력 조건: region=seoul, stage=pregnant" in captured["rag_search"]["query"]
    assert "충족 조건: 서울 거주" in captured["rag_search"]["query"]
    assert chunks[0].chunk_id == 9
