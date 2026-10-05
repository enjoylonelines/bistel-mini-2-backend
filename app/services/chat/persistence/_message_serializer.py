from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat_message import ChatMessage
from app.repositories.chat_repository import ChatRepository
from app.schemas.chat_schema import (
    ApplyCard,
    AssistantMessageEvidence,
    AssistantMessagePolicy,
    ChatMessageItem,
)

logger = logging.getLogger(__name__)


def unwrap_message_meta(structured_json: dict | None) -> dict:
    if not structured_json:
        return {}
    apply_card_payload = structured_json.get("apply_card")
    apply_card = ApplyCard(**apply_card_payload) if apply_card_payload else None
    return {
        "user_status": structured_json.get("user_status"),
        "easy_summary": structured_json.get("easy_summary"),
        "key_points": structured_json.get("key_points", []),
        "sources": structured_json.get("sources", []),
        "policies": structured_json.get("policies", []),
        "actions": structured_json.get("actions", []),
        "similar_policies": structured_json.get("similar_policies", []),
        "apply_card": apply_card,
        "disclaimer": structured_json.get("disclaimer"),
        "slot_request": structured_json.get("slot_request"),
        "profile_confirm": structured_json.get("profile_confirm"),
        "eligibility_result": structured_json.get("eligibility_result"),
        "suggested_actions": structured_json.get("suggested_actions", []),
        "policy_selection": structured_json.get("policy_selection"),
        "evidence_review": structured_json.get("evidence_review"),
        "claim_evidence_links": structured_json.get("claim_evidence_links", []),
    }


def to_message_item(
    message: ChatMessage,
    *,
    policies: list[dict],
    evidences: list[dict],
) -> ChatMessageItem:
    meta = unwrap_message_meta(message.structured_json)
    meta_policies = meta.pop("policies", [])
    if policies:
        meta_by_slug = {
            policy.get("slug"): policy
            for policy in meta_policies
            if policy.get("slug")
        }
        meta_by_id = {
            str(policy.get("policy_id")): policy
            for policy in meta_policies
            if policy.get("policy_id") is not None
        }
        resolved_policies = []
        for policy in policies:
            cached = (
                meta_by_slug.get(policy.get("slug"))
                or meta_by_id.get(str(policy.get("policy_id")))
                or {}
            )
            resolved_policies.append({
                **cached,
                **policy,
                "summary": policy.get("summary") or cached.get("summary"),
                "tag": policy.get("tag") or cached.get("tag"),
                "tagTone": policy.get("tagTone") or cached.get("tagTone"),
            })
    else:
        resolved_policies = meta_policies
    return ChatMessageItem(
        chat_message_id=str(message.chat_message_id),
        role=message.role,
        message_type=message.message_type,
        content=message.content,
        sequence_no=message.sequence_no,
        created_at=message.created_at,
        policies=[
            AssistantMessagePolicy(
                **{**p, "policy_id": str(p.get("policy_id", ""))}
            )
            for p in resolved_policies
        ],
        evidences=[AssistantMessageEvidence(**e) for e in evidences],
        **meta,
    )


async def build_message_items(
    db: AsyncSession, messages: list[ChatMessage]
) -> list[ChatMessageItem]:
    if not messages:
        return []
    message_ids = [m.chat_message_id for m in messages]
    policies_by_msg = await ChatRepository.find_policies_by_message_ids(db, message_ids)
    evidences_by_msg = await ChatRepository.find_evidences_by_message_ids(db, message_ids)
    return [
        to_message_item(
            message,
            policies=policies_by_msg.get(message.chat_message_id, []),
            evidences=evidences_by_msg.get(message.chat_message_id, []),
        )
        for message in messages
    ]
