"""Privacy-bounded Langfuse telemetry for completed recommendation executions.

This module deliberately exports operational summaries only. It must not receive
or serialize raw request text, profile fields, policy names/text, evidence
snippets, recommendation prose, or provider prompts/completions.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


class RecommendationLangfuseTelemetry:
    def __init__(
        self,
        *,
        enabled: bool | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.enabled = (
            settings.langfuse_recommendation_tracing_enabled
            if enabled is None
            else enabled
        )
        self._client_factory = client_factory or self._default_client
        self._client: Any | None = None

    def emit_completed(
        self,
        *,
        request_id: int,
        summary: dict[str, Any],
        elapsed_ms: float,
        source: str = "application",
    ) -> bool:
        """Queue a completed/fallback execution trace without affecting results."""
        if not self.enabled:
            return False
        try:
            client = self._get_client()
            if client is None:
                return False
            outcome = "FALLBACK" if bool(summary.get("llm_fallback_used")) else "AUGMENTED"
            with client.start_as_current_observation(
                as_type="span",
                name="recommendation-execution",
                input={"request_type": "recommendation", "content_captured": False},
                metadata={
                    "request_id": str(request_id),
                    "telemetry_source": source,
                    "terminal_outcome": outcome,
                    "data_classification": (
                        "operational_metadata_only"
                        if source == "application"
                        else "synthetic_control_only"
                    ),
                },
            ) as root:
                with client.start_as_current_observation(
                    as_type="retriever",
                    name="recommendation-rag-evidence",
                    input={"content_captured": False},
                ) as rag_observation:
                    rag_observation.update(output=self._rag_output(summary))

                provider_call_count = int(summary.get("llm_provider_call_count") or 0)
                if provider_call_count:
                    usage_details = self._usage_details(
                        summary.get("llm_provider_token_usage")
                    )
                    with client.start_as_current_observation(
                        as_type="generation",
                        name="recommendation-rerank",
                        model=str(summary.get("llm_model") or "unknown"),
                        input={"content_captured": False},
                    ) as rerank_observation:
                        rerank_observation.update(
                            output={
                                "provider_call_count": provider_call_count,
                                "fallback_used": bool(summary.get("llm_fallback_used")),
                                "error_category": self._error_category(
                                    summary.get("llm_error")
                                ),
                            },
                            usage_details=usage_details,
                        )

                root.update(
                    output={
                        "terminal_outcome": outcome,
                        "elapsed_ms": round(elapsed_ms, 3),
                        "result_count": int(summary.get("result_count") or 0),
                        "llm_provider_call_count": provider_call_count,
                        "llm_fallback_used": bool(summary.get("llm_fallback_used")),
                        "error_category": self._error_category(summary.get("llm_error")),
                    }
                )
            return True
        except Exception:
            # Observability must not turn a durable user result into a failure.
            logger.exception("Langfuse recommendation telemetry emission failed")
            return False

    def _get_client(self) -> Any | None:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    @staticmethod
    def _default_client() -> Any | None:
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            logger.warning("Langfuse recommendation telemetry is enabled without keys")
            return None
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
            environment=settings.app_env,
        )

    @staticmethod
    def _rag_output(summary: dict[str, Any]) -> dict[str, Any]:
        lane = summary.get("evidence_lane") or {}
        return {
            "candidate_count": int(summary.get("candidate_count") or 0),
            "evidence_count": int(summary.get("evidence_count") or 0),
            "provider_call_count": int(summary.get("evidence_search_call_count") or 0),
            "avoided_call_count": int(
                summary.get("evidence_search_avoided_call_count") or 0
            ),
            "estimated_embedding_input_tokens_avoided": (
                summary.get("evidence_estimated_embedding_input_tokens_avoided")
            ),
            "lane_capacity": lane.get("capacity"),
            "lane_max_in_flight": lane.get("max_in_flight"),
            "lane_queue_wait_p50_ms": lane.get("queue_wait_p50_ms"),
            "lane_queue_wait_p95_ms": lane.get("queue_wait_p95_ms"),
            "error_present": bool(summary.get("evidence_error")),
        }

    @staticmethod
    def _usage_details(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        try:
            return {
                "input": int(value["input_tokens"]),
                "output": int(value["output_tokens"]),
            }
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _error_category(value: Any) -> str | None:
        message = str(value or "").lower()
        if not message:
            return None
        if "429" in message or "rate limit" in message:
            return "RATE_LIMIT"
        if "timeout" in message or "timed out" in message:
            return "TIMEOUT"
        if "5" in message and ("error" in message or "http" in message):
            return "PROVIDER_5XX"
        return "OTHER"


recommendation_langfuse_telemetry = RecommendationLangfuseTelemetry()
