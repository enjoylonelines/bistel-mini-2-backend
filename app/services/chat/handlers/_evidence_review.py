"""Deterministic evidence-completeness gate for policy-scoped chat output.

This gate does not judge whether every sentence is true.  It enforces the
smaller contract that a policy-specific result is accompanied by retrievable
evidence with a chunk, snippet, and source URL.
"""
from __future__ import annotations

from typing import Any, Literal

EvidenceReviewVerdict = Literal["PASS", "REVIEW_REQUIRED", "BLOCKED"]

_ASSERTIVE_ELIGIBILITY_PHRASES = (
    "받을 수 있습니다",
    "받을 수 있어요",
    "받으실 수 있습니다",
    "신청 가능합니다",
    "신청하실 수 있습니다",
    "지원받을 수 있습니다",
    "지원받으실 수 있습니다",
    "대상입니다",
    "해당됩니다",
)


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _requires_evidence(
    state: dict[str, Any], *, intent: str | None = None
) -> list[str]:
    requirements: list[str] = []
    if state.get("branch_policies"):
        requirements.append("policy_result")
    if state.get("branch_eligibility_result") is not None:
        requirements.append("eligibility_result")
    if state.get("branch_apply_card") is not None:
        requirements.append("apply_card")
    if state.get("branch_easy_summary") or state.get("branch_key_points"):
        requirements.append("policy_summary")

    content = str(state.get("branch_content") or "")
    resolved_intent = intent or (state.get("supervisor_decision") or {}).get("intent")
    if resolved_intent == "eligibility" and any(
        phrase in content for phrase in _ASSERTIVE_ELIGIBILITY_PHRASES
    ):
        requirements.append("assertive_eligibility_content")
    return requirements


def _is_complete_evidence(evidence: dict[str, Any]) -> bool:
    return (
        evidence.get("chunk_id") is not None
        and _has_text(evidence.get("snippet"))
        and _has_text(evidence.get("source_url"))
    )


def review_evidence_completeness(
    state: dict[str, Any], *, intent: str | None = None
) -> dict[str, Any]:
    """Return a persisted, response-level evidence review verdict.

    `PASS` means either no policy-specific evidence is required, or at least one
    complete evidence item is available. `REVIEW_REQUIRED` retains the response
    but marks partial provenance. `BLOCKED` means a policy-specific response has
    no evidence and must not be presented as supported.
    """
    required_for = _requires_evidence(state, intent=intent)
    evidences = [
        evidence
        for evidence in (state.get("branch_evidences") or [])
        if isinstance(evidence, dict)
    ]
    complete_count = sum(_is_complete_evidence(evidence) for evidence in evidences)

    if not required_for:
        verdict: EvidenceReviewVerdict = "PASS"
        reasons: list[str] = []
    elif complete_count:
        verdict = "PASS"
        reasons = []
    elif evidences:
        verdict = "REVIEW_REQUIRED"
        reasons = ["incomplete_evidence_provenance"]
    else:
        verdict = "BLOCKED"
        reasons = ["missing_policy_evidence"]

    return {
        "verdict": verdict,
        "required_for": required_for,
        "evidence_count": len(evidences),
        "complete_evidence_count": complete_count,
        "reasons": reasons,
    }
