import asyncio

from experiments.chatbot.run_execution_dataset_experiment import run_experiment


def test_execution_dataset_compares_the_same_scenarios_for_both_variants() -> None:
    result = asyncio.run(run_experiment())

    baseline = result["variants"]["baseline"]["summary"]
    treatment = result["variants"]["treatment"]["summary"]

    assert result["dataset_version"] == "v1"
    assert baseline["scenario_count"] == treatment["scenario_count"] == 24
    assert baseline["provider_calls_started"] == 112
    assert baseline["post_cancel_provider_calls"] == 94
    assert treatment["provider_calls_started"] == 18
    assert treatment["post_cancel_provider_calls"] == 0
    assert treatment["avoided_calls"] == 94
    assert all(value == 24 for value in treatment["scores"].values())
    assert result["comparison"]["started_call_reduction_percent"] == 83.929
