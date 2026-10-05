import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat_message import ChatMessage
from app.repositories.chat_repository import ChatRepository
from app.repositories.policy_repository import PolicyRepository
from app.schemas.chat_schema import (
    ApplyCard,
    AssistantMessage,
    AssistantMessageEvidence,
    AssistantMessagePolicy,
)
from app.services.chat.persistence._slot_builder import build_next_slot

logger = logging.getLogger(__name__)


def fallback_payload() -> dict:
    return {
        "content": "죄송합니다. 답변을 생성하는 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요.",
        "user_status": None,
        "easy_summary": None,
        "key_points": [],
        "sources": [],
        "policies": [],
        "evidences": [],
        "actions": [],
        "apply_card": None,
        "disclaimer": False,
        "slot_request": None,
        "profile_confirm": None,
        "eligibility_result": None,
        "suggested_actions": [],
        "policy_selection": None,
        "evidence_review": None,
        "claim_evidence_links": [],
    }


def build_structured_json(decision: dict, payload: dict) -> dict:
    return {
        "_supervisor": decision,
        "user_status": payload.get("user_status"),
        "easy_summary": payload.get("easy_summary"),
        "key_points": payload.get("key_points", []),
        "sources": payload.get("sources", []),
        "policies": payload.get("policies", []),
        "actions": payload.get("actions", []),
        "similar_policies": payload.get("similar_policies", []),
        "apply_card": payload.get("apply_card"),
        "disclaimer": payload.get("disclaimer"),
        "slot_request": payload.get("slot_request"),
        "profile_confirm": payload.get("profile_confirm"),
        "eligibility_result": payload.get("eligibility_result"),
        "suggested_actions": payload.get("suggested_actions", []),
        "policy_selection": payload.get("policy_selection"),
        "evidence_review": payload.get("evidence_review"),
        "claim_evidence_links": payload.get("claim_evidence_links", []),
    }


def build_policy_link_rows(
    chat_message_id: int,
    policy_links: list[dict],
    slug_to_id: dict[str, int],
) -> list[dict]:
    rows: list[dict] = []
    for link in policy_links:
        slug = link.get("policy_slug")
        policy_id = slug_to_id.get(slug) if slug else None
        if policy_id is None:
            continue
        rows.append({
            "chat_message_id": chat_message_id,
            "policy_id": policy_id,
            "action_type": link["action_type"],
        })
    return rows


def build_evidence_rows(
    chat_message_id: int, evidences: list[dict]
) -> list[dict]:
    rows: list[dict] = []
    for ev in evidences:
        chunk_id = ev.get("chunk_id")
        if chunk_id is None:
            continue
        rows.append({
            "chat_message_id": chat_message_id,
            "chunk_id": chunk_id,
            "snippet": ev.get("snippet"),
            "evidence_role": ev.get("evidence_role"),
        })
    return rows


async def resolve_policy_ids(
    db: AsyncSession, policy_links: list[dict]
) -> dict[str, int]:
    slugs = [link["policy_slug"] for link in policy_links if link.get("policy_slug")]
    if not slugs:
        return {}
    slug_to_id = await PolicyRepository.find_ids_by_codes(db, list(set(slugs)))
    missing = [s for s in slugs if s not in slug_to_id]
    if missing:
        logger.warning("Skipping policy links for unknown slugs: %s", missing)
    return slug_to_id


