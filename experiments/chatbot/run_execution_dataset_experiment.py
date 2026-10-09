"""Compare RAG cancellation-control variants on a versioned local dataset.

The dataset shape follows an LLM-observability experiment: every scenario is a
versioned dataset item, each variant emits item-level scores, and aggregate
results compare the same items. It is intentionally local and fake-provider
only; it neither sends traces to Langfuse nor claims provider cost or capacity.

Example:
  PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_dataset_experiment.py \
    --output /tmp/dodam-execution-v1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from app.services.recommendation_candidate_service import PolicyCandidate
from app.services.recommendation_evidence_lane import ProcessLocalEvidenceLane
from app.services.recommendation_service import RecommendationService


BASE = Path(__file__).resolve().parent
DEFAULT_DATASET = BASE / "datasets" / "dodam_execution_v1.jsonl"
Variant = Literal["baseline", "treatment"]


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("dataset must contain at least one scenario")
    identifiers = [str(row["scenario_id"]) for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("dataset scenario_id values must be unique")
    for row in rows:
        if int(row["candidate_count"]) < int(row["lane_capacity"]):
            raise ValueError("candidate_count must be at least lane_capacity")
        if row["cancel_phase"] not in {
            "before_provider_start",
            "after_initial_admission",
        }:
            raise ValueError("unsupported cancel_phase")
        if row["provider_outcome"] not in {"success", "provider_error"}:
            raise ValueError("unsupported provider_outcome")
    return rows


def _candidates(count: int) -> list[PolicyCandidate]:
    return [
        PolicyCandidate(
            policy=SimpleNamespace(
                policy_id=1000 + index,
                policy_name=f"실험 정책 {index}",
                policy_code=f"EXPERIMENT-{index}",
                benefit_type=None,
            ),
            detail=SimpleNamespace(target_description="실험 대상"),
            retrieval_score=0.8,
            candidate_status="CANDIDATE",
            filter_match_json={},
            matched_rules=[],
        )
        for index in range(count)
    ]


async def _run_item(item: dict[str, Any], variant: Variant) -> dict[str, Any]:
    candidate_count = int(item["candidate_count"])
    lane_capacity = int(item["lane_capacity"])
    cancel_phase = str(item["cancel_phase"])
    provider_outcome = str(item["provider_outcome"])
    active = cancel_phase != "before_provider_start"
    cancelled = cancel_phase == "before_provider_start"
    provider_calls = 0
    post_cancel_calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    started_count = 0

    async def fake_chunk_searcher(**_kwargs: Any) -> list[Any]:
        nonlocal provider_calls, post_cancel_calls, started_count
        provider_calls += 1
        if cancelled:
            post_cancel_calls += 1
        started_count += 1
        if started_count >= lane_capacity:
            started.set()
        await release.wait()
        if provider_outcome == "provider_error":
            raise RuntimeError("controlled dataset provider error")
        return []

    async def execution_is_active() -> bool:
        return active

    service = RecommendationService(
        chunk_searcher=fake_chunk_searcher,
        evidence_timeout_seconds=5,
        evidence_lane=ProcessLocalEvidenceLane(lane_capacity),
    )
    task = asyncio.create_task(
        service._search_evidences(
            condition={"stage": "dataset", "needs": ["cancellation"]},
            candidates=_candidates(candidate_count),
            execution_is_active=execution_is_active if variant == "treatment" else None,
        )
    )
    if cancel_phase == "before_provider_start":
        release.set()
    else:
        await started.wait()
        active = False
        cancelled = True
        release.set()

    evidences, error, debug = await task
    expected_started = (
        0
        if variant == "treatment" and cancel_phase == "before_provider_start"
        else lane_capacity
        if variant == "treatment"
        else candidate_count
    )
    expected_post_cancel = (
        0 if variant == "treatment" else candidate_count - lane_capacity
        if cancel_phase == "after_initial_admission"
        else candidate_count
    )
    expected_avoided = candidate_count - expected_started
    score = {
        "started_call_contract": int(provider_calls == expected_started),
        "post_cancel_call_contract": int(post_cancel_calls == expected_post_cancel),
        "avoided_call_contract": int(
            int(debug["avoided_call_count"]) == expected_avoided
        ),
        "no_post_cancel_provider_call": int(post_cancel_calls == 0),
        "provider_error_classified": int(
            (
                provider_outcome == "provider_error" and expected_started > 0
            )
            == bool(error)
        ),
    }
    if evidences:
        raise AssertionError("fake searcher must not return evidence")
    return {
        "scenario_id": item["scenario_id"],
        "variant": variant,
        "metadata": {
            "candidate_count": candidate_count,
            "lane_capacity": lane_capacity,
            "cancel_phase": cancel_phase,
            "provider_outcome": provider_outcome,
        },
        "observations": {
            "provider_calls_started": provider_calls,
            "post_cancel_provider_calls": post_cancel_calls,
            "avoided_calls": int(debug["avoided_call_count"]),
            "estimated_embedding_input_tokens_avoided": debug[
                "estimated_embedding_input_tokens_avoided"
            ],
            "error": error,
        },
        "scores": score,
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    score_names = next(iter(rows), {}).get("scores", {}).keys()
    return {
        "scenario_count": len(rows),
        "provider_calls_started": sum(
            int(row["observations"]["provider_calls_started"]) for row in rows
        ),
        "post_cancel_provider_calls": sum(
            int(row["observations"]["post_cancel_provider_calls"]) for row in rows
        ),
        "avoided_calls": sum(int(row["observations"]["avoided_calls"]) for row in rows),
        "estimated_embedding_input_tokens_avoided": sum(
            int(row["observations"]["estimated_embedding_input_tokens_avoided"] or 0)
            for row in rows
        ),
        "scores": {
            name: sum(int(row["scores"][name]) for row in rows)
            for name in score_names
        },
    }


async def run_experiment(dataset_path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    dataset = _load_dataset(dataset_path)
    baseline = [await _run_item(item, "baseline") for item in dataset]
    treatment = [await _run_item(item, "treatment") for item in dataset]
    baseline_summary = _aggregate(baseline)
    treatment_summary = _aggregate(treatment)
    return {
        "experiment_name": "dodam-execution-v1",
        "dataset_path": str(dataset_path),
        "dataset_version": "v1",
        "scope": (
            "local versioned dataset; in-process fake search; no Langfuse ingestion, "
            "provider, database, terminal write, or production traffic"
        ),
        "variants": {
            "baseline": {
                "description": "evidence lane without post-admission execution recheck",
                "items": baseline,
                "summary": baseline_summary,
            },
            "treatment": {
                "description": "current evidence lane with execution recheck callback",
                "items": treatment,
                "summary": treatment_summary,
            },
        },
        "comparison": {
            "started_call_reduction_percent": round(
                (1 - treatment_summary["provider_calls_started"] / baseline_summary["provider_calls_started"])
                * 100,
                3,
            ),
            "post_cancel_calls_baseline_to_treatment": [
                baseline_summary["post_cancel_provider_calls"],
                treatment_summary["post_cancel_provider_calls"],
            ],
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = asyncio.run(run_experiment(args.dataset))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
