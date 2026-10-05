from __future__ import annotations

import logging
from typing import Any

from app.ai.nodes.chat.chat_nodes import _detect_assertive_phrases
from app.ai.nodes.chat.constants import (
    INTENT_TO_ACTION_TYPE as _INTENT_TO_ACTION_TYPE,
    INTENT_TO_API_ACTION as _INTENT_TO_API_ACTION,
)
from app.ai.states.chat_state import ChatGraphState, Intent
from app.services.chat.handlers._claim_evidence import build_claim_evidence_links
from app.services.chat.handlers._evidence_review import review_evidence_completeness

logger = logging.getLogger(__name__)


async def build_assistant_payload(
    state: ChatGraphState,
) -> dict[str, Any]:
    decision = state.get("supervisor_decision") or {"intent": "unclear", "raw": "missing"}
    intent: Intent = decision["intent"]
    slot_request = state.get("slot_request")
    profile_confirm = state.get("profile_confirm")
    is_prompt = bool(slot_request or profile_confirm)
    api_action = None if is_prompt else _INTENT_TO_API_ACTION.get(intent)
    content = state.get("branch_content") or ""
    disclaimer = False
    evidence_review = state.get("evidence_review") or review_evidence_completeness(state)
    claim_evidence_links = build_claim_evidence_links(state, evidence_review)
    if (
        intent != "unclear"
        and not is_prompt
        and state.get("branch_eligibility_result") is None
    ):
        assertive = _detect_assertive_phrases(content)
        if assertive:
            logger.warning(
                "chat answer contains assertive phrases despite safety prompt",
                extra={"intent": intent, "phrases": assertive},
            )
            disclaimer = True
    if evidence_review and evidence_review.get("verdict") != "PASS":
        disclaimer = True
    # suggested_actions: secondary_intents에서 파생된 후속 행동 목록
    suggested_actions = list(state.get("branch_suggested_actions") or [])

    # policy_selection: 모호한 정책 참조 시 사용자가 선택할 수 있는 후보 목록
    policy_candidates = state.get("branch_policy_candidates") or []
    policy_selection: dict | None = (
        {
            "intent": intent,
            "candidates": policy_candidates,
        }
        if policy_candidates
        else None
    )

    payload = {
        "content": content,
        "user_status": state.get("branch_user_status"),
        "sources": [],
        "policies": state.get("branch_policies", []),
        "evidences": state.get("branch_evidences", []),
        "actions": [api_action] if api_action else [],
        "apply_card": state.get("branch_apply_card"),
        "easy_summary": state.get("branch_easy_summary"),
        "key_points": state.get("branch_key_points", []),
        "disclaimer": disclaimer,
        "slot_request": slot_request,
        "profile_confirm": profile_confirm,
        "eligibility_result": state.get("branch_eligibility_result"),
        "suggested_actions": suggested_actions,
        "policy_selection": policy_selection,
        "evidence_review": evidence_review,
        "claim_evidence_links": claim_evidence_links,
    }
    return {"assistant_payload": payload}


async def extract_evidences(
    state: ChatGraphState,
) -> list[dict[str, Any]]:
    evidences = state.get("branch_evidences", [])
    return [
        {
            "chunk_id": e["chunk_id"],
            "snippet": e.get("snippet"),
            "evidence_role": e.get("evidence_role"),
        }
        for e in evidences
        if e.get("chunk_id") is not None
    ]


async def extract_policy_links(
    state: ChatGraphState,
) -> list[dict[str, Any]]:
    decision = state.get("supervisor_decision") or {"intent": "unclear", "raw": ""}
    intent: Intent = decision["intent"]
    action_type = _INTENT_TO_ACTION_TYPE.get(intent)
    if action_type is None:
        return []
    return [
        {"policy_slug": p["slug"], "action_type": action_type}
        for p in state.get("branch_policies", [])
        if p.get("slug")
    ]
