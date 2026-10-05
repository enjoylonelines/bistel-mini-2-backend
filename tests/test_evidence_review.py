import asyncio

from app.services.chat.handlers._evidence_review import review_evidence_completeness
from app.services.chat.handlers._claim_evidence import build_claim_evidence_links
from app.services.chat.handlers._payload_builders import build_assistant_payload
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


def test_claim_links_split_policy_response_into_sentences() -> None:
    state = {
        "branch_content": "지원 대상입니다. 신청 기간을 확인해 주세요.",
        "branch_policies": [{"slug": "WLF1"}],
        "branch_evidences": [_evidence(chunk_id=101), _evidence(chunk_id=102)],
    }
    review = review_evidence_completeness(state)

    links = build_claim_evidence_links(state, review)

    assert [link["claim_text"] for link in links] == [
        "지원 대상입니다.",
        "신청 기간을 확인해 주세요.",
    ]
    assert all(link["candidate_chunk_ids"] == ["101", "102"] for link in links)
    assert all(link["linkage_status"] == "CANDIDATE" for link in links)


def test_claim_links_are_not_emitted_for_blocked_response() -> None:
    state = {
        "branch_content": "지원 대상입니다.",
        "branch_eligibility_result": {"status": "ELIGIBLE"},
        "branch_evidences": [],
    }

    assert build_claim_evidence_links(
        state, review_evidence_completeness(state, intent="eligibility")
    ) == []


def test_payload_persists_candidate_claim_links() -> None:
    payload = asyncio.run(
        build_assistant_payload(
            {
                "supervisor_decision": {"intent": "summary", "raw": "test"},
                "branch_content": "지원 대상입니다. 신청 기간을 확인해 주세요.",
                "branch_policies": [{"slug": "WLF1"}],
                "branch_evidences": [_evidence()],
            }
        )
    )

    assert payload["assistant_payload"]["claim_evidence_links"] == [
        {
            "claim_id": "response_sentence:1",
            "claim_text": "지원 대상입니다.",
            "claim_type": "policy_response_sentence",
            "candidate_chunk_ids": ["101"],
            "linkage_status": "CANDIDATE",
        },
        {
            "claim_id": "response_sentence:2",
            "claim_text": "신청 기간을 확인해 주세요.",
            "claim_type": "policy_response_sentence",
            "candidate_chunk_ids": ["101"],
            "linkage_status": "CANDIDATE",
        },
    ]
