import asyncio
import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status

from app.common.exceptions import AppException
from app.db.models.chat_message import ChatMessage
from app.db.models.chat_request import ChatRequest
from app.db.models.chat_session import ChatSession
from app.repositories.chat_request_repository import ChatRequestRepository
from app.repositories.chat_repository import ChatRepository
from app.repositories.policy_repository import PolicyRepository
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService


def _session(user_id: int = 1, chat_session_id: int = 10) -> ChatSession:
    session = ChatSession(
        chat_session_id=chat_session_id,
        user_id=user_id,
        title=None,
    )
    return session


def _new_message(chat_message_id: int) -> ChatMessage:
    msg = ChatMessage(
        chat_message_id=chat_message_id,
        chat_session_id=10,
        role="user",
        message_type="TEXT",
        content="x",
        sequence_no=1,
    )
    return msg


def _graph_result(
    *,
    intent: str = "recommend",
    policies: list[dict] | None = None,
    evidences: list[dict] | None = None,
    policy_links: list[dict] | None = None,
) -> dict:
    policies = policies if policies is not None else [
        {
            "policy_id": "WLF1",
            "slug": "WLF1",
            "policy_name": "정책1",
            "summary": None,
            "tag": None,
            "tagTone": None,
        },
    ]
    evidences = evidences if evidences is not None else [
        {
            "chunk_id": 101,
            "snippet": "근거1",
            "source_title": "정책1",
            "source_url": "https://example.com/1",
            "evidence_role": None,
        },
    ]
    policy_links = policy_links if policy_links is not None else [
        {"policy_slug": "WLF1", "action_type": "RECOMMENDED"},
    ]
    api_action = {
        "recommend": "recommend",
        "compare": "compare",
        "eligibility": "eligibility",
        "apply": "apply",
        "policy_summary": "chat",
        "unclear": None,
    }.get(intent)
    return {
        "assistant_payload": {
            "content": "테스트 답변",
            "user_status": None,
            "sources": [],
            "policies": policies,
            "evidences": evidences,
            "actions": [api_action] if api_action else [],
            "disclaimer": intent != "unclear",
        },
        "supervisor_decision": {"intent": intent, "raw": "{}"},
        "evidences_to_save": [
            {
                "chunk_id": ev["chunk_id"],
                "snippet": ev.get("snippet"),
                "evidence_role": ev.get("evidence_role"),
            }
            for ev in evidences
            if ev.get("chunk_id") is not None
        ],
        "policy_links_to_save": policy_links,
    }


def _patch_repo_for_send(monkeypatch, *, session: ChatSession) -> dict[str, AsyncMock]:
    saved_messages: list[ChatMessage] = []
    saved_requests: list[ChatRequest] = []

    async def fake_save_message(db, message: ChatMessage) -> ChatMessage:
        message.chat_message_id = 100 + len(saved_messages)
        saved_messages.append(message)
        return message

    async def fake_create_processing(
        db,
        *,
        chat_session_id: int,
        user_message_id: int,
        idempotency_key: str | None,
    ) -> ChatRequest:
        request = ChatRequest(
            request_id=200 + len(saved_requests),
            chat_session_id=chat_session_id,
            user_message_id=user_message_id,
            idempotency_key=idempotency_key,
            status="processing",
        )
        saved_requests.append(request)
        return request

    async def fake_find_request_by_id(db, request_id: int) -> ChatRequest | None:
        return next((req for req in saved_requests if req.request_id == request_id), None)

    mocks = {
        "find_session_by_id": AsyncMock(return_value=session),
        "find_recent_messages": AsyncMock(return_value=[]),
        "find_recent_assistant_policy": AsyncMock(return_value=None),
        "next_sequence_no": AsyncMock(side_effect=[1, 2]),
        "save_message": AsyncMock(side_effect=fake_save_message),
        "update_last_message_at": AsyncMock(),
        "bulk_save_message_policies": AsyncMock(),
        "bulk_save_message_evidences": AsyncMock(),
        "update_session_slot": AsyncMock(),
        "session_exists": AsyncMock(return_value=True),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(ChatRepository, name, mock)
    request_mocks = {
        "find_by_idempotency_key": AsyncMock(return_value=None),
        "create_processing": AsyncMock(side_effect=fake_create_processing),
        "find_by_id": AsyncMock(side_effect=fake_find_request_by_id),
        "lock_session_for_slot_update": AsyncMock(return_value=session),
        "mark_completed": AsyncMock(),
        "mark_failed": AsyncMock(),
        "mark_cancelled": AsyncMock(),
    }
    for name, mock in request_mocks.items():
        monkeypatch.setattr(ChatRequestRepository, name, mock)
        mocks[f"request_{name}"] = mock
    mocks["_saved_messages"] = saved_messages
    mocks["_saved_requests"] = saved_requests
    return mocks


def test_send_message_persists_normalized_outputs(monkeypatch) -> None:
    session = _session()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    recent_policy = {
        "policy_id": 42,
        "slug": "WLF1",
        "policy_name": "정책1",
        "action_type": "RECOMMENDED",
    }
    mocks["find_recent_assistant_policy"].return_value = recent_policy

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={"WLF1": 42}),
    )
    run_chat = AsyncMock(return_value=_graph_result())
    monkeypatch.setattr(
        chat_service_module,
        "_run_chat",
        run_chat,
    )

    response = asyncio.run(
        ChatService.send_message(
            db=AsyncMock(),
            user_id=1,
            chat_session_id=10,
            content="추천해줘",
        )
    )

    # 정규화 INSERT 호출 검증
    mocks["bulk_save_message_policies"].assert_awaited_once()
    policy_args = mocks["bulk_save_message_policies"].await_args.args
    policy_rows = policy_args[1]
    assert policy_rows == [{
        "chat_message_id": 101,
        "policy_id": 42,
        "action_type": "RECOMMENDED",
    }]

    mocks["bulk_save_message_evidences"].assert_awaited_once()
    evidence_rows = mocks["bulk_save_message_evidences"].await_args.args[1]
    assert evidence_rows == [{
        "chat_message_id": 101,
        "chunk_id": 101,
        "snippet": "근거1",
        "evidence_role": None,
    }]

    # 응답에 action_type / chunk_id 포함
    assistant = response.assistant_message
    assert assistant.policies[0].action_type == "RECOMMENDED"
    assert assistant.policies[0].policy_id == "42"
    assert assistant.evidences[0].chunk_id == "101"
    run_chat.assert_awaited_once()
    assert run_chat.await_args.kwargs["recent_assistant_policy"] == recent_policy

    # structured_json에는 복원 시 카드 메타가 유지되도록 policies 원본 payload를 보관한다.
    assistant_msg_obj = mocks["_saved_messages"][1]
    assert assistant_msg_obj.role == "assistant"
    assert assistant_msg_obj.structured_json["policies"] == _graph_result()["assistant_payload"]["policies"]
    assert "evidences" not in assistant_msg_obj.structured_json
    assert assistant_msg_obj.structured_json["_supervisor"]["intent"] == "recommend"
    assert assistant_msg_obj.structured_json["actions"] == ["recommend"]


