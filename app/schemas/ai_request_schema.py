from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.ai_contract import RequestStatus


class RecommendationRequestCreate(BaseModel):
    source_type: str = "FORM"
    source_ref_id: str | None = None
    raw_query: str | None = None
    selected_conditions: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_condition_input(self) -> "RecommendationRequestCreate":
        if not self.raw_query and not self.selected_conditions:
            raise ValueError("raw_query or selected_conditions is required")
        return self


class ManualConfirmation(BaseModel):
    question: str
    answer: Literal["yes", "no", "unknown"]
    note: str | None = None
    # 추가 질문의 원본 항목(follow_up 질문의 source_point)을 그대로 돌려받는 매칭 키.
    # LLM이 질문 문장을 바꿔도 이 키로 원래 항목과 매칭한다. 미전달 시 question 텍스트로 매칭.
    source: str | None = None


class EligibilityRequestCreate(BaseModel):
    policy_id: int | str
    source_type: str = "POLICY_DETAIL"
    source_ref_id: str | None = None
    chat_session_id: int | None = None
    raw_query: str | None = None
    selected_conditions: dict[str, Any] | None = None
    user_conditions: dict[str, Any] | None = None
    manual_confirmations: list[ManualConfirmation] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_condition_input(self) -> "EligibilityRequestCreate":
        if self.selected_conditions is None and self.user_conditions is not None:
            self.selected_conditions = self.user_conditions
        if self.manual_confirmations:
            selected_conditions = dict(self.selected_conditions or {})
            existing_confirmations = selected_conditions.get("manual_confirmations")
            if not isinstance(existing_confirmations, list):
                existing_confirmations = []
            selected_conditions["manual_confirmations"] = [
                *existing_confirmations,
                *[
                    confirmation.model_dump(mode="json")
                    for confirmation in self.manual_confirmations
                ],
            ]
            self.selected_conditions = selected_conditions
        return self


class RecommendationAnswer(BaseModel):
    # 게이트 질문에 대한 사용자 답변. question_text는 raw_query 머지용 맥락.
    question_text: str | None = None
    answer: str | None = None


class RecommendationAnswerSubmit(BaseModel):
    # 빈 목록이면 "그냥 결과 보기"(건너뛰기)로 처리한다.
    answers: list[RecommendationAnswer] = Field(default_factory=list)


class AiRequestSnapshot(BaseModel):
    request_id: str
    request_type: Literal["recommendation", "eligibility"]
    status: RequestStatus
    policy_id: str | None = None
    source_type: str | None = None
    source_ref_id: str | None = None
    parsed_query_json: dict[str, Any] = Field(default_factory=dict)
    merged_condition_json: dict[str, Any] = Field(default_factory=dict)
    profile_conflict_json: list[dict[str, Any]] = Field(default_factory=list)
    result_json: dict[str, Any] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)
    input_issues: list[dict[str, Any]] = Field(default_factory=list)
    error_message: str | None = None


RecommendationPollingStatus = Literal["loading", "done", "error", "follow_up", "cancelled"]


class RecommendationEvidenceItem(BaseModel):
    chunk_id: int | str
    policy_id: int | str
    snippet: str
    display_text: str | None = None
    source_title: str
    source_url: str
    score: float | None = None
    evidence_role: str | None = None


class FollowUpQuestionItem(BaseModel):
    field_name: str
    question_text: str
    reason: str | None = None
    priority: int = 0
    options: list[dict[str, str]] = Field(default_factory=list)


class RecommendationReasons(BaseModel):
    model_config = ConfigDict(extra="allow")

    # 카드 "잘 맞는 점" 칩에 쓰는 짧은 매칭 조건 라벨(생애주기·자녀 나이 등).
    matched_labels: list[str] = Field(default_factory=list)
    # 상세 사유 목록(충족/확인 필요/미충족).
    matched: list[str] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)


class RecommendationResultItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    policy_id: str
    policy_name: str
    summary: str
    target_summary: str | None = None
    benefit_summary: str | None = None
    benefit_summary_display: str | None = None
    target_description: str | None = None
    benefit_description: str | None = None
    application_summary: str | None = None
    match_score: float | None = None
    raw_match_score: float | None = None
    # priority_score는 순위 정렬용 합성 점수(표시용 아님).
    priority_score: float | None = None
    # 표시 전용 적합도: 판정(assessment) 기반 신뢰도. 카드 "적합도 %"에 사용.
    condition_match_score: float | None = None
    confidence_score: float | None = None
    recommendation_rank: int | None = None
    priority_label: str | None = None
    # 후보 필터 단계 상태(CANDIDATE/UNCERTAIN/EXCLUDED).
    candidate_status: str | None = None
    # 최종 사용자 노출 판정. 카드 상태 배지는 user_status를 우선 사용한다.
    user_status: str | None = None
    assessment_status: str | None = None
    user_status_display: str | None = None
    assessment_status_display: str | None = None
    # 카드 본문/요약 사유.
    reason: str | None = None
    reason_summary: str | None = None
    reasons: RecommendationReasons | None = None
    # AI 판정이 뽑은 부족 정보(follow-up 질문 생성의 원천).
    missing_information: list[str] = Field(default_factory=list)
    why_recommended: str | None = None
    check_before_apply: str | None = None
    evidences: list[RecommendationEvidenceItem] = Field(default_factory=list)
    evidence: list[RecommendationEvidenceItem] = Field(default_factory=list)
    raw_evidences: list[Any] = Field(default_factory=list)
    follow_up_questions: list[FollowUpQuestionItem] = Field(default_factory=list)


class RecommendationPollingResponse(BaseModel):
    request_id: str
    status: RecommendationPollingStatus
    results: list[RecommendationResultItem] = Field(default_factory=list)
    recommendations: list[RecommendationResultItem] = Field(default_factory=list)
    follow_up_questions: list[FollowUpQuestionItem] = Field(default_factory=list)
    error_message: str | None = None


class RecommendationHistoryItem(BaseModel):
    request_id: str
    created_at: str | None = None
    # 입력 요약(폼 입력값 기반 조건 요약)과 추천 결과 요약.
    summary: str = ""
    policy_count: int = 0
    top_policy_names: list[str] = Field(default_factory=list)
    # 추가질문 게이트에서 받은 Q/A(있을 때만).
    follow_up_answers: list[RecommendationAnswer] = Field(default_factory=list)


class RecommendationHistoryResponse(BaseModel):
    items: list[RecommendationHistoryItem] = Field(default_factory=list)


class EligibilityCriteriaItem(BaseModel):
    label: str
    status: Literal["ok", "check", "no"]
    note: str


class EligibilityFollowUpQuestionItem(FollowUpQuestionItem):
    follow_up_id: str | None = None
    # 답변 제출 시 manual_confirmations[].source로 그대로 돌려보낼 원본 항목 키.
    source_point: str | None = None


class EligibilityResultResponse(BaseModel):
    request_id: str
    status: RequestStatus
    policy_id: str
    slug: str
    policy_name: str
    user_status: str | None = None
    user_status_display: str | None = None
    status_display: str | None = None
    banner_level: Literal["high", "mid", "low"] | None = None
    summary: str | None = None
    criteria: list[EligibilityCriteriaItem] = Field(default_factory=list)
    matched_conditions: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)
    conflicting_conditions: list[str] = Field(default_factory=list)
    manual_check_points: list[str] = Field(default_factory=list)
    evidences: list[RecommendationEvidenceItem] = Field(default_factory=list)
    questions: list[EligibilityFollowUpQuestionItem] = Field(default_factory=list)
    follow_up_questions: list[EligibilityFollowUpQuestionItem] = Field(default_factory=list)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
