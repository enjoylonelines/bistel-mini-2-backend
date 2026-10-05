from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.apply_schema import ChecklistItem


class ChatSessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class ChatSessionCreateResponse(BaseModel):
    chat_session_id: str
    session_status: str


class ChatSessionTitleUpdateRequest(BaseModel):
    title: str = Field(..., max_length=255)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_non_blank_title(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        title = value.strip()
        if not title:
            raise ValueError("Title must not be blank")
        return title


class ChatSessionTitleUpdateResponse(BaseModel):
    chat_session_id: str
    title: str
    updated_at: datetime


class ChatSessionListItem(BaseModel):
    chat_session_id: str
    title: str | None
    session_status: str
    last_message_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ChatSessionListResponse(BaseModel):
    sessions: list[ChatSessionListItem]


class ChatSessionDeleteResponse(BaseModel):
    chat_session_id: str
    deleted: bool = True


class ChatSessionBulkDeleteRequest(BaseModel):
    chat_session_ids: list[int] = Field(..., min_length=1)

    @field_validator("chat_session_ids")
    @classmethod
    def _dedupe_session_ids(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(value))


class ChatSessionBulkDeleteResponse(BaseModel):
    deleted_count: int
    deleted_session_ids: list[str]


class AssistantMessagePolicy(BaseModel):
    policy_id: str
    slug: str
    policy_name: str
    summary: str | None = None
    tag: str | None = None
    tagTone: str | None = None
    action_type: str | None = None
    recommendation_request_id: str | None = None
    source_ref_id: str | None = None
    selected_conditions: dict[str, Any] | None = None
    merged_condition_json: dict[str, Any] | None = None


class AssistantMessageEvidence(BaseModel):
    chunk_id: str | None = None
    snippet: str
    source_title: str | None = None
    source_url: str | None = None
    evidence_role: str | None = None

    @field_validator("evidence_role")
    @classmethod
    def _lower_evidence_role(cls, v: str | None) -> str | None:
        return v.lower() if v else v


class AssistantMessageKeyPoint(BaseModel):
    label: str
    content: str


class AssistantMessageClaimEvidence(BaseModel):
    """A displayed policy sentence and chunks available to support review."""

    claim_id: str
    claim_text: str
    claim_type: str
    candidate_chunk_ids: list[str] = Field(default_factory=list)
    linkage_status: str


class ApplyCard(BaseModel):
    policy_id: str
    policy_name: str
    how_to_apply: str | None = None
    contact: str | None = None
    official_url: str | None = None
    checklist: list[ChecklistItem] = Field(default_factory=list)
    caution: str | None = None


class SimilarPolicyChatItem(BaseModel):
    """명시적 '유사 정책' 요청 답변에 함께 내려보내는 비슷한 정책 1건."""

    policy_id: str
    slug: str
    name: str
    category: str | None = None
    similarity_reason: str | None = None


class AssistantMessage(BaseModel):
    chat_message_id: str
    content: str
    user_status: str | None = None
    easy_summary: str | None = None
    key_points: list[AssistantMessageKeyPoint] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    policies: list[AssistantMessagePolicy] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    evidences: list[AssistantMessageEvidence] = Field(default_factory=list)
    similar_policies: list[SimilarPolicyChatItem] = Field(default_factory=list)
    apply_card: ApplyCard | None = None
    disclaimer: bool | None = None
    slot_request: dict | None = None
    profile_confirm: dict | None = None
    eligibility_result: dict | None = None
    suggested_actions: list[str] = Field(default_factory=list)
    policy_selection: dict | None = None
    evidence_review: dict[str, Any] | None = None
    claim_evidence_links: list[AssistantMessageClaimEvidence] = Field(default_factory=list)


class ChatMessageSendRequest(BaseModel):
    content: str = Field(..., min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def _strip_idempotency_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ChatMessageSendResponse(BaseModel):
    chat_session_id: str
    user_message_id: str
    assistant_message: AssistantMessage


class ChatMessageItem(BaseModel):
    chat_message_id: str
    role: str
    message_type: str
    content: str | None
    sequence_no: int
    created_at: datetime | None
    user_status: str | None = None
    easy_summary: str | None = None
    key_points: list[AssistantMessageKeyPoint] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    policies: list[AssistantMessagePolicy] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    evidences: list[AssistantMessageEvidence] = Field(default_factory=list)
    similar_policies: list[SimilarPolicyChatItem] = Field(default_factory=list)
    apply_card: ApplyCard | None = None
    disclaimer: bool | None = None
    slot_request: dict | None = None
    profile_confirm: dict | None = None
    eligibility_result: dict | None = None
    suggested_actions: list[str] = Field(default_factory=list)
    policy_selection: dict | None = None
    evidence_review: dict[str, Any] | None = None
    claim_evidence_links: list[AssistantMessageClaimEvidence] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ChatMessageListResponse(BaseModel):
    chat_session_id: str
    messages: list[ChatMessageItem]


class ChatRequestStatusResponse(BaseModel):
    request_id: str
    chat_session_id: str
    user_message_id: str
    idempotency_key: str | None = None
    status: str
    intent: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    assistant_message_id: str | None = None
    retryable: bool = False
    payload: dict[str, Any] | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None