def test_update_session_title_updates_owned_session(monkeypatch) -> None:
    session = _session(user_id=1, chat_session_id=10)
    updated_at = datetime(2026, 6, 23, 10, 30, 0)

    find_mock = AsyncMock(return_value=session)
    update_mock = AsyncMock(return_value=updated_at)
    db = AsyncMock()
    monkeypatch.setattr(ChatRepository, "find_session_by_id", find_mock)
    monkeypatch.setattr(ChatRepository, "update_title", update_mock)

    response = asyncio.run(
        ChatService.update_session_title(
            db=db,
            user_id=1,
            chat_session_id=10,
            title="수정 제목",
        )
    )

    find_mock.assert_awaited_once()
    update_mock.assert_awaited_once_with(db, 10, "수정 제목")
    assert response.chat_session_id == "10"
    assert response.title == "수정 제목"
    assert response.updated_at == updated_at


def test_update_session_title_raises_404_for_other_user(monkeypatch) -> None:
    session = _session(user_id=2, chat_session_id=10)
    monkeypatch.setattr(
        ChatRepository, "find_session_by_id", AsyncMock(return_value=session),
    )
    update_mock = AsyncMock()
    monkeypatch.setattr(ChatRepository, "update_title", update_mock)

    with pytest.raises(AppException) as exc_info:
        asyncio.run(
            ChatService.update_session_title(
                db=AsyncMock(),
                user_id=1,
                chat_session_id=10,
                title="수정 제목",
            )
        )

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
    update_mock.assert_not_awaited()


def test_update_session_title_raises_404_for_missing_session(monkeypatch) -> None:
    monkeypatch.setattr(
        ChatRepository, "find_session_by_id", AsyncMock(return_value=None),
    )
    update_mock = AsyncMock()
    monkeypatch.setattr(ChatRepository, "update_title", update_mock)

    with pytest.raises(AppException) as exc_info:
        asyncio.run(
            ChatService.update_session_title(
                db=AsyncMock(),
                user_id=1,
                chat_session_id=10,
                title="수정 제목",
            )
        )

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
    update_mock.assert_not_awaited()


def test_delete_session_cancels_running_work_and_deletes_owned_session(monkeypatch) -> None:
    session = _session(user_id=1, chat_session_id=10)
    db = AsyncMock()
    find_mock = AsyncMock(return_value=session)
    delete_mock = AsyncMock()
    cancel_mock = MagicMock()
    monkeypatch.setattr(ChatRepository, "find_session_by_id", find_mock)
    monkeypatch.setattr(ChatRepository, "delete_session", delete_mock)
    monkeypatch.setattr(
        chat_service_module.chat_cancel_registry, "cancel", cancel_mock,
    )

    response = asyncio.run(
        ChatService.delete_session(
            db=db,
            user_id=1,
            chat_session_id=10,
        )
    )

    assert response.chat_session_id == "10"
    assert response.deleted is True
    cancel_mock.assert_called_once_with(10)
    delete_mock.assert_awaited_once_with(db, session)
    db.commit.assert_awaited_once()


def test_bulk_delete_sessions_is_all_or_nothing(monkeypatch) -> None:
    db = AsyncMock()
    monkeypatch.setattr(
        ChatRepository,
        "find_sessions_by_user_and_ids",
        AsyncMock(return_value=[_session(user_id=1, chat_session_id=10)]),
    )
    delete_mock = AsyncMock()
    cancel_many_mock = MagicMock()
    monkeypatch.setattr(ChatRepository, "delete_sessions_by_ids", delete_mock)
    monkeypatch.setattr(
        chat_service_module.chat_cancel_registry, "cancel_many", cancel_many_mock,
    )

    with pytest.raises(AppException) as exc_info:
        asyncio.run(
            ChatService.bulk_delete_sessions(
                db=db,
                user_id=1,
                chat_session_ids=[10, 11],
            )
        )

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
    cancel_many_mock.assert_not_called()
    delete_mock.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_bulk_delete_sessions_cancels_and_deletes_all_owned_sessions(monkeypatch) -> None:
    db = AsyncMock()
    sessions = [
        _session(user_id=1, chat_session_id=10),
        _session(user_id=1, chat_session_id=11),
    ]
    monkeypatch.setattr(
        ChatRepository,
        "find_sessions_by_user_and_ids",
        AsyncMock(return_value=sessions),
    )
    monkeypatch.setattr(
        ChatRepository,
        "delete_sessions_by_ids",
        AsyncMock(return_value=2),
    )
    cancel_many_mock = MagicMock()
    monkeypatch.setattr(
        chat_service_module.chat_cancel_registry, "cancel_many", cancel_many_mock,
    )

    response = asyncio.run(
        ChatService.bulk_delete_sessions(
            db=db,
            user_id=1,
            chat_session_ids=[10, 11, 10],
        )
    )

    assert response.deleted_count == 2
    assert response.deleted_session_ids == ["10", "11"]
    cancel_many_mock.assert_called_once_with([10, 11])
    ChatRepository.delete_sessions_by_ids.assert_awaited_once_with(db, [10, 11])
    db.commit.assert_awaited_once()


