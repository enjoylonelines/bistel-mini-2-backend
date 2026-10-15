from experiments.chatbot.policy_quality_goldset import goldset_readiness, load_goldset_candidates


def test_policy_quality_v1_contains_only_source_backed_cases() -> None:
    rows = load_goldset_candidates()
    readiness = goldset_readiness()

    assert len(rows) == 21
    assert readiness["quality_axis_counts"] == {
        "application_method": 7,
        "benefit_content": 7,
        "eligibility": 7,
    }
    assert readiness["review_status_counts"] == {"approved": 21}
    assert readiness["approved_case_count"] == 21
    assert readiness["ready_for_accuracy_reporting"] is True
