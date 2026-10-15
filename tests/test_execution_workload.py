import asyncio

from experiments.chatbot.run_execution_workload import run_workload


def test_single_axis_cancellation_prevents_calls_before_admission() -> None:
    result = asyncio.run(
        run_workload(
            profile="cancellation",
            concurrency=1,
            lane_capacity=1,
            provider_delay_ms=5,
        )
    )

    before_admission = next(
        row
        for row in result["requests"]
        if row["scenario_id"] == "cancel-before-admission"
    )
    assert before_admission["provider_calls_started"] == 0
    assert before_admission["avoided_calls"] == 4
    assert result["summary"]["post_cancel_provider_calls"] == 0


def test_mixed_workload_reports_all_execution_measurements() -> None:
    result = asyncio.run(
        run_workload(
            profile="mixed",
            repeat=2,
            concurrency=2,
            lane_capacity=1,
            provider_delay_ms=2,
        )
    )

    assert result["workload_version"] == "v1"
    assert result["summary"]["request_count"] == 4
    assert result["summary"]["by_axis"] == {"mixed": 4}
    assert result["summary"]["elapsed_ms"]["p95"] >= 0
    assert result["summary"]["queue_wait_p95_ms"] >= 0
    assert result["summary"]["provider_calls_started"] >= 1