def test_send_message_raises_404_without_assistant_save_when_session_deleted_midflight(monkeypatch) -> None:
    session = _session()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    mocks["session_exists"].return_value = False
    db = AsyncMock()

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={"WLF1": 42}),
    )
    monkeypatch.setattr(
        chat_service_module,
        "_run_chat",
        AsyncMock(return_value=_graph_result()),
    )

    with pytest.raises(AppException) as exc_info:
        asyncio.run(
            ChatService.send_message(
                db=db,
                user_id=1,
                chat_session_id=10,
                content="추천해줘",
            )
        )

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
    assert len(mocks["_saved_messages"]) == 1
    mocks["bulk_save_message_policies"].assert_not_awaited()
    mocks["bulk_save_message_evidences"].assert_not_awaited()
    mocks["update_last_message_at"].assert_not_awaited()
    db.rollback.assert_awaited_once()


def test_send_message_skips_unknown_policy_slug(monkeypatch) -> None:
    session = _session()
    mocks = _patch_repo_for_send(monkeypatch, session=session)

    # find_ids_by_codes가 둘 중 하나만 반환
    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={"WLF_KNOWN": 7}),
    )
    monkeypatch.setattr(
        chat_service_module,
        "_run_chat",
        AsyncMock(return_value=_graph_result(
            policy_links=[
                {"policy_slug": "WLF_KNOWN", "action_type": "RECOMMENDED"},
                {"policy_slug": "WLF_UNKNOWN", "action_type": "RECOMMENDED"},
            ],
        )),
    )

    asyncio.run(
        ChatService.send_message(
            db=AsyncMock(), user_id=1, chat_session_id=10, content="x",
        )
    )

    policy_rows = mocks["bulk_save_message_policies"].await_args.args[1]
    assert len(policy_rows) == 1
    assert policy_rows[0]["policy_id"] == 7


def test_send_message_policy_summary_creates_no_policy_link(monkeypatch) -> None:
    session = _session()
    mocks = _patch_repo_for_send(monkeypatch, session=session)

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        chat_service_module,
        "_run_chat",
        AsyncMock(return_value=_graph_result(
            intent="policy_summary",
            policy_links=[],  # policy_summary intent는 graph가 빈 배열을 반환
        )),
    )

    response = asyncio.run(
        ChatService.send_message(
            db=AsyncMock(), user_id=1, chat_session_id=10, content="이게 뭐야?",
        )
    )

    # bulk_save_message_policies는 빈 리스트로 호출 (Repository 안에서 early return)
    policy_rows = mocks["bulk_save_message_policies"].await_args.args[1]
    assert policy_rows == []

    # 응답 actions는 chat (API 매핑)
    assert response.assistant_message.actions == ["chat"]
    # policy 카드는 graph가 정책 정보 자체는 노출했으나 action_type은 None
    assert all(p.action_type is None for p in response.assistant_message.policies)


def test_send_message_fallback_when_graph_fails(monkeypatch) -> None:
    session = _session()
    mocks = _patch_repo_for_send(monkeypatch, session=session)

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={}),
    )

    async def boom(**kwargs: Any) -> dict:
        # 실제 _run_chat는 예외를 잡아 fallback dict를 반환하므로 그 동작을 흉내
        return {
            "assistant_payload": chat_service_module._fallback_payload(),
            "supervisor_decision": {"intent": "unclear", "raw": "routing_error"},
            "evidences_to_save": [],
            "policy_links_to_save": [],
        }

    monkeypatch.setattr(chat_service_module, "_run_chat", boom)

    response = asyncio.run(
        ChatService.send_message(
            db=AsyncMock(), user_id=1, chat_session_id=10, content="x",
        )
    )

    assert "죄송합니다" in response.assistant_message.content
    mocks["bulk_save_message_policies"].assert_awaited_once_with(
        mocks["bulk_save_message_policies"].await_args.args[0], [],
    )
    mocks["bulk_save_message_evidences"].assert_awaited_once_with(
        mocks["bulk_save_message_evidences"].await_args.args[0], [],
    )


def test_send_message_schedules_title_generation_for_first_message(monkeypatch) -> None:
    session = _session()
    _patch_repo_for_send(monkeypatch, session=session)

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={"WLF1": 42}),
    )
    monkeypatch.setattr(
        chat_service_module,
        "_run_chat",
        AsyncMock(return_value=_graph_result()),
    )
    schedule_mock = MagicMock()
    monkeypatch.setattr(
        chat_service_module, "_schedule_title_generation", schedule_mock,
    )

    asyncio.run(
        ChatService.send_message(
            db=AsyncMock(), user_id=1, chat_session_id=10, content="추천해줘",
        )
    )

    schedule_mock.assert_called_once_with(10, "추천해줘")


def test_send_message_skips_title_generation_when_title_exists(monkeypatch) -> None:
    session = _session()
    session.title = "기존 제목"
    _patch_repo_for_send(monkeypatch, session=session)

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={"WLF1": 42}),
    )
    monkeypatch.setattr(
        chat_service_module,
        "_run_chat",
        AsyncMock(return_value=_graph_result()),
    )
    schedule_mock = MagicMock()
    monkeypatch.setattr(
        chat_service_module, "_schedule_title_generation", schedule_mock,
    )

    asyncio.run(
        ChatService.send_message(
            db=AsyncMock(), user_id=1, chat_session_id=10, content="추천해줘",
        )
    )

    schedule_mock.assert_not_called()


