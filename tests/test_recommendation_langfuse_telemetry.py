from __future__ import annotations

from typing import Any

from app.services.recommendation_langfuse_telemetry import (
    RecommendationLangfuseTelemetry,
)


class _FakeObservation:
    def __init__(self, call: dict[str, Any]) -> None:
        self.call = call

    def __enter__(self) -> _FakeObservation:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def update(self, **kwargs: object) -> None:
        self.call["update"] = kwargs


class _FakeLangfuse:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def start_as_current_observation(self, **kwargs: object) -> _FakeObservation:
        self.calls.append(dict(kwargs))
        return _FakeObservation(self.calls[-1])


def test_completed_trace_exports_only_operational_recommendation_summary() -> None:
    client = _FakeLangfuse()
    telemetry = RecommendationLangfuseTelemetry(
        enabled=True,
        client_factory=lambda: client,
    )

    emitted = telemetry.emit_completed(
        request_id=73,
        elapsed_ms=125.25,
        summary={
            "candidate_count": 4,
            "result_count": 3,
            "evidence_count": 5,
            "evidence_search_call_count": 4,
            "evidence_search_avoided_call_count": 2,
            "evidence_estimated_embedding_input_tokens_avoided": 83,
            "evidence_lane": {
                "capacity": 2,
                "max_in_flight": 2,
                "queue_wait_p50_ms": 3.5,
                "queue_wait_p95_ms": 7.2,
            },
            "llm_provider_call_count": 1,
            "llm_model": "gpt-5.4-mini",
            "llm_provider_token_usage": {
                "input_tokens": 80,
                "output_tokens": 20,
                "total_tokens": 100,
            },
            "llm_fallback_used": True,
            "llm_error": "429 provider rate limit",
        },
    )

    assert emitted is True
    assert [call["name"] for call in client.calls] == [
        "recommendation-execution",
        "recommendation-rag-evidence",
        "recommendation-rerank",
    ]
    assert client.calls[0]["input"] == {
        "request_type": "recommendation",
        "content_captured": False,
    }
    assert client.calls[0]["metadata"]["telemetry_source"] == "application"
    assert client.calls[1]["update"]["output"]["lane_queue_wait_p95_ms"] == 7.2
    assert client.calls[2]["update"]["usage_details"] == {"input": 80, "output": 20}
    assert client.calls[2]["update"]["output"]["error_category"] == "RATE_LIMIT"
    assert "raw_query" not in str(client.calls)
    assert "profile" not in str(client.calls)


def test_disabled_telemetry_does_not_create_a_client() -> None:
    telemetry = RecommendationLangfuseTelemetry(
        enabled=False,
        client_factory=lambda: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert telemetry.emit_completed(request_id=1, summary={}, elapsed_ms=1) is False
