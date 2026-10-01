from typing import Any

from pydantic import BaseModel, Field


class PolicyRagEmbeddingItem(BaseModel):
    chunk_id: int
    document_id: int
    policy_id: int
    condition_profile_id: int | None = None
    policy_code: str
    policy_name: str


class PolicyRagEmbeddingResponse(BaseModel):
    requested_count: int
    embedded_count: int
    collection_name: str
    items: list[PolicyRagEmbeddingItem]


class PolicyRagSearchResult(BaseModel):
    chunk_id: int | None = None
    document_id: int | None = None
    policy_id: int | None = None
    condition_profile_id: int | None = None
    policy_code: str | None = None
    policy_name: str | None = None
    section: str | None = None
    semantic_section: str | None = None
    section_subtype: str | None = None
    source_type: str | None = None
    source_title: str | None = None
    source_url: str | None = None
    evidence_role: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_text: str
    distance: float


class PolicyRagSearchResponse(BaseModel):
    query: str
    result_count: int
    results: list[PolicyRagSearchResult]
