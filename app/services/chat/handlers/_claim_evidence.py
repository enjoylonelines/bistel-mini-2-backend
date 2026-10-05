"""Deterministic, sentence-level trace links for policy-scoped replies.

The links answer a deliberately narrow question: which retrieved chunks were
available when each displayed policy sentence was produced?  They do *not*
prove semantic entailment.  A later evaluator can use this persisted trace to
judge whether a sentence is actually supported by its candidate chunks.
"""
from __future__ import annotations

import re
from typing import Any

_SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def _policy_sentences(content: Any) -> list[str]:
    if not isinstance(content, str):
        return []
    return [
        sentence.strip()
        for sentence in _SENTENCE_BREAK.split(content.strip())
        if sentence.strip()
    ]


def _complete_chunk_ids(evidences: Any) -> list[str]:
    if not isinstance(evidences, list):
        return []
    chunk_ids: list[str] = []
    for evidence in evidences:
        if not isinstance(evidence, dict):
            continue
        chunk_id = evidence.get("chunk_id")
        if (
            chunk_id is not None
            and isinstance(evidence.get("snippet"), str)
            and evidence["snippet"].strip()
            and isinstance(evidence.get("source_url"), str)
            and evidence["source_url"].strip()
        ):
            chunk_id_text = str(chunk_id)
            if chunk_id_text not in chunk_ids:
                chunk_ids.append(chunk_id_text)
    return chunk_ids


def build_claim_evidence_links(
    state: dict[str, Any], evidence_review: dict[str, Any]
) -> list[dict[str, Any]]:
    """Create persisted candidate links for displayed policy sentences.

    Only policy-scoped output that already passed the response-level evidence
    gate receives links.  The ``linkage_status`` remains ``CANDIDATE`` until a
    claim-to-snippet evaluator establishes semantic support.
    """
    if evidence_review.get("verdict") != "PASS" or not evidence_review.get(
        "required_for"
    ):
        return []

    chunk_ids = _complete_chunk_ids(state.get("branch_evidences"))
    if not chunk_ids:
        return []

    return [
        {
            "claim_id": f"response_sentence:{index}",
            "claim_text": sentence,
            "claim_type": "policy_response_sentence",
            "candidate_chunk_ids": chunk_ids,
            "linkage_status": "CANDIDATE",
        }
        for index, sentence in enumerate(
            _policy_sentences(state.get("branch_content")), start=1
        )
    ]
