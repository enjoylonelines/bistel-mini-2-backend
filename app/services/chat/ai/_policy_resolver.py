from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.ai.nodes.chat.chat_nodes import (
    _find_slot_policy_by_slug,
    _is_context_dependent_apply_question,
    _is_context_dependent_compare_question,
    _llm,
    _pick_apply_target,
    _pick_compare_targets,
    _recent_assistant_policy_target,
    _user_mentions_policy_name,
)
from app.ai.nodes.chat.constants import (
    EVIDENCES_MAX as _EVIDENCES_MAX,
    POLICIES_MAX as _POLICIES_MAX,
    RAG_TOP_K as _RAG_TOP_K,
    SNIPPET_LIMIT as _SNIPPET_LIMIT,
)
from app.ai.states.chat_state import ChatGraphState, ChatSlot, Intent
from app.db.session import AsyncSessionLocal
from app.services.chat.ai._graph_clients import get_rag_service

logger = logging.getLogger(__name__)

_RAW_STRUCTURED_RE = re.compile(
    r"\b(?:operator|matchingstrength|confidence):\s*\S"
    r"|\bsourcetext:\s*\S.*\breason:\s*[A-Z_]{5}",
    re.IGNORECASE | re.DOTALL,
)


def _is_raw_structured_snippet(text: str) -> bool:
    return bool(_RAW_STRUCTURED_RE.search(text))


async def rag_lookup(query: str) -> tuple[list[dict], list[dict]]:
    try:
        result = await get_rag_service().search(query=query, k=_RAG_TOP_K)
    except Exception:
        logger.exception("RAG lookup failed; returning empty context")
        return [], []

    evidences: list[dict] = []
    seen_chunks: set[int] = set()
    for chunk in result.results:
        if chunk.chunk_id is None or chunk.chunk_id in seen_chunks:
            continue
        seen_chunks.add(chunk.chunk_id)
        snippet = (chunk.chunk_text or "")[:_SNIPPET_LIMIT]
        if _is_raw_structured_snippet(snippet):
            continue
        evidences.append({
            "chunk_id": chunk.chunk_id,
            "snippet": snippet,
            "source_title": chunk.policy_name,
            "source_url": chunk.source_url,
            "evidence_role": None,
        })
        if len(evidences) >= _EVIDENCES_MAX:
            break

    policies: list[dict] = []
    seen_policies: set[str] = set()
    for chunk in result.results:
        if not chunk.policy_code or chunk.policy_code in seen_policies:
            continue
        seen_policies.add(chunk.policy_code)
        policies.append({
            "policy_id": chunk.policy_code,
            "slug": chunk.policy_code,
            "policy_name": chunk.policy_name or "",
            "summary": None,
            "tag": None,
            "tagTone": None,
        })
        if len(policies) >= _POLICIES_MAX:
            break

    return policies, evidences


def recent_slot_policies(slot: ChatSlot | None) -> list[dict[str, Any]]:
    return list((slot or {}).get("recent_policies") or [])


def _single_recent_policy_target(
    state: ChatGraphState,
) -> tuple[str | None, str | None]:
    recent = recent_slot_policies(state.get("slot"))
    if len(recent) == 1 and recent[0].get("slug"):
        return str(recent[0]["slug"]), recent[0].get("policy_name") or None
    return None, None


def _collect_recent_policy_candidates(
    slot: "ChatSlot | None",
) -> list[dict[str, Any]]:
    """슬롯에 정책이 여러 개 있어 대상이 모호할 때 선택지 목록을 반환한다."""
    recent = recent_slot_policies(slot)
    if len(recent) < 2:
        return []
    return [
        {"slug": str(p["slug"]), "policy_name": p.get("policy_name") or ""}
        for p in recent
        if p.get("slug")
    ]