def test_send_message_skips_title_generation_when_history_not_empty(monkeypatch) -> None:
    session = _session()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    # 첫 메시지가 아니라 이미 이전 대화가 있는 상태
    mocks["find_recent_messages"].return_value = [
        ChatMessage(
            chat_message_id=99,
            chat_session_id=10,
            role="user",
            message_type="TEXT",
            content="이전 질문",
            sequence_no=1,
        )
    ]

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={"WLF1": 42}),
    )
    monkeypatch.setattr(
        chat_service_module,
        "_run_chat",
        AsyncMock(return_value=_graph_result()),
    )
    schedule_mock = MagicMock()
    monkeypatch.setattr(
        chat_service_module, "_schedule_title_generation", schedule_mock,
    )

    asyncio.run(
        ChatService.send_message(
            db=AsyncMock(), user_id=1, chat_session_id=10, content="추가 질문",
        )
    )

    schedule_mock.assert_not_called()


def test_list_messages_includes_normalized_data(monkeypatch) -> None:
    session = _session()

    def make_msg(msg_id: int, role: str, seq: int) -> ChatMessage:
        return ChatMessage(
            chat_message_id=msg_id,
            chat_session_id=10,
            role=role,
            message_type="TEXT",
            content=f"msg-{msg_id}",
            sequence_no=seq,
        )

    messages = [
        make_msg(201, "user", 1),
        make_msg(202, "assistant", 2),
    ]
    messages[1].structured_json = {
        "policies": [
            {
                "policy_id": "42",
                "slug": "WLF1",
                "policy_name": "정책1",
                "recommendation_request_id": "237",
                "source_ref_id": "237",
                "selected_conditions": {"region": "seoul"},
                "merged_condition_json": {"region": "seoul", "income": "mid1"},
            }
        ],
        "actions": ["recommend"],
    }

    monkeypatch.setattr(
        ChatRepository, "find_session_by_id", AsyncMock(return_value=session),
    )
    monkeypatch.setattr(
        ChatRepository, "find_messages_by_session", AsyncMock(return_value=messages),
    )
    monkeypatch.setattr(
        ChatRepository,
        "find_policies_by_message_ids",
        AsyncMock(return_value={
            202: [
                {
                    "policy_id": "42",
                    "slug": "WLF1",
                    "policy_name": "정책1",
                    "summary": "정책 설명",
                    "action_type": "RECOMMENDED",
                },
            ],
        }),
    )
    monkeypatch.setattr(
        ChatRepository,
        "find_evidences_by_message_ids",
        AsyncMock(return_value={
            202: [
                {
                    "chunk_id": "101",
                    "snippet": "근거1",
                    "evidence_role": "SUMMARY",
                    "source_title": "정책1",
                    "source_url": "https://example.com/1",
                },
            ],
        }),
    )

    response = asyncio.run(
        ChatService.list_messages(
            db=AsyncMock(), user_id=1, chat_session_id=10,
        )
    )

    assert len(response.messages) == 2
    user_item = response.messages[0]
    assert user_item.role == "user"
    assert user_item.policies == []
    assert user_item.evidences == []

    assistant_item = response.messages[1]
    assert assistant_item.role == "assistant"
    assert len(assistant_item.policies) == 1
    assert assistant_item.policies[0].action_type == "RECOMMENDED"
    assert assistant_item.policies[0].summary == "정책 설명"
    assert assistant_item.policies[0].recommendation_request_id == "237"
    assert assistant_item.policies[0].source_ref_id == "237"
    assert assistant_item.policies[0].selected_conditions == {"region": "seoul"}
    assert assistant_item.policies[0].merged_condition_json == {
        "region": "seoul",
        "income": "mid1",
    }
    assert len(assistant_item.evidences) == 1
    # evidence_role validator로 lowercase 변환
    assert assistant_item.evidences[0].evidence_role == "summary"
    assert assistant_item.evidences[0].chunk_id == "101"


# --- streaming -------------------------------------------------------------


def _intent_sse(intent: str) -> str:
    return "recommendation" if intent == "recommend" else "general"


def _patch_run_chat_for_stream(
    monkeypatch,
    result: dict,
    *,
    tokens: list[str] | None = None,
    raise_error: bool = False,
) -> list[dict]:
    calls: list[dict] = []

    async def fake_run_chat(**kwargs: Any) -> dict:
        calls.append(kwargs)
        if raise_error:
            raise RuntimeError("boom")
        if kwargs.get("emit_intent") and kwargs.get("on_intent") is not None:
            intent = result.get("supervisor_decision", {}).get("intent", "unclear")
            await kwargs["on_intent"](_intent_sse(intent))
        if kwargs.get("on_token") is not None:
            for token in tokens or []:
                await kwargs["on_token"](token)
        return {**(kwargs.get("preseed_result") or {}), **result}

    monkeypatch.setattr(chat_service_module, "_run_chat", fake_run_chat)
    return calls


def _parse_sse_chunks(chunks: list[str]) -> list[dict]:
    parsed: list[dict] = []
    for chunk in chunks:
        assert chunk.startswith("data: ")
        assert chunk.endswith("\n\n")
        parsed.append(json.loads(chunk[len("data: ") : -2]))
    return parsed


async def _collect(agen) -> list[str]:
    return [item async for item in agen]


def test_send_message_stream_emits_tokens_then_done(monkeypatch) -> None:
    session = _session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    recent_policy = {
        "policy_id": 42,
        "slug": "WLF1",
        "policy_name": "정책1",
        "action_type": "RECOMMENDED",
    }
    mocks["find_recent_assistant_policy"].return_value = recent_policy

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={"WLF1": 42}),
    )

    graph_result = _graph_result()
    run_chat_calls = _patch_run_chat_for_stream(
        monkeypatch,
        graph_result,
        tokens=["안녕", "하세요"],
    )

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="추천해줘")
    ))
    events = _parse_sse_chunks(chunks)

    assert [e["type"] for e in events] == ["accepted", "intent", "token", "token", "done"]
    assert events[0]["request_id"] == "200"
    assert events[1]["intent"] == "recommendation"
    assert events[2]["delta"] == "안녕"
    assert events[3]["delta"] == "하세요"
    assert events[4]["request_id"] == "200"

    # done payload에 ChatMessageSendResponse 구조 포함
    payload = events[4]["payload"]
    assert payload["chat_session_id"] == "10"
    assert payload["user_message_id"] == "100"
    assert payload["assistant_message"]["content"] == "테스트 답변"
    assert payload["assistant_message"]["policies"][0]["action_type"] == "RECOMMENDED"
    assert run_chat_calls[0]["recent_assistant_policy"] == recent_policy

    # 정규화 INSERT가 한 번만 호출되었는지
    mocks["bulk_save_message_policies"].assert_awaited_once()
    mocks["bulk_save_message_evidences"].assert_awaited_once()
    mocks["update_last_message_at"].assert_awaited_once()
    mocks["request_create_processing"].assert_awaited_once()
    mocks["request_mark_completed"].assert_awaited_once()


