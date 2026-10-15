"""Evaluate the source-backed policy-quality gold set against PGVector.

The evaluator deliberately filters each query to its reviewed expected policy.
It therefore measures section retrieval inside a known policy, not policy
discovery or an end-to-end recommendation rate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.ai.tools.policy_chunk_search_tool import (
    _order_section_aware_results,
    infer_section_hint,
)
from app.services.policy_rag_service import PolicyRagService
from experiments.chatbot.policy_quality_goldset import (
    DEFAULT_DATASET,
    goldset_readiness,
    load_goldset_candidates,
)


DEFAULT_OUTPUT = Path("output/deep-dive/policy-quality-goldset-eval.json")


def _percent(hits: int, count: int) -> float:
    return round(hits / count * 100, 2) if count else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * 0.95)))
    return round(ordered[index], 3)


def summarize(rows: list[dict[str, Any]], *, rank_key: str = "target_first_rank") -> dict[str, Any]:
    ranks = [row.get(rank_key) for row in rows]
    timings = [float(row["elapsed_ms"]) for row in rows]
    return {
        "case_count": len(rows),
        "section_hit_at_1_pct": _percent(
            sum(rank == 1 for rank in ranks), len(rows)
        ),
        "section_hit_at_k_pct": _percent(
            sum(rank is not None for rank in ranks), len(rows)
        ),
        "mrr": round(
            statistics.fmean(0.0 if rank is None else 1.0 / int(rank) for rank in ranks),
            4,
        ) if rows else 0.0,
        "query_latency": {
            "p50_ms": round(statistics.median(timings), 3) if timings else 0.0,
            "p95_ms": _p95(timings),
        },
        "miss_case_ids": [
            row["case_id"] for row in rows if row.get(rank_key) is None
        ],
        "not_top_1_case_ids": [
            row["case_id"]
            for row in rows
            if row.get(rank_key) not in (None, 1)
        ],
    }


def expected_policy_rank(
    ranked_policy_ids: list[int | None],
    expected_policy_ids: list[int | str],
) -> int | None:
    expected = {str(policy_id) for policy_id in expected_policy_ids}
    return next(
        (
            index
            for index, policy_id in enumerate(ranked_policy_ids, start=1)
            if str(policy_id) in expected
        ),
        None,
    )


def policy_rank(
    results: list[Any],
    expected_policy_ids: list[int | str],
) -> tuple[int | None, list[int | None]]:
    """Return the first distinct-policy rank from an unfiltered chunk search."""
    ranked_policy_ids: list[int | None] = []
    seen: set[str] = set()
    for result in results:
        policy_id = getattr(result, "policy_id", None)
        policy_key = str(policy_id)
        if policy_id is None or policy_key in seen:
            continue
        seen.add(policy_key)
        ranked_policy_ids.append(policy_id)
    return expected_policy_rank(ranked_policy_ids, expected_policy_ids), ranked_policy_ids


def summarize_policy_discovery(
    rows: list[dict[str, Any]],
    *,
    rank_key: str = "expected_policy_first_rank",
) -> dict[str, Any]:
    ranks = [row.get(rank_key) for row in rows]
    timings = [float(row["elapsed_ms"]) for row in rows]
    return {
        "case_count": len(rows),
        "policy_hit_at_1_pct": _percent(sum(rank == 1 for rank in ranks), len(rows)),
        "policy_hit_at_3_pct": _percent(
            sum(rank is not None and rank <= 3 for rank in ranks), len(rows)
        ),
        "mrr": round(
            statistics.fmean(0.0 if rank is None else 1.0 / int(rank) for rank in ranks),
            4,
        ) if rows else 0.0,
        "query_latency": {
            "p50_ms": round(statistics.median(timings), 3) if timings else 0.0,
            "p95_ms": _p95(timings),
        },
        "miss_case_ids": [
            row["case_id"] for row in rows if row.get(rank_key) is None
        ],
        "not_top_1_case_ids": [
            row["case_id"]
            for row in rows
            if row.get(rank_key) not in (None, 1)
        ],
    }


async def run(*, top_k: int = 3, candidate_k: int = 21) -> dict[str, Any]:
    readiness = goldset_readiness(DEFAULT_DATASET)
    if not readiness["ready_for_accuracy_reporting"]:
        raise ValueError("gold set is not approved for accuracy reporting")

    service = PolicyRagService()
    rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    cases = load_goldset_candidates(DEFAULT_DATASET)
    for case in cases:
        started = time.perf_counter()
        response = await service.search(
            query=case["query"],
            k=top_k,
            policy_ids=case["expected_policy_ids"],
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        expected_sections = set(case["expected_sections"])
        baseline_sections = [item.section for item in response.results]
        section_hint = infer_section_hint(case["query"])
        ordered_results = _order_section_aware_results(
            response.results,
            top_k=top_k,
            section_hint=section_hint,
        )
        ordered_sections = [item.section for item in ordered_results]
        baseline_first_rank = next(
            (
                rank
                for rank, section in enumerate(baseline_sections, start=1)
                if section in expected_sections
            ),
            None,
        )
        section_aware_first_rank = next(
            (
                rank
                for rank, section in enumerate(ordered_sections, start=1)
                if section in expected_sections
            ),
            None,
        )
        rows.append(
            {
                "case_id": case["case_id"],
                "quality_axis": case["quality_axis"],
                "expected_policy_ids": case["expected_policy_ids"],
                "expected_sections": case["expected_sections"],
                "section_hint": section_hint,
                "target_first_rank": baseline_first_rank,
                "section_aware_first_rank": section_aware_first_rank,
                "ranked_sections": baseline_sections,
                "section_aware_ranked_sections": ordered_sections,
                "elapsed_ms": round(elapsed_ms, 3),
            }
        )

        candidate_started = time.perf_counter()
        candidate_response = await service.search(
            query=case["query"],
            k=candidate_k,
        )
        candidate_elapsed_ms = (time.perf_counter() - candidate_started) * 1000
        expected_policy_first_rank, ranked_policy_ids = policy_rank(
            candidate_response.results,
            case["expected_policy_ids"],
        )
        candidate_rows.append(
            {
                "case_id": case["case_id"],
                "quality_axis": case["quality_axis"],
                "expected_policy_ids": case["expected_policy_ids"],
                "expected_policy_first_rank": expected_policy_first_rank,
                "ranked_policy_ids": ranked_policy_ids,
                "elapsed_ms": round(candidate_elapsed_ms, 3),
            }
        )

    axes = sorted({str(row["quality_axis"]) for row in rows})
    return {
        "evaluation": "policy_scoped_section_retrieval",
        "scope": {
            "expected_policy_filter": True,
            "isolated_source_backed_corpus": True,
            "top_k": top_k,
        },
        "candidate_policy_discovery": {
            "evaluation": "unfiltered_chunk_vector_retrieval_distinct_policy_rank",
            "expected_policy_filter": False,
            "candidate_k": candidate_k,
            "corpus_policy_count": len(
                {str(policy_id) for case in cases for policy_id in case["expected_policy_ids"]}
            ),
            "negative_or_abstention_cases_present": False,
            "overall": summarize_policy_discovery(candidate_rows),
            "rows": candidate_rows,
        },
        "baseline": {
            "overall": summarize(rows),
            "by_axis": {
                axis: summarize([row for row in rows if row["quality_axis"] == axis])
                for axis in axes
            },
        },
        "section_aware": {
            "ordering": "query-only section hint; no gold label input",
            "overall": summarize(rows, rank_key="section_aware_first_rank"),
            "by_axis": {
                axis: summarize(
                    [row for row in rows if row["quality_axis"] == axis],
                    rank_key="section_aware_first_rank",
                )
                for axis in axes
            },
        },
        "rows": rows,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--candidate-k", type=int, default=21)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.top_k < 1 or args.candidate_k < 1:
        parser.error("--top-k and --candidate-k must be positive")

    result = await run(top_k=args.top_k, candidate_k=args.candidate_k)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
