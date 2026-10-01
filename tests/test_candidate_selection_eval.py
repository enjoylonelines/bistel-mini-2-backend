from experiments.chatbot.candidate_selection_eval import (
    CandidateCase,
    aggregate_candidate_metrics,
    evaluate_ranked_candidates,
    score_candidate_case,
)


def test_score_candidate_case_uses_first_expected_policy_rank() -> None:
    case = CandidateCase(case_id="C1", expected_policy_ids=(20, 30))

    row = score_candidate_case(case, [10, 30, 20], top_k=3)

    assert row["hit"] is True
    assert row["rank"] == 2
    assert row["reciprocal_rank"] == 0.5


def test_score_candidate_case_respects_top_k() -> None:
    case = CandidateCase(case_id="C2", expected_policy_ids=(30,))

    row = score_candidate_case(case, [10, 20, 30], top_k=2)

    assert row["hit"] is False
    assert row["rank"] is None
    assert row["reciprocal_rank"] == 0.0


def test_candidate_metrics_separate_recall_from_rank_quality() -> None:
    rows = [
        score_candidate_case(CandidateCase("C1", (10,)), [10, 20], top_k=2),
        score_candidate_case(CandidateCase("C2", (20,)), [10, 20], top_k=2),
        score_candidate_case(CandidateCase("C3", (30,)), [10, 20], top_k=2),
    ]

    metrics = aggregate_candidate_metrics(rows)

    assert metrics["recall_at_k_pct"] == 66.67
    assert metrics["mrr"] == 0.5
    assert metrics["miss_count"] == 1
    assert metrics["miss_case_ids"] == ["C3"]


def test_evaluate_ranked_candidates_scores_missing_case_as_miss() -> None:
    cases = [
        CandidateCase("C1", (1,)),
        CandidateCase("C2", (2,)),
    ]

    result = evaluate_ranked_candidates(
        cases,
        {"C1": [1, 3]},
        top_k=2,
    )

    assert result["metrics"]["recall_at_k_pct"] == 50.0
    assert result["cases"][1]["retrieved_policy_ids"] == []
    assert result["cases"][1]["hit"] is False
