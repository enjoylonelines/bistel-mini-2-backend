from app.services.chat.handlers._evidence_review import review_evidence_completeness
from app.services.chat.handlers._quality_validator import validate_branch_result


def _evidence(**overrides: object) -> dict:
    evidence = {
        "chunk_id": 101,
        "snippet": "지원 대상은 기준을 충족한 신청자입니다.",
        "source_url": "https://example.com/policy",
    }
    evidence.update(overrides)
    return evidence


def test_policy_result_with_complete_evidence_passes() -> None:
    review = review_evidence_completeness(
        {
            "branch_policies": [{"slug": "WLF1"}],
            "branch_evidences": [_evidence()],
        }
    )

    assert review["verdict"] == "PASS"
    assert review["required_for"] == ["policy_result"]
    assert review["complete_evidence_count"] == 1


def test_partial_evidence_requires_review_without_hiding_response() -> None:
    review = review_evidence_completeness(
        {
            "branch_apply_card": {"policy_id": "WLF1"},
            "branch_evidences": [_evidence(source_url="")],
        }
    )

    assert review["verdict"] == "REVIEW_REQUIRED"
    assert review["reasons"] == ["incomplete_evidence_provenance"]


def test_policy_result_without_evidence_is_blocked_and_stripped() -> None:
    correction = validate_branch_result(
        {
            "branch_content": "이 정책을 신청할 수 있습니다.",
            "branch_policies": [{"slug": "WLF1"}],
            "branch_evidences": [],
            "branch_apply_card": {"policy_id": "WLF1"},
        },
        "apply",
    )

    assert correction is not None
    assert correction["evidence_review"]["verdict"] == "BLOCKED"
    assert correction["branch_policies"] == []
    assert correction["branch_apply_card"] is None
    assert "근거를 확인하지 못해" in correction["branch_content"]


def test_generic_clarification_does_not_require_evidence() -> None:
    review = review_evidence_completeness(
        {
            "branch_content": "어떤 정책을 확인할까요?",
            "branch_evidences": [],
        }
    )

    assert review["verdict"] == "PASS"
    assert review["required_for"] == []


def test_assertive_eligibility_without_evidence_is_blocked() -> None:
    correction = validate_branch_result(
        {
            "branch_content": "지원 대상입니다.",
            "branch_evidences": [],
        },
        "eligibility",
    )

    assert correction is not None
    assert correction["evidence_review"]["verdict"] == "BLOCKED"
