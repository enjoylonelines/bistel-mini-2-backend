from typing import Any, Literal, NotRequired, TypedDict


Intent = Literal[
    "recommend",
    "eligibility",
    "compare",
    "apply",
    "summary",
    "policy_summary",
    "unclear",
]


class HistoryMessage(TypedDict):
    role: str
    content: str | None


class SlotPolicy(TypedDict):
    policy_id: int
    slug: str
    policy_name: str
    last_action: str
    eligibility_request_id: NotRequired[int | None]
    follow_up_questions: NotRequired[list[dict] | None]
    eligibility_status: NotRequired[str | None]


class ProfileSlot(TypedDict, total=False):
    """세션 전역 사용자 조건. 모든 intent가 공유한다."""

    stage: str          # 생애단계: 임신/출산/영유아/초등
    child_age: str      # 자녀 나이
    income: str         # 소득 구간
    region: str         # 거주 지역
    special: list[str]  # 특이사항: 한부모/다자녀/맞벌이 등
    skipped: list[str]  # 사용자가 "건너뛰기"한 슬롯 (다시 묻지 않음)


class PendingState(TypedDict, total=False):
    """채우는 중인 intent와 아직 못 받은 슬롯. 다음 턴에 이어받기 위함."""

    intent: Intent
    awaiting: list[str]
    asked: list[str]
    kind: str  # "slot"(조건 칩 폼) | "confirm"(회원 프로필 확인) | "clarification"(정책 명확화)


class ChatSlot(TypedDict, total=False):
    recent_policies: list[SlotPolicy]
    profile: ProfileSlot
    pending: PendingState | None
    updated_at: str
    last_intent: Intent | None       # 마지막으로 완료된 의도
    last_result_type: str | None     # 마지막 응답 유형: "policy_list" | "eligibility_result" | "apply_card" | ...
    suggested_actions: list[str]     # Handler가 제안한 후속 액션 목록


class RecentAssistantPolicy(TypedDict):
    policy_id: int
    slug: str
    policy_name: str
    action_type: str


class SupervisorDecision(TypedDict):
    intent: Intent
    raw: str
    resolved_policy_slug: NotRequired[str | None]
    secondary_intents: NotRequired[list[Intent]]     # 복합 의도 시 보조 의도 목록
    is_context_dependent: NotRequired[bool]          # 지시어("이 정책", "그거") 사용 여부
    confidence: NotRequired[float]                   # 의도 분류 확신도 0.0~1.0
    ambiguity_reason: NotRequired[str | None]        # 확신도 < 0.7일 때 모호성 이유


class ChatGraphState(TypedDict):
    user_id: int
    user_content: str
    history: list[HistoryMessage]
    slot: NotRequired[ChatSlot]
    recent_assistant_policy: NotRequired[RecentAssistantPolicy | None]
    supervisor_decision: NotRequired[SupervisorDecision]
    # 슬롯 필링 / 이어받기
    profile: NotRequired[ProfileSlot]
    pending: NotRequired[PendingState | None]
    pending_intent: NotRequired[Intent | None]
    awaiting_slots: NotRequired[list[str]]
    slot_request: NotRequired[dict[str, Any] | None]
    profile_confirm: NotRequired[dict[str, Any] | None]
    branch_content: NotRequired[str]
    branch_user_status: NotRequired[str | None]
    branch_easy_summary: NotRequired[str | None]
    branch_key_points: NotRequired[list[dict[str, Any]]]
    branch_policies: NotRequired[list[dict[str, Any]]]
    branch_evidences: NotRequired[list[dict[str, Any]]]
    branch_apply_card: NotRequired[dict[str, Any] | None]
    branch_suggested_actions: NotRequired[list[str]]         # secondary_intents → 후속 액션
    branch_policy_candidates: NotRequired[list[dict[str, Any]]]  # 정책 선택지 (모호한 참조 시)
    evidence_review: NotRequired[dict[str, Any]]
    claim_evidence_links: NotRequired[list[dict[str, Any]]]
    assistant_payload: NotRequired[dict[str, Any]]
    evidences_to_save: NotRequired[list[dict[str, Any]]]
    policy_links_to_save: NotRequired[list[dict[str, Any]]]
    eligibility_slot_update: NotRequired[dict[str, Any] | None]
