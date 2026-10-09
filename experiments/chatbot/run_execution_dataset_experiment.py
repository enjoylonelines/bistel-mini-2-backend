"""Compare RAG cancellation-control variants on a versioned local dataset.

The dataset shape follows an LLM-observability experiment: every scenario is a
versioned dataset item, each variant emits item-level scores, and aggregate
results compare the same items. It is intentionally local and fake-provider
only. ``--langfuse`` explicitly exports the same synthetic items as two Langfuse
experiment runs. It never sends a real recommendation request, user profile,
policy text, provider token usage, or production capacity data.

Example:
  PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_dataset_experiment.py \
    --output /tmp/dodam-execution-v1.json

  PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_dataset_experiment.py \
    --langfuse --output /tmp/dodam-execution-v1-langfuse.json
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


def _local_experiment_data(dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build Langfuse local-data items without including user or policy content."""
    return [
        {
            "input": {
                "scenario_id": str(item["scenario_id"]),
                "candidate_count": int(item["candidate_count"]),
                "lane_capacity": int(item["lane_capacity"]),
                "cancel_phase": str(item["cancel_phase"]),
                "provider_outcome": str(item["provider_outcome"]),
            },
            "expected_output": {"control_contracts_pass": True},
            "metadata": {
                "dataset_version": "v1",
                "data_classification": "synthetic_control_only",
            },
        }
        for item in dataset
    ]


def _item_evaluator(*, output: dict[str, Any], **_kwargs: Any) -> list[Any]:
    """Attach deterministic control-flow contracts as Langfuse item scores."""
    from langfuse import Evaluation

    return [
        Evaluation(name=name, value=bool(value), data_type="BOOLEAN")
        for name, value in output["scores"].items()
    ]


def _run_evaluator(*, item_results: list[Any], **_kwargs: Any) -> list[Any]:
    """Expose synthetic call counts for comparison; these are not billing data."""
    from langfuse import Evaluation

    observations = [result.output["observations"] for result in item_results]
    return [
        Evaluation(
            name="provider_calls_started_total",
            value=sum(int(item["provider_calls_started"]) for item in observations),
            comment="Synthetic fake-search calls; not a provider invoice or capacity metric.",
        ),
        Evaluation(
            name="post_cancel_provider_calls_total",
            value=sum(
                int(item["post_cancel_provider_calls"]) for item in observations
            ),
            comment="Synthetic calls started after the controlled cancellation point.",
        ),
        Evaluation(
            name="avoided_calls_total",
            value=sum(int(item["avoided_calls"]) for item in observations),
            comment="Controlled fake-search calls avoided by the execution recheck.",
        ),
        Evaluation(
            name="estimated_embedding_input_tokens_avoided_total",
            value=sum(
                int(item["estimated_embedding_input_tokens_avoided"] or 0)
                for item in observations
            ),
            comment="Local token estimate only; not provider usage or cost.",
        ),
    ]


def _langfuse_result_reference(result: Any) -> dict[str, Any]:
    return {
        "experiment_id": result.experiment_id,
        "run_name": result.run_name,
        "item_count": len(result.item_results),
        "dataset_run_url": result.dataset_run_url,
    }


def run_langfuse_experiments(
    dataset_path: Path = DEFAULT_DATASET,
    *,
    max_concurrency: int = 1,
    langfuse_client: Any | None = None,
) -> dict[str, Any]:
    """Export the two existing synthetic variants as comparable Langfuse runs.

    ``max_concurrency`` controls only this short-lived SDK experiment runner. It
    neither configures nor measures application/provider concurrency.
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be positive")

    dataset = _load_dataset(dataset_path)
    if langfuse_client is None:
        from app.core.config import get_settings
        from langfuse import Langfuse

        settings = get_settings()
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            raise RuntimeError(
                "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required for --langfuse"
            )
        langfuse_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
        )

    data = _local_experiment_data(dataset)
    results: dict[str, Any] = {}
    try:
        for variant in ("baseline", "treatment"):

            async def task(*, item: dict[str, Any], _variant: Variant = variant, **_kwargs: Any) -> dict[str, Any]:
                return await _run_item(item["input"], _variant)

            result = langfuse_client.run_experiment(
                name="dodam-execution-v1",
                run_name=f"dodam-execution-v1-{variant}",
                description=(
                    "Synthetic RAG cancellation-control experiment. No user request, "
                    "policy text, provider call, billing data, or production traffic."
                ),
                data=data,
                task=task,
                evaluators=[_item_evaluator],
                run_evaluators=[_run_evaluator],
                max_concurrency=max_concurrency,
                metadata={
                    "variant": variant,
                    "dataset_version": "v1",
                    "measurement_scope": "synthetic_control_only",
                    "recommendation_capacity_scope": "not_measured",
                },
            )
            results[variant] = _langfuse_result_reference(result)
    finally:
        langfuse_client.flush()

    return {
        "experiment_name": "dodam-execution-v1",
        "dataset_version": "v1",
        "scope": (
            "Langfuse export of the local fake-search control experiment; no real "
            "provider, database, terminal write, user data, or production capacity."
        ),
        "max_concurrency": max_concurrency,
        "runs": results,
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
    parser.add_argument(
        "--langfuse",
        action="store_true",
        help="Export the synthetic baseline and treatment runs to configured Langfuse.",
    )
    parser.add_argument(
        "--langfuse-max-concurrency",
        type=int,
        default=1,
        help="SDK experiment-runner item concurrency only; not application capacity.",
    )
    args = parser.parse_args()
    payload = asyncio.run(run_experiment(args.dataset))
    if args.langfuse:
        payload["langfuse"] = run_langfuse_experiments(
            args.dataset,
            max_concurrency=args.langfuse_max_concurrency,
        )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