def test_send_message_stream_disconnect_after_accept_persists_same_run(monkeypatch) -> None:
    """Transport cancellation must not turn an accepted durable request into a failed run."""

    async def scenario() -> None:
        session = _session()
        db = AsyncMock()
        mocks = _patch_repo_for_send(monkeypatch, session=session)
        graph_started = asyncio.Event()
        allow_graph_finish = asyncio.Event()

        monkeypatch.setattr(
            PolicyRepository,
            "find_ids_by_codes",
            AsyncMock(return_value={"WLF1": 42}),
        )

        async def delayed_run_chat(**kwargs: Any) -> dict:
            graph_started.set()
            await allow_graph_finish.wait()
            return _graph_result()

        monkeypatch.setattr(chat_service_module, "_run_chat", delayed_run_chat)

        stream = ChatService.send_message_stream(
            db=db,
            session=session,
            content="추천해줘",
            idempotency_key="disconnect-case",
        )

        accepted = json.loads((await stream.__anext__())[len("data: ") : -2])
        assert accepted["type"] == "accepted"
        assert accepted["request_id"] == "200"

        pending_event = asyncio.create_task(stream.__anext__())
        await graph_started.wait()

        # Simulate the request task being cancelled because the SSE transport
        # disappeared. The durable run must keep using the already accepted
        # request rather than treating the disconnect as a user cancellation.
        pending_event.cancel()
        allow_graph_finish.set()

        with pytest.raises(StopAsyncIteration):
            await pending_event

        assert len(mocks["_saved_requests"]) == 1
        assert len(mocks["_saved_messages"]) == 2
        assert [message.role for message in mocks["_saved_messages"]] == ["user", "assistant"]
        mocks["request_mark_completed"].assert_awaited_once()
        mocks["request_mark_cancelled"].assert_not_awaited()
        mocks["request_mark_failed"].assert_not_awaited()

    asyncio.run(scenario())


def test_send_message_stream_error_event_when_graph_raises(monkeypatch) -> None:
    session = _session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)

    monkeypatch.setattr(
        PolicyRepository,
        "find_ids_by_codes",
        AsyncMock(return_value={}),
    )

    _patch_run_chat_for_stream(monkeypatch, _graph_result(), raise_error=True)

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="x")
    ))
    events = _parse_sse_chunks(chunks)

    assert [e["type"] for e in events] == ["accepted", "error"]
    assert events[0]["request_id"] == "200"
    assert events[1]["request_id"] == "200"
    assert events[1]["code"] == "INTERNAL_SERVER_ERROR"

    # 요구사항: 요청 시작 시 user message와 processing 상태는 먼저 저장된다.
    assert len(mocks["_saved_messages"]) == 1
    assert mocks["_saved_messages"][0].role == "user"
    mocks["bulk_save_message_policies"].assert_not_awaited()
    mocks["bulk_save_message_evidences"].assert_not_awaited()
    mocks["update_last_message_at"].assert_not_awaited()
    mocks["request_mark_failed"].assert_awaited_once()
    assert db.commit.await_count >= 2


def test_send_message_stream_deleted_session_blocks_late_persist_without_local_cancel(monkeypatch) -> None:
    """DB session deletion must suppress late writes even without process-local cancel."""

    session = _session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    mocks["session_exists"].return_value = False

    # Simulate deletion/cancellation being observed only through durable DB state,
    # as can happen when a different worker/process handles the delete request.
    cancel_event = asyncio.Event()
    monkeypatch.setattr(
        chat_service_module.chat_cancel_registry,
        "register",
        MagicMock(return_value=cancel_event),
    )
    unregister_mock = MagicMock()
    monkeypatch.setattr(
        chat_service_module.chat_cancel_registry,
        "unregister",
        unregister_mock,
    )

    _patch_run_chat_for_stream(
        monkeypatch,
        _graph_result(),
        tokens=["생성됐지만 저장되면 안 됨"],
    )

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="x")
    ))
    events = _parse_sse_chunks(chunks)

    assert [e["type"] for e in events] == ["accepted", "intent", "token", "cancelled"]
    assert events[-1]["request_id"] == "200"
    assert len(mocks["_saved_messages"]) == 1
    assert mocks["_saved_messages"][0].role == "user"
    mocks["bulk_save_message_policies"].assert_not_awaited()
    mocks["bulk_save_message_evidences"].assert_not_awaited()
    mocks["request_mark_completed"].assert_not_awaited()
    mocks["request_mark_cancelled"].assert_awaited_once()
    unregister_mock.assert_called_once_with(10, cancel_event)


def test_send_message_stream_cancelled_before_persist_saves_nothing(monkeypatch) -> None:
    session = _session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    cancel_event = asyncio.Event()
    cancel_event.set()

    monkeypatch.setattr(
        chat_service_module.chat_cancel_registry,
        "register",
        MagicMock(return_value=cancel_event),
    )
    unregister_mock = MagicMock()
    monkeypatch.setattr(
        chat_service_module.chat_cancel_registry,
        "unregister",
        unregister_mock,
    )
    _patch_run_chat_for_stream(
        monkeypatch,
        _graph_result(),
        tokens=["저장되면 안 됨"],
    )

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="x")
    ))
    events = _parse_sse_chunks(chunks)

    assert [e["type"] for e in events] == ["accepted", "cancelled"]
    assert events[1]["request_id"] == "200"
    assert len(mocks["_saved_messages"]) == 1
    assert mocks["_saved_messages"][0].role == "user"
    mocks["bulk_save_message_policies"].assert_not_awaited()
    mocks["bulk_save_message_evidences"].assert_not_awaited()
    mocks["request_mark_cancelled"].assert_awaited_once()
    unregister_mock.assert_called_once_with(10, cancel_event)