def build_assistant_response(
    assistant_message: ChatMessage,
    payload: dict,
    policy_links: list[dict],
    slug_to_id: dict[str, int],
) -> AssistantMessage:
    slug_to_action = {
        link["policy_slug"]: link["action_type"]
        for link in policy_links
        if link.get("policy_slug") in slug_to_id
    }
    policies = [
        AssistantMessagePolicy(
            policy_id=str(slug_to_id[p["slug"]]) if p.get("slug") in slug_to_id else str(p["policy_id"]),
            slug=p["slug"],
            policy_name=p["policy_name"],
            summary=p.get("summary"),
            tag=p.get("tag"),
            tagTone=p.get("tagTone"),
            action_type=slug_to_action.get(p.get("slug")),
            recommendation_request_id=(
                str(p.get("recommendation_request_id"))
                if p.get("recommendation_request_id") is not None
                else None
            ),
            source_ref_id=(
                str(p.get("source_ref_id"))
                if p.get("source_ref_id") is not None
                else None
            ),
            selected_conditions=p.get("selected_conditions"),
            merged_condition_json=p.get("merged_condition_json"),
        )
        for p in payload.get("policies", [])
    ]
    evidences = [
        AssistantMessageEvidence(
            chunk_id=str(ev["chunk_id"]) if ev.get("chunk_id") is not None else None,
            snippet=ev.get("snippet", ""),
            source_title=ev.get("source_title"),
            source_url=ev.get("source_url"),
            evidence_role=ev.get("evidence_role"),
        )
        for ev in payload.get("evidences", [])
    ]
    apply_card_payload = payload.get("apply_card")
    apply_card = ApplyCard(**apply_card_payload) if apply_card_payload else None
    return AssistantMessage(
        chat_message_id=str(assistant_message.chat_message_id),
        content=payload.get("content") or "",
        user_status=payload.get("user_status"),
        easy_summary=payload.get("easy_summary"),
        key_points=payload.get("key_points", []),
        sources=payload.get("sources", []),
        policies=policies,
        actions=payload.get("actions", []),
        evidences=evidences,
        similar_policies=payload.get("similar_policies", []),
        apply_card=apply_card,
        disclaimer=payload.get("disclaimer"),
        slot_request=payload.get("slot_request"),
        profile_confirm=payload.get("profile_confirm"),
        eligibility_result=payload.get("eligibility_result"),
        suggested_actions=payload.get("suggested_actions", []),
        policy_selection=payload.get("policy_selection"),
        evidence_review=payload.get("evidence_review"),
        claim_evidence_links=payload.get("claim_evidence_links", []),
    )


async def persist_assistant_outputs(
    db: AsyncSession,
    *,
    session_id: int,
    user_message_id: int,
    graph_result: dict,
    current_slot: dict | None = None,
) -> tuple[ChatMessage, AssistantMessage]:
    payload = graph_result.get("assistant_payload") or fallback_payload()
    decision = graph_result.get("supervisor_decision") or {
        "intent": "unclear",
        "raw": "missing",
    }
    evidences_to_save = graph_result.get("evidences_to_save", [])
    policy_links_to_save = graph_result.get("policy_links_to_save", [])

    assistant_sequence = await ChatRepository.next_sequence_no(db, session_id)
    structured_json = build_structured_json(decision, payload)
    assistant_message = await ChatRepository.save_message(
        db,
        ChatMessage(
            chat_session_id=session_id,
            parent_message_id=user_message_id,
            role="assistant",
            message_type="TEXT",
            content=payload.get("content"),
            structured_json=structured_json,
            sequence_no=assistant_sequence,
        ),
    )

    slug_to_policy_id = await resolve_policy_ids(db, policy_links_to_save)
    await ChatRepository.bulk_save_message_policies(
        db,
        build_policy_link_rows(
            assistant_message.chat_message_id,
            policy_links_to_save,
            slug_to_policy_id,
        ),
    )
    await ChatRepository.bulk_save_message_evidences(
        db,
        build_evidence_rows(assistant_message.chat_message_id, evidences_to_save),
    )

    # last_result_type 결정: 응답 유형에 따라 슬롯에 기록
    _last_result_type: str | None = None
    if payload.get("slot_request"):
        _last_result_type = "slot_request"
    elif payload.get("profile_confirm"):
        _last_result_type = "profile_confirm"
    elif payload.get("policy_selection"):
        _last_result_type = "policy_selection"
    elif payload.get("eligibility_result"):
        _last_result_type = "eligibility_result"
    elif payload.get("apply_card"):
        _last_result_type = "apply_card"
    elif payload.get("policies"):
        _last_result_type = "policy_list"
    elif payload.get("content"):
        _last_result_type = "text"

    _last_intent: str | None = decision.get("intent") if _last_result_type else None

    next_slot = build_next_slot(
        current_slot=current_slot or {},
        policy_links=policy_links_to_save,
        branch_policies=payload.get("policies", []),
        slug_to_policy_id=slug_to_policy_id,
        profile=graph_result.get("profile"),
        pending=graph_result.get("pending"),
        eligibility_slot_update=graph_result.get("eligibility_slot_update"),
        similar_policies=payload.get("similar_policies", []),
        base_slug=decision.get("resolved_policy_slug"),
        last_intent=_last_intent,
        last_result_type=_last_result_type,
        suggested_actions=payload.get("suggested_actions") or [],
    )
    if next_slot is not None:
        await ChatRepository.update_session_slot(db, session_id, next_slot)

    response = build_assistant_response(
        assistant_message,
        payload,
        policy_links_to_save,
        slug_to_policy_id,
    )
    return assistant_message, response