async def _find_explicit_policy_name_matches(
    user_content: str,
) -> list[tuple[str, str | None]]:
    """사용자가 문장 안에 명시한 활성 정책명을 직접 찾는다.

    신청 의도는 대상 정책이 정해져야만 신청 준비 카드로 이어진다. RAG 청크가
    아직 없거나 검색에서 누락돼도, 정책명이 문장에 그대로 있으면 이 경로로
    대상을 확정한다. 둘 이상의 정책명이 매칭되면 호출자가 선택을 요구한다.
    """
    normalized_content = re.sub(r"\s+", "", user_content or "")
    if len(normalized_content) < 4:
        return []

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text(
                    """
                    SELECT policy_code AS slug, policy_name
                    FROM policy
                    WHERE is_active = TRUE
                      AND char_length(regexp_replace(policy_name, '\\s+', '', 'g')) >= 4
                      AND position(
                        regexp_replace(policy_name, '\\s+', '', 'g')
                        IN :normalized_content
                      ) > 0
                    ORDER BY char_length(policy_name) DESC, policy_code
                    LIMIT 2
                    """
                ),
                {"normalized_content": normalized_content},
            )
            return [
                (str(row["slug"]), row["policy_name"] or None)
                for row in result.mappings().all()
                if row["slug"]
            ]
    except Exception:
        logger.exception("explicit policy-name lookup failed")
        return []


def _is_contextual_single_policy_request(intent: Intent, user_content: str) -> bool:
    if intent in ("summary", "policy_summary"):
        return any(token in user_content for token in ("요약", "정리", "뭐야", "무엇"))
    if intent == "eligibility":
        return any(
            token in user_content
            for token in ("지원 가능", "지원가능", "받을 수", "대상", "자격", "조건")
        )
    if intent == "apply":
        return _is_context_dependent_apply_question(user_content)
    return False


def _mentioned_recent_policy_targets(
    state: ChatGraphState,
) -> list[tuple[str, str | None]]:
    targets: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for policy in recent_slot_policies(state.get("slot")):
        slug = policy.get("slug")
        if not slug or slug in seen:
            continue
        if _user_mentions_policy_name(state["user_content"], policy.get("policy_name")):
            seen.add(str(slug))
            targets.append((str(slug), policy.get("policy_name") or None))
    return targets


_COMPARE_QUERY_GENERIC_TOKENS = {
    "비교",
    "비교해",
    "비교해줘",
    "알려줘",
    "추천",
    "정책",
    "비슷한",
}


def _extract_compare_policy_name_parts(user_content: str) -> list[str]:
    content = re.sub(
        r"(비교해\s*줘|비교해줘|비교해|비교|차이(?:점)?|알려\s*줘|알려줘)",
        " ",
        user_content,
    )
    parts = [
        re.sub(r"(?:을|를|은|는|이|가)$", "", part.strip(" \t\r\n'\"“”‘’.,!?？"))
        for part in re.split(r"\s*(?:와|과|이랑|랑|하고|및|,|/)\s*", content)
    ]
    return [
        part
        for part in parts
        if len(re.sub(r"[^0-9A-Za-z가-힣]+", "", part)) >= 2
        and part not in _COMPARE_QUERY_GENERIC_TOKENS
    ]


def _policy_name_match_score(query_part: str, policy_name: str | None) -> int:
    if not query_part or not policy_name:
        return 0
    normalized_query = re.sub(r"[^0-9A-Za-z가-힣]+", "", query_part).lower()
    normalized_name = re.sub(r"[^0-9A-Za-z가-힣]+", "", policy_name).lower()
    if not normalized_query or not normalized_name:
        return 0
    if _user_mentions_policy_name(query_part, policy_name):
        return 1000 + min(len(normalized_query), len(normalized_name))
    if normalized_query in normalized_name:
        return 700 + len(normalized_query)

    tokens = [
        re.sub(r"[^0-9A-Za-z가-힣]+", "", token).lower()
        for token in re.findall(r"[0-9A-Za-z가-힣]+", query_part)
    ]
    tokens = [
        token
        for token in tokens
        if len(token) >= 2 and token not in _COMPARE_QUERY_GENERIC_TOKENS
    ]
    matched_tokens = [token for token in tokens if token in normalized_name]
    if not matched_tokens:
        return 0
    return sum(len(token) for token in matched_tokens)


