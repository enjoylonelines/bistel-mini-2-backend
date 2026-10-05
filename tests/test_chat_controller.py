import json
from collections.abc import AsyncGenerator
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from app.api.chat_controller import router
from app.common.exceptions import AppException, ErrorCode, register_exception_handlers
from app.core.dependencies import get_current_user
from app.db.models.chat_session import ChatSession
from app.db.session import get_db_session
from app.schemas.chat_schema import (
    AssistantMessage,
    AssistantMessageEvidence,
    AssistantMessagePolicy,
    ChatRequestStatusResponse,
    ChatMessageItem,
    ChatMessageListResponse,
    ChatMessageSendResponse,
    ChatSessionBulkDeleteResponse,
    ChatSessionCreateResponse,
    ChatSessionDeleteResponse,
    ChatSessionTitleUpdateResponse,
)
from app.services.chat_service import ChatService
from app.services.chat_service import _to_message_item


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)

    async def fake_db() -> AsyncGenerator[object, None]:
        yield object()

    async def fake_user() -> SimpleNamespace:
        return SimpleNamespace(user_id=5, email="t@t.com", nickname="t")

    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[get_current_user] = fake_user
    return app


def test_create_chat_session_returns_201(monkeypatch) -> None:
    monkeypatch.setattr(
        ChatService,
        "create_session",
        AsyncMock(return_value=ChatSessionCreateResponse(
            chat_session_id="9",
            session_status="ACTIVE",
        )),
    )

    with TestClient(_build_app()) as client:
        resp = client.post("/api/v1/chat/sessions", json={"title": "테스트"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["chat_session_id"] == "9"
    assert body["data"]["session_status"] == "ACTIVE"


def test_update_chat_session_title_returns_updated_title(monkeypatch) -> None:
    update_mock = AsyncMock(return_value=ChatSessionTitleUpdateResponse(
        chat_session_id="9",
        title="수정 제목",
        updated_at=datetime(2026, 6, 23, 10, 30, 0),
    ))
    monkeypatch.setattr(ChatService, "update_session_title", update_mock)

    with TestClient(_build_app()) as client:
        resp = client.patch(
            "/api/v1/chat/sessions/9",
            json={"title": "  수정 제목  "},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["chat_session_id"] == "9"
    assert body["data"]["title"] == "수정 제목"
    update_mock.assert_awaited_once()
    assert update_mock.await_args.kwargs["chat_session_id"] == 9
    assert update_mock.await_args.kwargs["title"] == "수정 제목"


def test_update_chat_session_title_rejects_blank_title(monkeypatch) -> None:
    update_mock = AsyncMock()
    monkeypatch.setattr(ChatService, "update_session_title", update_mock)

    with TestClient(_build_app()) as client:
        resp = client.patch("/api/v1/chat/sessions/9", json={"title": "   "})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    update_mock.assert_not_awaited()


def test_update_chat_session_title_rejects_over_max_length(monkeypatch) -> None:
    update_mock = AsyncMock()
    monkeypatch.setattr(ChatService, "update_session_title", update_mock)

    with TestClient(_build_app()) as client:
        resp = client.patch(
            "/api/v1/chat/sessions/9",
            json={"title": "가" * 256},
        )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    update_mock.assert_not_awaited()


def test_delete_chat_session_returns_deleted_response(monkeypatch) -> None:
    delete_mock = AsyncMock(return_value=ChatSessionDeleteResponse(
        chat_session_id="9",
        deleted=True,
    ))
    monkeypatch.setattr(ChatService, "delete_session", delete_mock)

    with TestClient(_build_app()) as client:
        resp = client.delete("/api/v1/chat/sessions/9")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"] == {"chat_session_id": "9", "deleted": True}
    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.kwargs["user_id"] == 5
    assert delete_mock.await_args.kwargs["chat_session_id"] == 9


def test_bulk_delete_chat_sessions_returns_deleted_ids(monkeypatch) -> None:
    bulk_delete_mock = AsyncMock(return_value=ChatSessionBulkDeleteResponse(
        deleted_count=2,
        deleted_session_ids=["9", "10"],
    ))
    monkeypatch.setattr(ChatService, "bulk_delete_sessions", bulk_delete_mock)

    with TestClient(_build_app()) as client:
        resp = client.post(
            "/api/v1/chat/sessions/bulk-delete",
            json={"chat_session_ids": [9, 10]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["deleted_count"] == 2
    assert body["data"]["deleted_session_ids"] == ["9", "10"]
    bulk_delete_mock.assert_awaited_once()
    assert bulk_delete_mock.await_args.kwargs["user_id"] == 5
    assert bulk_delete_mock.await_args.kwargs["chat_session_ids"] == [9, 10]


def test_bulk_delete_chat_sessions_rejects_empty_ids(monkeypatch) -> None:
    bulk_delete_mock = AsyncMock()
    monkeypatch.setattr(ChatService, "bulk_delete_sessions", bulk_delete_mock)

    with TestClient(_build_app()) as client:
        resp = client.post(
            "/api/v1/chat/sessions/bulk-delete",
            json={"chat_session_ids": []},
        )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    bulk_delete_mock.assert_not_awaited()


def test_send_chat_message_serializes_normalized_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        ChatService,
        "send_message",
        AsyncMock(return_value=ChatMessageSendResponse(
            chat_session_id="9",
            user_message_id="11",
            assistant_message=AssistantMessage(
                chat_message_id="12",
                content="답변",
                user_status=None,
                sources=[],
                policies=[
                    AssistantMessagePolicy(
                        policy_id="42",
                        slug="WLF1",
                        policy_name="정책1",
                        action_type="RECOMMENDED",
                    ),
                ],
                actions=["recommend"],
                evidences=[
                    AssistantMessageEvidence(
                        chunk_id="101",
                        snippet="근거",
                        source_title="정책1",
                        source_url="https://example.com/1",
                        evidence_role="SUMMARY",
                    ),
                ],
                disclaimer=True,
            ),
        )),
    )

    with TestClient(_build_app()) as client:
        resp = client.post(
            "/api/v1/chat/sessions/9/messages",
            json={"content": "추천해줘"},
        )

    assert resp.status_code == 201
    am = resp.json()["data"]["assistant_message"]

    # 신규 필드가 JSON으로 직렬화되는지
    assert am["policies"][0]["action_type"] == "RECOMMENDED"
    assert am["evidences"][0]["chunk_id"] == "101"
    # evidence_role lowercase 변환 (Pydantic field_validator)
    assert am["evidences"][0]["evidence_role"] == "summary"
    assert am["actions"] == ["recommend"]
    assert am["disclaimer"] is True


def test_list_chat_messages_includes_normalized_join(monkeypatch) -> None:
    monkeypatch.setattr(
        ChatService,
        "list_messages",
        AsyncMock(return_value=ChatMessageListResponse(
            chat_session_id="9",
            messages=[
                ChatMessageItem(
                    chat_message_id="11",
                    role="user",
                    message_type="TEXT",
                    content="추천해줘",
                    sequence_no=1,
                    created_at=None,
                ),
                ChatMessageItem(
                    chat_message_id="12",
                    role="assistant",
                    message_type="TEXT",
                    content="답변",
                    sequence_no=2,
                    created_at=None,
                    user_status=None,
                    sources=[],
                    policies=[
                        AssistantMessagePolicy(
                            policy_id="42",
                            slug="WLF1",
                            policy_name="정책1",
                            action_type="RECOMMENDED",
                        ),
                    ],
                    actions=["recommend"],
                    evidences=[
                        AssistantMessageEvidence(
                            chunk_id="101",
                            snippet="근거",
                            source_title="정책1",
                            source_url="https://example.com/1",
                            evidence_role="SUMMARY",
                        ),
                    ],
                    disclaimer=True,
                ),
            ],
        )),
    )

    with TestClient(_build_app()) as client:
        resp = client.get("/api/v1/chat/sessions/9/messages")

    assert resp.status_code == 200
    messages = resp.json()["data"]["messages"]
    assert len(messages) == 2

    user_msg = messages[0]
    assert user_msg["role"] == "user"
    assert user_msg["policies"] == []
    assert user_msg["evidences"] == []

    assistant_msg = messages[1]
    assert assistant_msg["policies"][0]["action_type"] == "RECOMMENDED"
    assert assistant_msg["evidences"][0]["chunk_id"] == "101"
    assert assistant_msg["evidences"][0]["evidence_role"] == "summary"
    assert assistant_msg["actions"] == ["recommend"]


def test_to_message_item_uses_structured_policy_fallback_without_duplicate_kwargs() -> None:
    message = SimpleNamespace(
        chat_message_id=12,
        role="assistant",
        message_type="TEXT",
        content="답변",
        sequence_no=2,
        created_at=None,
        structured_json={
            "policies": [
                {
                    "policy_id": "42",
                    "slug": "WLF1",
                    "policy_name": "정책1",
                    "summary": "요약",
                    "action_type": "RECOMMENDED",
                }
            ],
            "actions": ["recommend"],
            "disclaimer": True,
        },
    )

    item = _to_message_item(message, policies=[], evidences=[])

    assert item.chat_message_id == "12"
    assert item.policies[0].policy_id == "42"
    assert item.policies[0].summary == "요약"
    assert item.policies[0].action_type == "RECOMMENDED"
    assert item.actions == ["recommend"]


def test_to_message_item_merges_cached_policy_summary_into_joined_policy() -> None:
    message = SimpleNamespace(
        chat_message_id=12,
        role="assistant",
        message_type="TEXT",
        content="답변",
        sequence_no=2,
        created_at=None,
        structured_json={
            "policies": [
                {
                    "policy_id": "42",
                    "slug": "WLF1",
                    "policy_name": "정책1",
                    "summary": "저장된 카드 설명",
                    "tag": "추천 이유",
                    "tagTone": "coral",
                    "action_type": "RECOMMENDED",
                }
            ],
        },
    )

    item = _to_message_item(
        message,
        policies=[
            {
                "policy_id": "42",
                "slug": "WLF1",
                "policy_name": "정책1",
                "action_type": "RECOMMENDED",
            }
        ],
        evidences=[],
    )

    assert item.policies[0].summary == "저장된 카드 설명"
    assert item.policies[0].tag == "추천 이유"
    assert item.policies[0].tagTone == "coral"


def test_to_message_item_preserves_suggested_actions_and_policy_selection() -> None:
    """저장된 structured_json에서 suggested_actions·policy_selection이 복원되는지 검증."""
    candidates = [{"slug": "WLF1", "policy_name": "A 정책"}, {"slug": "WLF2", "policy_name": "B 정책"}]
    message = SimpleNamespace(
        chat_message_id=99,
        role="assistant",
        message_type="TEXT",
        content="어떤 정책을 말씀하시는 건가요?",
        sequence_no=3,
        created_at=None,
        structured_json={
            "suggested_actions": ["eligibility", "apply"],
            "policy_selection": {"intent": "compare", "candidates": candidates},
            "evidence_review": {
                "verdict": "REVIEW_REQUIRED",
                "reasons": ["incomplete_evidence_provenance"],
            },
        },
    )

    item = _to_message_item(message, policies=[], evidences=[])

    assert item.suggested_actions == ["eligibility", "apply"]
    assert item.policy_selection == {"intent": "compare", "candidates": candidates}
    assert item.evidence_review == {
        "verdict": "REVIEW_REQUIRED",
        "reasons": ["incomplete_evidence_provenance"],
    }


def test_to_message_item_defaults_suggested_actions_when_absent() -> None:
    """structured_json에 두 필드가 없을 때 기본값이 반환되는지 검증."""
    message = SimpleNamespace(
        chat_message_id=100,
        role="assistant",
        message_type="TEXT",
        content="안녕하세요.",
        sequence_no=1,
        created_at=None,
        structured_json={"actions": ["recommend"]},
    )

    item = _to_message_item(message, policies=[], evidences=[])

    assert item.suggested_actions == []
    assert item.policy_selection is None


# --- SSE streaming endpoint ----------------------------------------------


def _stream_response() -> ChatMessageSendResponse:
    return ChatMessageSendResponse(
        chat_session_id="9",
        user_message_id="11",
        assistant_message=AssistantMessage(
            chat_message_id="12",
            content="안녕하세요",
            actions=["recommend"],
            disclaimer=True,
        ),
    )


def _parse_sse_body(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: ")
        events.append(json.loads(block[len("data: ") :]))
    return events


def test_stream_chat_message_returns_sse_token_and_done(monkeypatch) -> None:
    monkeypatch.setattr(
        ChatService,
        "ensure_owned_session",
        AsyncMock(return_value=ChatSession(chat_session_id=9, user_id=5)),
    )

    async def fake_stream(db, *, session, content, idempotency_key=None):
        yield 'data: {"type":"token","delta":"안녕"}\n\n'
        yield 'data: {"type":"token","delta":"하세요"}\n\n'
        yield (
            'data: '
            + json.dumps(
                {"type": "done", "payload": _stream_response().model_dump(mode="json")},
                ensure_ascii=False,
            )
            + "\n\n"
        )

    monkeypatch.setattr(ChatService, "send_message_stream", fake_stream)

    with TestClient(_build_app()) as client:
        resp = client.post(
            "/api/v1/chat/sessions/9/messages/stream",
            json={"content": "추천해줘"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"

    events = _parse_sse_body(resp.text)
    assert [e["type"] for e in events] == ["token", "token", "done"]
    assert events[0]["delta"] == "안녕"
    assert events[2]["payload"]["assistant_message"]["content"] == "안녕하세요"


def test_stream_chat_message_returns_404_for_unknown_session(monkeypatch) -> None:
    async def raise_404(db, *, user_id, chat_session_id):
        raise AppException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ErrorCode.NOT_FOUND,
            message="Chat session not found",
        )

    monkeypatch.setattr(ChatService, "ensure_owned_session", raise_404)
    stream_mock = AsyncMock()
    monkeypatch.setattr(ChatService, "send_message_stream", stream_mock)

    with TestClient(_build_app()) as client:
        resp = client.post(
            "/api/v1/chat/sessions/9/messages/stream",
            json={"content": "추천해줘"},
        )

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"
    stream_mock.assert_not_called()


def test_get_chat_request_status_returns_payload(monkeypatch) -> None:
    status_mock = AsyncMock(return_value=ChatRequestStatusResponse(
        request_id="300",
        chat_session_id="9",
        user_message_id="11",
        status="completed",
        assistant_message_id="12",
        retryable=False,
        payload=_stream_response().model_dump(mode="json"),
    ))
    monkeypatch.setattr(ChatService, "get_request_status", status_mock)

    with TestClient(_build_app()) as client:
        resp = client.get("/api/v1/chat/requests/300")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["request_id"] == "300"
    assert body["status"] == "completed"
    assert body["payload"]["assistant_message"]["content"] == "안녕하세요"
    status_mock.assert_awaited_once()
    assert status_mock.await_args.kwargs["user_id"] == 5
    assert status_mock.await_args.kwargs["request_id"] == 300


def test_get_latest_incomplete_chat_request_returns_null(monkeypatch) -> None:
    latest_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(ChatService, "get_latest_incomplete_request", latest_mock)

    with TestClient(_build_app()) as client:
        resp = client.get("/api/v1/chat/sessions/9/requests/incomplete/latest")

    assert resp.status_code == 200
    assert resp.json()["data"] is None
    latest_mock.assert_awaited_once()
    assert latest_mock.await_args.kwargs["chat_session_id"] == 9
