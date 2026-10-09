import pytest


@pytest.fixture(autouse=True)
def _disable_application_langfuse_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit/integration tests offline even when a developer opts in locally."""
    from app.services.recommendation_langfuse_telemetry import (
        recommendation_langfuse_telemetry,
    )

    monkeypatch.setattr(recommendation_langfuse_telemetry, "enabled", False)