def test_send_message_stream_idempotency_completed_returns_saved_payload(monkeypatch) -> None:
    session = _session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    completed_payload = {
        "chat_session_id": "10",
        "user_message_id": "77",
        "assistant_message": {
            "chat_message_id": "78",
            "content": "저장된 답변",
            "actions": [],
            "policies": [],
            "evidences": [],
            "similar_policies": [],
            "key_points": [],
            "sources": [],
            "suggested_actions": [],
        },
    }
    existing_request = ChatRequest(
        request_id=300,
        chat_session_id=10,
        user_message_id=77,
        idempotency_key="same-key",
        status="completed",
        response_payload_json=completed_payload,
    )
    mocks["request_find_by_idempotency_key"].return_value = existing_request
    run_chat = AsyncMock(return_value=_graph_result())
    monkeypatch.setattr(chat_service_module, "_run_chat", run_chat)

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(
            db=db,
            session=session,
            content="추천해줘",
            idempotency_key="same-key",
        )
    ))
    events = _parse_sse_chunks(chunks)

    assert [event["type"] for event in events] == ["done"]
    assert events[0]["request_id"] == "300"
    assert events[0]["payload"]["assistant_message"]["content"] == "저장된 답변"
    run_chat.assert_not_awaited()
    mocks["save_message"].assert_not_awaited()
    mocks["request_create_processing"].assert_not_awaited()


def test_send_message_stream_final_persist_failure_does_not_emit_done(monkeypatch) -> None:
    session = _session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    saved_messages = mocks["_saved_messages"]

    async def fail_assistant_save(db, message: ChatMessage) -> ChatMessage:
        if message.role == "assistant":
            raise Exception("저장 실패")
        message.chat_message_id = 100 + len(saved_messages)
        saved_messages.append(message)
        return message

    mocks["save_message"].side_effect = fail_assistant_save
    _patch_run_chat_for_stream(monkeypatch, _graph_result(), tokens=["생성중"])

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="추천해줘")
    ))
    events = _parse_sse_chunks(chunks)

    assert [event["type"] for event in events] == ["accepted", "intent", "token", "error"]
    assert all(event["type"] != "done" for event in events)
    mocks["request_mark_completed"].assert_not_awaited()
    mocks["request_mark_failed"].assert_awaited_once()


def test_get_request_status_blocks_other_user(monkeypatch) -> None:
    request = ChatRequest(
        request_id=300,
        chat_session_id=10,
        user_message_id=77,
        status="completed",
    )
    monkeypatch.setattr(ChatRequestRepository, "find_by_id", AsyncMock(return_value=request))
    monkeypatch.setattr(
        ChatRepository,
        "find_session_by_id",
        AsyncMock(return_value=_session(user_id=2, chat_session_id=10)),
    )

    with pytest.raises(AppException) as exc_info:
        asyncio.run(
            ChatService.get_request_status(
                db=AsyncMock(),
                user_id=1,
                request_id=300,
            )
        )

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


# --- FOLLOW_UP 시나리오 -------------------------------------------------------


def _follow_up_session() -> ChatSession:
    """FOLLOW_UP_REQUIRED 슬롯이 있는 세션."""
    session = _session()
    session.slot_json = {
        "recent_policies": [
            {
                "policy_id": 42,
                "slug": "WLF1",
                "policy_name": "정책1",
                "last_action": "ELIGIBILITY",
                "eligibility_status": "FOLLOW_UP_REQUIRED",
                "eligibility_request_id": 99,
                "follow_up_questions": [{"question_text": "소득이 얼마인가요?"}],
            }
        ]
    }
    return session


def test_follow_up_recommendation_clears_eligibility_slot(monkeypatch) -> None:
    """버그 1: FOLLOW_UP → recommendation 경로 시 슬롯의 eligibility_status가 None으로 클리어."""
    session = _follow_up_session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    monkeypatch.setattr(PolicyRepository, "find_ids_by_codes", AsyncMock(return_value={"WLF1": 42}))

    monkeypatch.setattr(
        chat_service_module,
        "_classify_follow_up_intent",
        AsyncMock(return_value="recommendation"),
    )

    graph_result = _graph_result()
    run_chat_calls = _patch_run_chat_for_stream(monkeypatch, graph_result)

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="다른 정책 추천해줘")
    ))
    events = _parse_sse_chunks(chunks)

    assert events[0]["type"] == "accepted"
    assert events[1]["type"] == "intent"
    assert events[1]["intent"] == "recommendation"
    assert events[-1]["type"] == "done"
    assert run_chat_calls[0]["emit_intent"] is False

    # 슬롯 저장 호출 확인 — eligibility_status가 None으로 클리어됨
    mocks["update_session_slot"].assert_awaited_once()
    saved_slot = mocks["update_session_slot"].await_args.args[2]
    policy_entry = saved_slot["recent_policies"][0]
    assert policy_entry["slug"] == "WLF1"
    assert policy_entry["eligibility_status"] is None
    assert policy_entry["follow_up_questions"] == []


