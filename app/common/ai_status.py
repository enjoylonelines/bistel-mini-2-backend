from enum import StrEnum


class RequestStatus(StrEnum):
    """비동기 요청 생명주기 상태 — API 응답 status 필드에 그대로 사용한다."""
    READY = "READY"                          # DB DEFAULT — row 생성 후 Graph 시작 전
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FOLLOW_UP_REQUIRED = "FOLLOW_UP_REQUIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AssessmentStatus(StrEnum):
    """정책 판단 내부 5상태 — policy_assessment.assessment_status (사용자에게 직접 노출 금지)"""
    LIKELY_MATCH = "LIKELY_MATCH"
    NEEDS_MORE_INFO = "NEEDS_MORE_INFO"
    NOT_MATCH = "NOT_MATCH"
    INSUFFICIENT_PROFILE = "INSUFFICIENT_PROFILE"
    CONFLICTING_PROFILE = "CONFLICTING_PROFILE"


class UserStatus(StrEnum):
    """사용자 노출 3상태 — API 응답 user_status 필드"""
    RECOMMENDABLE = "RECOMMENDABLE"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    DIFFICULT_TO_RECOMMEND = "DIFFICULT_TO_RECOMMEND"


_ASSESSMENT_TO_USER: dict[AssessmentStatus, UserStatus] = {
    AssessmentStatus.LIKELY_MATCH: UserStatus.RECOMMENDABLE,
    AssessmentStatus.NEEDS_MORE_INFO: UserStatus.NEEDS_CONFIRMATION,
    AssessmentStatus.INSUFFICIENT_PROFILE: UserStatus.NEEDS_CONFIRMATION,
    AssessmentStatus.CONFLICTING_PROFILE: UserStatus.NEEDS_CONFIRMATION,
    AssessmentStatus.NOT_MATCH: UserStatus.DIFFICULT_TO_RECOMMEND,
}


def map_assessment_to_user_status(status: AssessmentStatus) -> UserStatus:
    return _ASSESSMENT_TO_USER[status]
