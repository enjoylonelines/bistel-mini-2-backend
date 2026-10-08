from typing import Any, NotRequired, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession


class RecommendationGraphState(TypedDict):
    db: AsyncSession
    request_id: int
    execution_token: NotRequired[str | None]
    merged_condition_json: dict[str, Any]
    raw_query: NotRequired[str | None]
    selected_conditions: NotRequired[dict[str, Any] | None]
    follow_up_answers: NotRequired[list[dict[str, Any]]]
    follow_up_denials: NotRequired[list[dict[str, Any]]]
    input_issues: NotRequired[list[dict[str, Any]]]
    profile_conflict_json: NotRequired[list[dict[str, Any]]]
    candidate_rows: NotRequired[list[dict[str, Any]]]
    query_terms: NotRequired[list[str]]
    candidates: NotRequired[list[Any]]
    assessments: NotRequired[list[Any]]
    base_result_json: NotRequired[dict[str, Any]]
    llm_rerank_result: NotRequired[Any]
    llm_fallback_used: NotRequired[bool]
    llm_error: NotRequired[str | None]
    result_json: NotRequired[dict[str, Any]]
