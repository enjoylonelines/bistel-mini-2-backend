"""Offline section-aware retrieval challenger.

This does not replace vector retrieval. It tests whether, after policy discovery,
application queries can use section metadata to distinguish "신청 방법" from
"신청 기간" before adding a reranker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from experiments.chatbot.application_section_subtype import (
    classify_application_subtype,
    load_application_cases,
)


INVENTORY_PATH = Path(__file__).with_name("application_policy_section_inventory.json")
TARGET_SECTION = {
    "method": "신청 방법",
    "period": "신청 기간",
}


def load_inventory(path: Path = INVENTORY_PATH) -> dict[int, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(item["policy_id"]): list(item.get("chunks") or [])
        for item in payload
    }


def select_section_candidate(
    *,
    query: str,
    chunks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    subtype = classify_application_subtype(query)
    target = TARGET_SECTION.get(subtype)
    if target is None:
        return None
    return next((chunk for chunk in chunks if chunk.get("section") == target), None)


def evaluate() -> dict[str, Any]:
    cases = load_application_cases()
    inventory = load_inventory()
    rows: list[dict[str, Any]] = []
    hit_count = 0

    for case in cases:
        policy_id = int(case["expected_policy_ids"][0])
        selected = select_section_candidate(
            query=case["query"],
            chunks=inventory.get(policy_id, []),
        )
        expected_sections = set(case["expected_sections"])
        hit = bool(selected and selected.get("section") in expected_sections)
        hit_count += int(hit)
        rows.append(
            {
                "case_id": case["case_id"],
                "policy_id": policy_id,
                "predicted_subtype": classify_application_subtype(case["query"]),
                "selected_chunk_id": None if selected is None else selected.get("chunk_id"),
                "selected_section": None if selected is None else selected.get("section"),
                "expected_sections": list(case["expected_sections"]),
                "hit": hit,
            }
        )

    return {
        "case_count": len(rows),
        "hit_count": hit_count,
        "section_selection_hit_pct": round(
            (hit_count / len(rows) * 100) if rows else 0.0,
            2,
        ),
        "rows": rows,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
