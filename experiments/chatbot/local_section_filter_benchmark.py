"""Repeated local timing for the section-aware fallback mechanism.

Synthetic/local timing only. Do not use as production latency evidence.
"""

from __future__ import annotations

import asyncio
import json
import statistics

from experiments.chatbot.local_section_filter_probe import run_probe


QUERY = "법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?"


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * 0.95)))
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


async def main() -> None:
    # warm up local embedding/vector path
    await run_probe(QUERY, top_k=5)
    await run_probe(QUERY, top_k=4)

    baseline_latencies: list[float] = []
    larger_pool_latencies: list[float] = []
    forced_baseline_latencies: list[float] = []
    fallback_latencies: list[float] = []
    fallback_total_latencies: list[float] = []

    for _ in range(10):
        normal = await run_probe(QUERY, top_k=5)
        larger_pool = await run_probe(QUERY, top_k=7)
        forced = await run_probe(QUERY, top_k=4)
        baseline_latencies.append(normal.baseline_latency_ms)
        larger_pool_latencies.append(larger_pool.baseline_latency_ms)
        forced_baseline_latencies.append(forced.baseline_latency_ms)
        fallback_latencies.append(forced.fallback_latency_ms)
        fallback_total_latencies.append(
            forced.baseline_latency_ms + forced.fallback_latency_ms
        )

    print(
        json.dumps(
            {
                "runs": 10,
                "k5_baseline": _summary(baseline_latencies),
                "k7_single_query_candidate_pool": _summary(larger_pool_latencies),
                "k4_baseline_before_fallback": _summary(forced_baseline_latencies),
                "section_filtered_second_query": _summary(fallback_latencies),
                "two_query_total": _summary(fallback_total_latencies),
                "interpretation": "local synthetic mechanism timing only",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