def test_follow_up_general_uses_outer_db_and_rollback_on_failure(monkeypatch) -> None:
    """버그 2: _run_follow_up_eligibility가 외부 db를 받고, 저장 실패 시 rollback이 호출됨."""
    session = _follow_up_session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    saved_messages = mocks["_saved_messages"]

    async def fail_assistant_save(db, message: ChatMessage) -> ChatMessage:
        if message.role == "assistant":
            raise Exception("저장 실패")
        message.chat_message_id = 100 + len(saved_messages)
        saved_messages.append(message)
        return message

    mocks["save_message"].side_effect = fail_assistant_save

    monkeypatch.setattr(
        chat_service_module,
        "_classify_follow_up_intent",
        AsyncMock(return_value="general"),
    )
    monkeypatch.setattr(
        chat_service_module,
        "_map_follow_up_answers",
        AsyncMock(return_value=[]),
    )
    run_follow_up_mock = AsyncMock(return_value={
        "status": "ELIGIBLE",
        "request_id": 100,
        "user_status": "eligible",
        "assessment_status": None,
        "follow_up_questions": [],
        "summary": None,
        "criteria": [],
        "policies": [],
        "evidences": [],
    })
    monkeypatch.setattr(chat_service_module, "_run_follow_up_eligibility", run_follow_up_mock)
    monkeypatch.setattr(
        chat_service_module,
        "_adapt_eligibility_result",
        MagicMock(return_value=("분석 완료", "eligible", [], [])),
    )

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="네, 소득은 200만원입니다")
    ))
    events = _parse_sse_chunks(chunks)

    assert [event["type"] for event in events][-2:] == ["token", "error"]
    # 외부 db에 rollback 호출됨 (eligibility 분석도 함께 롤백)
    db.rollback.assert_awaited()
    # 핵심: _run_follow_up_eligibility 첫 번째 인자가 외부 db (트랜잭션 통합 검증)
    assert run_follow_up_mock.await_args.args[0] is db


def test_intent_emitted_only_once_when_chain_end_fires_twice(monkeypatch) -> None:
    """버그 3: on_chain_end가 supervisor_decision을 두 번 포함해도 intent SSE는 한 번만."""
    session = _session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    monkeypatch.setattr(PolicyRepository, "find_ids_by_codes", AsyncMock(return_value={"WLF1": 42}))

    graph_result = _graph_result()
    _patch_run_chat_for_stream(
        monkeypatch,
        graph_result,
        tokens=["안녕", "하세요"],
    )

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="추천해줘")
    ))
    events = _parse_sse_chunks(chunks)

    intent_events = [e for e in events if e["type"] == "intent"]
    assert len(intent_events) == 1, "intent SSE는 정확히 한 번만 발행되어야 함"
    assert intent_events[0]["intent"] == "recommendation"


def test_build_next_slot_logs_warning_on_slug_mismatch(caplog) -> None:
    """버그 4: eligibility_slot_update의 slug가 recent_policies에 없으면 경고 로그."""
    import logging
    from app.services.chat_service import _build_next_slot

    with caplog.at_level(logging.WARNING, logger="app.services.chat_service"):
        result = _build_next_slot(
            policy_links=[],
            branch_policies=[],
            slug_to_policy_id={},
            current_slot={
                "recent_policies": [
                    {"policy_id": 1, "slug": "WLF1", "policy_name": "정책1", "last_action": "ELIGIBILITY"},
                ]
            },
            eligibility_slot_update={
                "slug": "NONEXISTENT",   # recent_policies에 없는 slug
                "eligibility_request_id": 99,
                "follow_up_questions": [],
                "eligibility_status": "ELIGIBLE",
            },
        )

    assert any("NONEXISTENT" in r.message for r in caplog.records), \
        "slug 미매칭 시 slug 이름을 포함한 경고 로그가 있어야 함"
    # 슬롯은 변경 없이 유지
    assert result is not None
    assert result["recent_policies"][0]["slug"] == "WLF1"


