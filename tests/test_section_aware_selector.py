from experiments.chatbot.application_section_subtype import (
    classify_application_subtype,
)
from experiments.chatbot.section_aware_selector import (
    evaluate,
    load_inventory,
    select_section_candidate,
)


def test_application_subtype_distinguishes_method_from_period() -> None:
    assert classify_application_subtype("어디에서 신청하나요?") == "method"
    assert classify_application_subtype("모집 기간 없이 신청할 수 있나요?") == "period"
    assert classify_application_subtype("쉰 기간의 임금은 어디에 신청하나요?") == "method"


def test_section_aware_selector_picks_application_method_for_r019_policy() -> None:
    inventory = load_inventory()

    selected = select_section_candidate(
        query="법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?",
        chunks=inventory[242],
    )

    assert selected is not None
    assert selected["chunk_id"] == 24290000004
    assert selected["section"] == "신청 방법"


def test_section_aware_offline_fixture_hits_all_application_cases() -> None:
    result = evaluate()

    assert result["case_count"] == 10
    assert result["hit_count"] == 10
    assert result["section_selection_hit_pct"] == 100.0
