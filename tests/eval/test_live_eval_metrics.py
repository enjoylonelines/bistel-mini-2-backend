import asyncio
from typing import Any

import pytest

from tests.eval.live_eval import (
    _compute_metrics,
    _is_clarification_result,
    _patch_data_dependencies,
    _patch_lifecycle_runners,
    _restore_data_dependencies,
    _restore_lifecycle_runners,
)
from tests.eval.scenarios import SCENARIO_BY_ID


def _scenario(**overrides: Any) -> dict[str, Any]:
    scenario = {
        "scenario_id": "L99",
        "expected_primary_intent": "eligibility",
        "expected_secondary_intents": ["apply"],
        "expected_response_type": "eligibility_result",
        "expected_slot_changes": {},
        "expected_clarification": False,
    }
    scenario.update(overrides)
    return scenario


def _result(**overrides: Any) -> dict[str, Any]:
    result = {
        "error": None,
        "elapsed_ms": 10,
        "actual_intent": "eligibility",
        "actual_secondary_intents": ["apply"],
        "actual_response_type": "eligibility_result",
        "actual_clarification": False,
        "extracted_profile": {},
    }
    result.update(overrides)
    return result


def _metrics(scenario: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return _compute_metrics([scenario], {scenario["scenario_id"]: [result]})


def test_clarification_requires_explicit_orchestration_state() -> None:
    assert not _is_clarification_result({}, {"content": "어떤 정책인지 알려주세요."})
    assert _is_clarification_result(
        {"pending": {"kind": "clarification"}},
        {"content": "어떤 정책인지 알려주세요."},
    )


def test_apply_without_policy_preserves_clarification_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.chat.handlers import _handler_apply

    async def no_policy(*args: Any, **kwargs: Any) -> tuple[None, None, list, list]:
        return None, None, [], []

    async def clarification(*args: Any, **kwargs: Any) -> str:
        return "어떤 정책을 신청하고 싶으신가요?"

    monkeypatch.setattr(_handler_apply, "_resolve_single_policy_target", no_policy)
    monkeypatch.setattr(_handler_apply, "_generate_clarification_answer", clarification)

    result = asyncio.run(
        _handler_apply.handle_apply(
            {
                "user_id": 1,
                "user_content": "그 정책 신청 방법 알려줘",
                "history": [],
                "slot": {},
                "profile": {},
                "supervisor_decision": {"intent": "apply"},
            }
        )
    )

    assert result["pending"] == {"intent": "apply", "kind": "clarification"}


def test_comparison_stub_patches_and_restores_actual_call_site() -> None:
    import app.services.chat.chat_handlers as chat_handlers

    originals: dict[str, Any] = {}
    original_comparison = chat_handlers._run_comparison_branch

    try:
        _patch_lifecycle_runners(originals)
        assert chat_handlers._run_comparison_branch is not original_comparison
    finally:
        _restore_lifecycle_runners(originals)

    assert chat_handlers._run_comparison_branch is original_comparison


def test_live_eval_data_fixture_prevents_real_rag_or_policy_sql_calls() -> None:
    import app.services.chat.ai._policy_resolver as policy_resolver

    originals: dict[str, Any] = {}
    original_rag_lookup = policy_resolver.rag_lookup
    original_compare_lookup = policy_resolver._find_compare_targets_by_policy_names

    try:
        _patch_data_dependencies(originals)
        policies, evidences = asyncio.run(policy_resolver.rag_lookup("정책 찾아줘"))
        compare_targets = asyncio.run(
            policy_resolver._find_compare_targets_by_policy_names("A와 B 비교")
        )
    finally:
        _restore_data_dependencies(originals)

    assert policies[0]["slug"] == "live-eval-policy-001"
    assert evidences[0]["source_url"] == "fixture://live-eval/policy-001"
    assert compare_targets == (None, None)
    assert policy_resolver.rag_lookup is original_rag_lookup
    assert policy_resolver._find_compare_targets_by_policy_names is original_compare_lookup


def test_live_comparison_scenarios_expect_successful_policy_payload() -> None:
    assert SCENARIO_BY_ID["L05"]["expected_response_type"] == "policy_list"
    assert SCENARIO_BY_ID["L10"]["expected_response_type"] == "policy_list"


def test_strict_e2e_fails_when_secondary_intent_is_missing() -> None:
    metrics = _metrics(_scenario(), _result(actual_secondary_intents=[]))

    assert metrics["secondary_intent_accuracy_pct"] == 0.0
    assert metrics["strict_e2e_success_rate_pct"] == 0.0


def test_strict_e2e_fails_when_extra_secondary_intent_is_added() -> None:
    metrics = _metrics(
        _scenario(),
        _result(actual_secondary_intents=["apply", "compare"]),
    )

    assert metrics["secondary_intent_accuracy_pct"] == 0.0
    assert metrics["strict_e2e_success_rate_pct"] == 0.0


def test_plain_text_is_not_counted_as_clarification() -> None:
    metrics = _metrics(
        _scenario(
            expected_secondary_intents=[],
            expected_response_type="text",
            expected_clarification=True,
        ),
        _result(
            actual_secondary_intents=[],
            actual_response_type="text",
            actual_clarification=False,
        ),
    )

    assert metrics["clarification_accuracy_pct"] == 0.0
    assert metrics["strict_e2e_success_rate_pct"] == 0.0


def test_explicit_clarification_and_profile_match_pass_strict_e2e() -> None:
    metrics = _metrics(
        _scenario(
            expected_secondary_intents=[],
            expected_response_type="text",
            expected_clarification=True,
            expected_slot_changes={"profile.region": "seoul"},
        ),
        _result(
            actual_secondary_intents=[],
            actual_response_type="text",
            actual_clarification=True,
            extracted_profile={"region": "seoul"},
        ),
    )

    assert metrics["clarification_accuracy_pct"] == 100.0
    assert metrics["profile_extraction_accuracy_pct"] == 100.0
    assert metrics["strict_e2e_success_rate_pct"] == 100.0


def test_error_run_is_included_in_strict_e2e_denominator() -> None:
    scenario = _scenario()
    metrics = _compute_metrics(
        [scenario],
        {
            scenario["scenario_id"]: [
                _result(),
                _result(error="LLM timeout"),
            ]
        },
    )

    assert metrics["error_rate_pct"] == 50.0
    assert metrics["strict_e2e_success_rate_pct"] == 50.0
    assert metrics["handler_accuracy_pct"] is None