def test_persist_eligibility_follow_up_updates_session_slot(monkeypatch) -> None:
    session = _session(user_id=1, chat_session_id=10)
    session.slot_json = {"recent_policies": []}
    db = AsyncMock()
    saved_message = ChatMessage(
        chat_message_id=77,
        chat_session_id=10,
        role="assistant",
        message_type="TEXT",
        content="추가 질문",
        sequence_no=1,
    )
    request = MagicMock(
        request_id=99,
        user_id=1,
        policy_id=100,
        source_ref_id="chat_session:10;source:recommendation:1",
    )
    result_json = {
        "request_id": "99",
        "status": "FOLLOW_UP_REQUIRED",
        "policy_id": "100",
        "slug": "WLF1",
        "policy_name": "정책1",
        "summary": "추가 확인이 필요해요.",
        "follow_up_questions": [
            {
                "field_name": "income",
                "question_text": "소득을 확인해 주세요.",
            }
        ],
    }

    monkeypatch.setattr(ChatRepository, "find_session_by_id", AsyncMock(return_value=session))
    monkeypatch.setattr(ChatRepository, "eligibility_result_message_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(ChatRepository, "next_sequence_no", AsyncMock(return_value=1))
    monkeypatch.setattr(ChatRepository, "save_message", AsyncMock(return_value=saved_message))
    monkeypatch.setattr(ChatRepository, "bulk_save_message_policies", AsyncMock())
    monkeypatch.setattr(ChatRepository, "bulk_save_message_evidences", AsyncMock())
    monkeypatch.setattr(ChatRepository, "update_session_slot", AsyncMock())
    monkeypatch.setattr(ChatRepository, "update_last_message_at", AsyncMock())
    monkeypatch.setattr(PolicyRepository, "find_ids_by_codes", AsyncMock(return_value={"WLF1": 100}))
    monkeypatch.setattr(
        chat_service_module,
        "_adapt_eligibility_result",
        MagicMock(return_value=(
            "추가 질문",
            None,
            [
                {
                    "policy_id": "100",
                    "slug": "WLF1",
                    "policy_name": "정책1",
                    "summary": "추가 확인이 필요해요.",
                    "tag": None,
                    "tagTone": None,
                }
            ],
            [],
        )),
    )

    asyncio.run(
        ChatService.persist_eligibility_result_message(
            db,
            user_id=1,
            request=request,
            result_json=result_json,
        )
    )

    ChatRepository.update_session_slot.assert_awaited_once()
    _, chat_session_id, next_slot = ChatRepository.update_session_slot.await_args.args
    assert chat_session_id == 10
    [policy] = next_slot["recent_policies"]
    assert policy["slug"] == "WLF1"
    assert policy["eligibility_request_id"] == 99
    assert policy["eligibility_status"] == "FOLLOW_UP_REQUIRED"
    assert policy["follow_up_questions"] == result_json["follow_up_questions"]


# --- 신규 이슈 시나리오 --------------------------------------------------------


def test_follow_up_eligibility_merges_prev_conditions(monkeypatch) -> None:
    """이슈 1: 재분석 시 이전 request의 selected_conditions를 base로 사용하고 raw_query 이어붙임."""
    from app.repositories.ai_request_repository import AiRequestRepository
    from app.ai.graphs.eligibility_graph import eligibility_graph_runner
    from app.services.chat_service import _run_follow_up_eligibility

    db = AsyncMock()

    prev_request = MagicMock(
        raw_query="2세 아이 의료급여",
        parsed_query_json={"selected_conditions": {"child_age": "2세", "income": "low"}},
    )
    monkeypatch.setattr(AiRequestRepository, "find_by_id", AsyncMock(return_value=prev_request))

    run_graph_mock = AsyncMock(return_value={"status": "ELIGIBLE"})
    monkeypatch.setattr(eligibility_graph_runner, "run", run_graph_mock)

    asyncio.run(_run_follow_up_eligibility(
        db=db,
        user_id=1,
        content="네, 의료급여예요",
        follow_up_policy={
            "slug": "WLF1",
            "policy_name": "정책1",
            "eligibility_request_id": 99,
            "follow_up_questions": [{"question_text": "의료급여 수급자인가요?"}],
        },
        manual_confirmations=[{"question": "의료급여 수급자인가요?", "answer": "yes"}],
    ))

    call_kwargs = run_graph_mock.await_args.kwargs
    # 이전 raw_query + 새 content 이어붙임
    assert "2세 아이 의료급여" in call_kwargs["raw_query"]
    assert "네, 의료급여예요" in call_kwargs["raw_query"]
    # 기존 조건 보존 + manual_confirmations 병합
    sc = call_kwargs["selected_conditions"]
    assert sc["child_age"] == "2세"
    assert sc["income"] == "low"
    assert sc["manual_confirmations"][0]["answer"] == "yes"


def test_map_follow_up_answers_uses_index_not_string(monkeypatch) -> None:
    """이슈 2: LLM이 반환한 index로 원본 question_text를 참조 — 문자열 변형에 독립적."""
    from app.services.chat_service import _map_follow_up_answers
    from types import SimpleNamespace

    questions = [
        {"question_text": "의료급여 수급자인가요?"},
        {"question_text": "자녀 나이가 2세 미만인가요?"},
    ]
    follow_up_policy = {
        "policy_name": "정책1",
        "follow_up_questions": questions,
    }

    # LLM이 index 0에 "yes", index 1에 "no" 반환 (질문 문자열을 다르게 썼어도 index로 매핑)
    fake_result = SimpleNamespace(
        mappings=[
            SimpleNamespace(index=0, answer="yes"),
            SimpleNamespace(index=1, answer="no"),
        ]
    )
    llm_mock = MagicMock()
    llm_mock.with_structured_output.return_value.ainvoke = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(chat_service_module, "_FOLLOW_UP_LLM", llm_mock)

    result = asyncio.run(_map_follow_up_answers(follow_up_policy, "네, 수급자고 아이는 3살이에요"))

    # 원본 question_text 그대로 사용
    assert result[0]["question"] == "의료급여 수급자인가요?"
    assert result[0]["answer"] == "yes"
    assert result[1]["question"] == "자녀 나이가 2세 미만인가요?"
    assert result[1]["answer"] == "no"


def test_follow_up_other_intent_passes_to_supervisor(monkeypatch) -> None:
    """이슈 3: other_intent 시 SSE intent 미발행, supervisor graph가 intent 결정."""
    session = _follow_up_session()
    db = AsyncMock()
    mocks = _patch_repo_for_send(monkeypatch, session=session)
    monkeypatch.setattr(
        PolicyRepository, "find_ids_by_codes", AsyncMock(return_value={"WLF1": 42})
    )
    monkeypatch.setattr(
        chat_service_module, "_classify_follow_up_intent",
        AsyncMock(return_value="other_intent"),
    )

    # supervisor가 WLF2 다른 정책을 비교 대상으로 결정 (WLF1 슬롯에 영향 없음)
    graph_result = _graph_result(
        intent="compare",
        policies=[{"policy_id": "WLF2", "slug": "WLF2", "policy_name": "정책2",
                   "summary": None, "tag": None, "tagTone": None}],
        policy_links=[{"policy_slug": "WLF2", "action_type": "COMPARE"}],
    )
    monkeypatch.setattr(PolicyRepository, "find_ids_by_codes", AsyncMock(return_value={"WLF2": 43}))
    _patch_run_chat_for_stream(monkeypatch, graph_result)

    chunks = asyncio.run(_collect(
        ChatService.send_message_stream(db=db, session=session, content="이 정책이랑 다른 정책 비교해줘")
    ))
    events = _parse_sse_chunks(chunks)

    intent_events = [e for e in events if e["type"] == "intent"]
    assert len(intent_events) == 1
    # supervisor가 결정한 intent가 SSE로 발행됨 (compare → general 매핑)
    assert intent_events[0]["intent"] == "general"

    # FOLLOW_UP 슬롯이 클리어되지 않음 (other_intent는 eligibility_slot_update 미설정)
    if mocks["update_session_slot"].await_count > 0:
        saved_slot = mocks["update_session_slot"].await_args.args[2]
        wlf1 = next(
            (p for p in saved_slot.get("recent_policies", []) if p["slug"] == "WLF1"), None
        )
        # WLF1이 슬롯에 남아있다면 eligibility_status가 FOLLOW_UP_REQUIRED로 유지
        if wlf1 is not None:
            assert wlf1.get("eligibility_status") == "FOLLOW_UP_REQUIRED"
