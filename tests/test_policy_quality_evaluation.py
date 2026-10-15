from types import SimpleNamespace

from experiments.chatbot.evaluate_policy_quality_goldset import (
    policy_rank,
    summarize,
    summarize_policy_discovery,
)


def test_summarize_reports_section_metrics_and_not_top_one_cases() -> None:
    result = summarize(
        [
            {"case_id": "Q1", "target_first_rank": 1, "elapsed_ms": 10.0},
            {"case_id": "Q2", "target_first_rank": 2, "elapsed_ms": 20.0},
            {"case_id": "Q3", "target_first_rank": None, "elapsed_ms": 30.0},
        ]
    )

    assert result["section_hit_at_1_pct"] == 33.33
    assert result["section_hit_at_k_pct"] == 66.67
    assert result["mrr"] == 0.5
    assert result["miss_case_ids"] == ["Q3"]
    assert result["not_top_1_case_ids"] == ["Q2"]


def test_summarize_can_use_section_aware_rank() -> None:
    result = summarize(
        [
            {
                "case_id": "Q1",
                "target_first_rank": 2,
                "section_aware_first_rank": 1,
                "elapsed_ms": 10.0,
            }
        ],
        rank_key="section_aware_first_rank",
    )

    assert result["section_hit_at_1_pct"] == 100.0
    assert result["mrr"] == 1.0


def test_policy_rank_deduplicates_chunk_results_by_policy() -> None:
    rank, policy_ids = policy_rank(
        [
            SimpleNamespace(policy_id=242),
            SimpleNamespace(policy_id=242),
            SimpleNamespace(policy_id=255),
            SimpleNamespace(policy_id=240),
        ],
        [255],
    )

    assert rank == 2
    assert policy_ids == [242, 255, 240]


def test_summarize_policy_discovery_reports_policy_metrics() -> None:
    rows = [
        {
            "case_id": "Q1",
            "expected_policy_first_rank": 1,
            "section_aware_policy_first_rank": 1,
            "elapsed_ms": 10.0,
        },
        {
            "case_id": "Q2",
            "expected_policy_first_rank": 2,
            "section_aware_policy_first_rank": 1,
            "elapsed_ms": 20.0,
        },
        {
            "case_id": "Q3",
            "expected_policy_first_rank": None,
            "section_aware_policy_first_rank": None,
            "elapsed_ms": 30.0,
        },
    ]
    result = summarize_policy_discovery(rows)
    section_aware = summarize_policy_discovery(
        rows,
        rank_key="section_aware_policy_first_rank",
    )

    assert result["policy_hit_at_1_pct"] == 33.33
    assert result["policy_hit_at_3_pct"] == 66.67
    assert result["mrr"] == 0.5
    assert section_aware["policy_hit_at_1_pct"] == 66.67