async def _find_compare_targets_by_policy_names(
    user_content: str,
) -> tuple[tuple[str, str | None] | None, tuple[str, str | None] | None]:
    parts = _extract_compare_policy_name_parts(user_content)
    if len(parts) < 2:
        return None, None

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                """
                SELECT policy_code AS slug, policy_name
                FROM policy
                WHERE is_active = TRUE
                  AND policy_code IS NOT NULL
                  AND policy_name IS NOT NULL
                """
            )
        )
        policies = [dict(row) for row in result.mappings().all()]

    targets: list[tuple[str, str | None]] = []
    used_slugs: set[str] = set()
    for part in parts[:3]:
        scored = sorted(
            (
                (
                    _policy_name_match_score(part, str(policy.get("policy_name") or "")),
                    policy,
                )
                for policy in policies
                if str(policy.get("slug") or "") not in used_slugs
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] <= 0:
            continue
        policy = scored[0][1]
        slug = str(policy.get("slug") or "")
        if not slug:
            continue
        used_slugs.add(slug)
        targets.append((slug, str(policy.get("policy_name") or "") or None))
        if len(targets) >= 2:
            break

    if len(targets) < 2:
        return None, None
    return targets[0], targets[1]


async def resolve_single_policy_target(
    state: ChatGraphState,
    *,
    intent: Intent,
) -> tuple[str | None, str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """정책 대상을 결정한다.

    Returns:
        (slug, policy_name, evidences, candidates)
        - slug=None이고 candidates가 비어있지 않으면 → 모호한 참조, 정책 선택 질문 필요
        - slug=None이고 candidates도 비어있으면 → 정책 특정 불가, 일반 clarification 필요
    """
    decision = state.get("supervisor_decision") or {}
    resolved_slug = decision.get("resolved_policy_slug")
    if resolved_slug:
        slot_policy = _find_slot_policy_by_slug(state.get("slot"), resolved_slug)
        logger.info(
            "chat_slot_resolved",
            extra={"intent": intent, "slot_used": bool(slot_policy), "rag_skipped": True},
        )
        return str(resolved_slug), (slot_policy or {}).get("policy_name"), [], []

    mentioned_recent = _mentioned_recent_policy_targets(state)
    if len(mentioned_recent) == 1:
        logger.info(
            "chat_slot_resolved",
            extra={"intent": intent, "slot_used": True, "rag_skipped": True},
        )
        return mentioned_recent[0][0], mentioned_recent[0][1], [], []

    is_clarification_resume = (
        (state.get("slot") or {}).get("pending", {}) or {}
    ).get("kind") == "clarification"

    policies, evidences = await rag_lookup(state["user_content"])
    explicit_name_matches = await _find_explicit_policy_name_matches(
        state["user_content"]
    )
    if len(explicit_name_matches) == 1:
        slug, policy_name = explicit_name_matches[0]
        logger.info(
            "chat_explicit_policy_name_resolved",
            extra={"intent": intent, "rag_candidate_count": len(policies)},
        )
        return slug, policy_name, evidences, []

    slug, policy_name = _pick_apply_target(
        policies,
        user_content=state["user_content"],
        require_policy_name_mention=True,
    )
    if slug is None and is_clarification_resume and policies:
        slug, policy_name = _pick_apply_target(policies, require_policy_name_mention=False)
    logger.info(
        "chat_slot_resolved",
        extra={
            "intent": intent,
            "slot_used": False,
            "rag_skipped": False,
            "explicit_policy_found": slug is not None,
            "clarification_resume": is_clarification_resume,
        },
    )
    if slug is not None:
        return slug, policy_name, evidences, []

    slug, policy_name = _single_recent_policy_target(state)
    if slug is not None:
        logger.info(
            "chat_slot_resolved",
            extra={"intent": intent, "slot_used": True, "rag_skipped": False},
        )
        return slug, policy_name, [], []

    if (
        not recent_slot_policies(state.get("slot"))
        and _is_contextual_single_policy_request(intent, state["user_content"])
    ):
        slug, policy_name = _recent_assistant_policy_target(
            state.get("recent_assistant_policy")
        )
        if slug is not None:
            logger.info(
                "chat_recent_assistant_policy_resolved",
                extra={"intent": intent, "recent_policy_used": True},
            )
            return slug, policy_name, [], []

    # 슬롯에 후보가 여러 개 있고 is_context_dependent → 선택지 제공
    is_context_dependent = bool(decision.get("is_context_dependent"))
    if is_context_dependent:
        candidates = _collect_recent_policy_candidates(state.get("slot"))
        if candidates:
            logger.info(
                "chat_policy_candidates_found",
                extra={"intent": intent, "candidate_count": len(candidates)},
            )
            return None, None, evidences, candidates

    return None, None, [], []


class _ComparePolicyExtraction(BaseModel):
    policy_names: list[str] = Field(
        default_factory=list,
        description="비교 대상 정책명 2개. 사용자 입력에 명시된 이름만 추출.",
    )


async def _llm_extract_compare_policy_names(user_content: str) -> list[str]:
    try:
        llm = _llm().with_structured_output(_ComparePolicyExtraction)
        system = (
            "사용자 메시지에서 비교하려는 정책 이름을 최대 2개 추출하세요. "
            "사용자가 명시적으로 언급한 이름만 추출하고, 없으면 빈 배열을 반환하세요."
        )
        result = await llm.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=user_content),
        ])
        return result.policy_names[:2]
    except Exception:
        logger.exception("LLM compare policy name extraction failed")
        return []


