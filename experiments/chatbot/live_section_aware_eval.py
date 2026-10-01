"""Fresh original-corpus section-aware evaluation.

Run this only when DATABASE_URL / PSYCOPG_DATABASE_URL / OPENAI_API_KEY point to
the original Dodam database and embedding path.

For each historical application case, make a single K=10 retrieval call against
the expected policy. Derive:
- baseline: original vector ranks 1..5
- challenger: candidate ranks 1..7, section-aware output top 5
- target recall at K=7 / K=10
- target first rank
- retrieval latency

Using a single K=10 call per case keeps baseline/challenger comparison on the
same query embedding and vector snapshot.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.ai.tools.policy_chunk_search_tool import (
    _select_section_aware_results,
    infer_application_section_subtype,
)
from app.services.policy_rag_service import PolicyRagService


CASES_PATH = Path(__file__).with_name("historical_application_scoped_baseline.json")
DEFAULT_OUTPUT = Path("output/deep-dive/live-section-aware-eval.json")


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def _latency_summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {"median_ms": 0.0, "p95_ms": 0.0}
    p95_index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * 0.95)))
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


def _derive_case_result(
    *,
    case: dict[str, Any],
    results: list[Any],
    elapsed_ms: float,
) -> dict[str, Any]:
    expected_sections = set(case["expected_sections"])
    subtype = infer_application_section_subtype(case["query"])

    baseline_top5 = list(results[:5])
    candidate_top7 = list(results[:7])
    challenger_top5 = _select_section_aware_results(
        candidate_top7,
        top_k=5,
        section_subtype=subtype,
    )

    baseline_sections = [item.section for item in baseline_top5]
    challenger_sections = [item.section for item in challenger_top5]
    top10_sections = [item.section for item in results[:10]]

    baseline_hit = any(section in expected_sections for section in baseline_sections)
    challenger_hit = any(
        section in expected_sections for section in challenger_sections
    )
    target_hit_at_7 = any(
        section in expected_sections for section in top10_sections[:7]
    )
    target_hit_at_10 = any(
        section in expected_sections for section in top10_sections[:10]
    )
    first_rank = next(
        (
            rank
            for rank, section in enumerate(top10_sections, start=1)
            if section in expected_sections
        ),
        None,
    )

    return {
        "case_id": case["case_id"],
        "query": case["query"],
        "policy_id": int(case["expected_policy_ids"][0]),
        "expected_sections": sorted(expected_sections),
        "predicted_subtype": subtype,
        "baseline_hit_at_5": baseline_hit,
        "challenger_hit_at_5": challenger_hit,
        "target_hit_at_7": target_hit_at_7,
        "target_hit_at_10": target_hit_at_10,
        "target_first_rank": first_rank,
        "baseline_sections": baseline_sections,
        "challenger_sections": challenger_sections,
        "top10_sections": top10_sections,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_hits = sum(bool(row["baseline_hit_at_5"]) for row in rows)
    challenger_hits = sum(bool(row["challenger_hit_at_5"]) for row in rows)
    hit7 = sum(bool(row["target_hit_at_7"]) for row in rows)
    hit10 = sum(bool(row["target_hit_at_10"]) for row in rows)
    timings = [float(row["elapsed_ms"]) for row in rows]

    baseline_pct = _pct(baseline_hits, len(rows))
    challenger_pct = _pct(challenger_hits, len(rows))

    return {
        "evaluation": "fresh_original_corpus_section_aware_application_subset",
        "case_count": len(rows),
        "baseline_section_hit_at_5_pct": baseline_pct,
        "challenger_section_hit_at_5_pct": challenger_pct,
        "section_hit_delta_pp": round(challenger_pct - baseline_pct, 2),
        "target_recall_at_7_pct": _pct(hit7, len(rows)),
        "target_recall_at_10_pct": _pct(hit10, len(rows)),
        "k10_query_timing": _latency_summary(timings),
        "recovered_case_ids": [
            row["case_id"]
            for row in rows
            if not row["baseline_hit_at_5"] and row["challenger_hit_at_5"]
        ],
        "still_missed_case_ids": [
            row["case_id"] for row in rows if not row["challenger_hit_at_5"]
        ],
        "rows": rows,
    }


async def run(output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    cases: list[dict[str, Any]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    service = PolicyRagService()
    rows: list[dict[str, Any]] = []

    for case in cases:
        policy_id = int(case["expected_policy_ids"][0])
        started = time.perf_counter()
        response = await service.search(
            query=case["query"],
            k=10,
            policy_ids=[policy_id],
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        rows.append(
            _derive_case_result(
                case=case,
                results=list(response.results),
                elapsed_ms=elapsed_ms,
            )
        )

    summary = _summarize(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


async def main() -> None:
    result = await run()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
