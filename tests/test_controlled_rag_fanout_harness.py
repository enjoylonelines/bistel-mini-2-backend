import asyncio

from experiments.chatbot.controlled_rag_fanout_harness import (
    _measure_cancellation_comparison,
)


def test_repeated_cancellation_comparison_preserves_baseline_treatment_contract() -> None:
    result = asyncio.run(
        _measure_cancellation_comparison(candidate_count=4, trials=3)
    )

    assert result["baseline_without_post_admission_recheck"] == {
        "trials": 3,
        "provider_calls_started": 12,
        "post_cancel_provider_calls": 9,
        "avoided_calls": 0,
        "estimated_embedding_input_tokens_avoided": 0,
    }
    assert result["treatment_with_durable_recheck_callback"]["provider_calls_started"] == 3
    assert result["treatment_with_durable_recheck_callback"]["post_cancel_provider_calls"] == 0
    assert result["treatment_with_durable_recheck_callback"]["avoided_calls"] == 9
    assert result["total_started_call_reduction_percent"] == 75.0
    assert result["post_cancel_call_reduction_percent"] == 100.0
