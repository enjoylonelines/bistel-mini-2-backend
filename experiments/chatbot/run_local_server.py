"""Run the Dodam backend against the disposable local deep-dive stack.

This helper is local-only:
- PostgreSQL/pgvector: 127.0.0.1:55432
- OpenAI-compatible embeddings: local Ollama on 127.0.0.1:11434

It must not be used for production benchmarking.
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault(
    "PSYCOPG_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault("OPENAI_API_KEY", "local-ollama")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
os.environ.setdefault("JWT_SECRET_KEY", "local-deep-dive-only")

import uvicorn
from langchain_openai import OpenAIEmbeddings

import app.services.policy_rag_service as policy_rag_module


def _local_init_embeddings(model: str, **_: object) -> OpenAIEmbeddings:
    # LangChain's OpenAI embedding helper normally tokenizes long inputs into
    # integer arrays before sending them. Ollama's OpenAI-compatible endpoint
    # accepts raw string inputs for this model, so disable that token-length
    # preprocessing in this local-only runner.
    return OpenAIEmbeddings(
        model="text-embedding-3-large",
        api_key="local-ollama",
        base_url="http://127.0.0.1:11434/v1",
        check_embedding_ctx_length=False,
    )


policy_rag_module.init_embeddings = _local_init_embeddings

from app.main import app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8001,
        reload=False,
        access_log=True,
    )
