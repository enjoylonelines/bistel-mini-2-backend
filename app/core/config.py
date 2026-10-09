from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str
    app_env: str
    database_url: str
    psycopg_database_url: str
    data_go_kr_service_key: str | None = None
    openai_api_key: str | None = None
    # Optional observability project credentials. They are used only by an
    # explicitly requested experiment export, never by normal request handling.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str | None = None
    # Application request traces require a separate opt-in from offline
    # experiment exports because they may contain operational metadata.
    langfuse_recommendation_tracing_enabled: bool = False
    jwt_secret_key: str
    jwt_algorithm: str
    access_token_expire_minutes: int
    # None leaves the process-local rerank lane disabled. A value is an
    # explicitly chosen per-process capacity, never a global limit.
    recommendation_rerank_max_in_flight: int | None = None
    # None leaves candidate-level RAG evidence fan-out unbounded. A configured
    # value is an explicitly chosen per-process capacity, never a global limit.
    recommendation_evidence_max_in_flight: int | None = None

    model_config = SettingsConfigDict(
        env_file=(".env.example", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
