from types import SimpleNamespace

from experiments.chatbot.live_section_aware_eval import (
    _derive_case_result,
    _summarize,
)


def _row(rank: int, section: str):
    return SimpleNamespace(
        chunk_id=100 + rank,
        policy_id=242,
        section=section,
    )


def test_live_eval_derives_baseline_and_section_aware_challenger() -> None:
    case = {
        "case_id": "R019",
        "query": "법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?",
        "expected_policy_ids": [242],
        "expected_sections": ["신청 방법"],
    }
    results = [
        _row(1, "신청 기간"),
        _row(2, "공식 지원대상 원문"),
        _row(3, "유의 사항"),
        _row(4, "정리된 지원 조건"),
        _row(5, "조건 구조"),
        _row(6, "지원 내용"),
        _row(7, "신청 방법"),
    ]

    result = _derive_case_result(
        case=case,
        results=results,
        elapsed_ms=123.4,
    )

    assert result["baseline_hit_at_5"] is False
    assert result["challenger_hit_at_5"] is True
    assert result["target_hit_at_7"] is True
    assert result["target_first_rank"] == 7
    assert result["challenger_sections"][-1] == "신청 방법"


def test_live_eval_summary_reports_delta_and_recovered_case() -> None:
    rows = [
        {
            "case_id": "R019",
            "baseline_hit_at_5": False,
            "challenger_hit_at_5": True,
            "target_hit_at_7": True,
            "target_hit_at_10": True,
            "elapsed_ms": 100.0,
        },
        {
            "case_id": "R004",
            "baseline_hit_at_5": True,
            "challenger_hit_at_5": True,
            "target_hit_at_7": True,
            "target_hit_at_10": True,
            "elapsed_ms": 120.0,
        },
    ]

    summary = _summarize(rows)

    assert summary["baseline_section_hit_at_5_pct"] == 50.0
    assert summary["challenger_section_hit_at_5_pct"] == 100.0
    assert summary["section_hit_delta_pp"] == 50.0
    assert summary["target_recall_at_7_pct"] == 100.0
    assert summary["target_recall_at_10_pct"] == 100.0
    assert summary["recovered_case_ids"] == ["R019"]
    assert summary["still_missed_case_ids"] == []