async def resolve_compare_targets(
    state: ChatGraphState,
) -> tuple[
    tuple[str, str | None] | None,
    tuple[str, str | None] | None,
    list[dict[str, Any]],
]:
    mentioned_recent = _mentioned_recent_policy_targets(state)
    if len(mentioned_recent) == 2:
        return mentioned_recent[0], mentioned_recent[1], []
    if len(mentioned_recent) == 1:
        combined = [mentioned_recent[0]]
        seen = {mentioned_recent[0][0]}
        extracted_names = await _llm_extract_compare_policy_names(state["user_content"])
        other_name = next(
            (n for n in extracted_names if n != mentioned_recent[0][1]),
            None,
        )
        if other_name:
            other_policies, evidences = await rag_lookup(other_name)
            other_hit = other_policies[0] if other_policies else None
            if other_hit and other_hit.get("slug") != mentioned_recent[0][0]:
                return (
                    mentioned_recent[0],
                    (str(other_hit["slug"]), other_hit.get("policy_name") or None),
                    evidences,
                )

        policies, evidences = await rag_lookup(state["user_content"])
        explicit_targets = [
            target
            for target in _pick_compare_targets(
                policies,
                slot=None,
                user_content=state["user_content"],
            )
            if target is not None
        ]
        for target in explicit_targets:
            if target[0] not in seen:
                combined.append(target)
                seen.add(target[0])
        if len(combined) == 2:
            return combined[0], combined[1], evidences

        direct_first, direct_second = await _find_compare_targets_by_policy_names(
            state["user_content"]
        )
        for target in (direct_first, direct_second):
            if target is None or target[0] in seen:
                continue
            combined.append(target)
            seen.add(target[0])
            if len(combined) == 2:
                return combined[0], combined[1], evidences
        return None, None, []

    recent = recent_slot_policies(state.get("slot"))
    if len(recent) == 2 and _is_context_dependent_compare_question(state["user_content"]):
        first = (str(recent[0]["slug"]), recent[0].get("policy_name") or None)
        second = (str(recent[1]["slug"]), recent[1].get("policy_name") or None)
        return first, second, []

    extracted_names = await _llm_extract_compare_policy_names(state["user_content"])
    if len(extracted_names) >= 2:
        (first_policies, first_evidences), (second_policies, second_evidences) = (
            await asyncio.gather(
                rag_lookup(extracted_names[0]),
                rag_lookup(extracted_names[1]),
            )
        )
        first_hit = first_policies[0] if first_policies else None
        second_hit = second_policies[0] if second_policies else None
        if (
            first_hit
            and second_hit
            and first_hit.get("slug") != second_hit.get("slug")
        ):
            return (
                (str(first_hit["slug"]), first_hit.get("policy_name") or None),
                (str(second_hit["slug"]), second_hit.get("policy_name") or None),
                first_evidences + second_evidences,
            )

    policies, evidences = await rag_lookup(state["user_content"])
    first, second = _pick_compare_targets(
        policies,
        slot=None,
        user_content=state["user_content"],
    )
    if first is not None and second is not None:
        return first, second, evidences

    direct_first, direct_second = await _find_compare_targets_by_policy_names(
        state["user_content"]
    )
    if direct_first is not None and direct_second is not None:
        return direct_first, direct_second, evidences

    return None, None, []
