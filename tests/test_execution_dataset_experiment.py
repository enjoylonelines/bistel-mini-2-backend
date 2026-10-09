import asyncio
from types import SimpleNamespace

from experiments.chatbot.run_execution_dataset_experiment import (
    run_experiment,
    run_langfuse_experiments,
)


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


def test_langfuse_export_uses_same_local_dataset_for_both_variants() -> None:
    class FakeLangfuse:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.flushed = False

        def run_experiment(self, **kwargs: object) -> SimpleNamespace:
            self.calls.append(kwargs)
            return SimpleNamespace(
                experiment_id=f"experiment-{len(self.calls)}",
                run_name=kwargs["run_name"],
                item_results=[object()] * len(kwargs["data"]),
                dataset_run_url=None,
            )

        def flush(self) -> None:
            self.flushed = True

    client = FakeLangfuse()

    result = run_langfuse_experiments(langfuse_client=client)

    assert [call["metadata"]["variant"] for call in client.calls] == [
        "baseline",
        "treatment",
    ]
    assert all(len(call["data"]) == 24 for call in client.calls)
    assert all(call["max_concurrency"] == 1 for call in client.calls)
    assert result["runs"]["treatment"]["item_count"] == 24
    assert client.flushed is True
