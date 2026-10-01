"""Candidate-selection evaluation helpers for the Dodam deep dive.

This module deliberately separates policy discovery from policy-scoped evidence
retrieval. It scores only the ranked policy candidate list produced before the
evidence search stage.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class CandidateCase:
    case_id: str
    expected_policy_ids: tuple[int, ...]


def score_candidate_case(
    case: CandidateCase,
    ranked_policy_ids: Iterable[int],
    *,
    top_k: int,
) -> dict[str, Any]:
    ranked = [int(policy_id) for policy_id in ranked_policy_ids][:top_k]
    expected = set(case.expected_policy_ids)

    first_rank = next(
        (rank for rank, policy_id in enumerate(ranked, start=1) if policy_id in expected),
        None,
    )
    return {
        "case_id": case.case_id,
        "expected_policy_ids": list(case.expected_policy_ids),
        "retrieved_policy_ids": ranked,
        "hit": first_rank is not None,
        "rank": first_rank,
        "reciprocal_rank": 0.0 if first_rank is None else 1.0 / first_rank,
    }


def aggregate_candidate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    if count == 0:
        return {
            "case_count": 0,
            "recall_at_k_pct": 0.0,
            "mrr": 0.0,
            "miss_count": 0,
            "miss_case_ids": [],
        }

    hit_count = sum(bool(row["hit"]) for row in rows)
    reciprocal_ranks = [float(row["reciprocal_rank"]) for row in rows]
    return {
        "case_count": count,
        "recall_at_k_pct": round(hit_count / count * 100, 2),
        "mrr": round(statistics.fmean(reciprocal_ranks), 4),
        "miss_count": count - hit_count,
        "miss_case_ids": [row["case_id"] for row in rows if not row["hit"]],
    }


def load_expected_cases(path: Path) -> list[CandidateCase]:
    cases: list[CandidateCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        try:
            expected = tuple(int(item) for item in payload["expected_policy_ids"])
            cases.append(CandidateCase(case_id=str(payload["case_id"]), expected_policy_ids=expected))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid candidate case at line {line_number}") from exc
    return cases


def evaluate_ranked_candidates(
    cases: list[CandidateCase],
    candidate_rows: dict[str, list[int]],
    *,
    top_k: int,
) -> dict[str, Any]:
    rows = [
        score_candidate_case(
            case,
            candidate_rows.get(case.case_id, []),
            top_k=top_k,
        )
        for case in cases
    ]
    return {
        "top_k": top_k,
        "metrics": aggregate_candidate_metrics(rows),
        "cases": rows,
    }
