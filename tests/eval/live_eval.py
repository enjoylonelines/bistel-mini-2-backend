"""
실제 LLM E2E 평가 스크립트.

15개 시나리오를 각 3회 실행해 의도 분류·프로필 추출·응답 구조·변동성을 측정한다.

사용 방법:
  python tests/eval/live_eval.py

  # 특정 시나리오만 실행
  python tests/eval/live_eval.py --ids L01 L02 L03

  # 반복 횟수 변경 (기본 3)
  python tests/eval/live_eval.py --runs 1

필수 환경변수:
  OPENAI_API_KEY  — 없으면 실행하지 않고 이 메시지만 출력한다.

주의:
  - 실제 LLM을 호출한다. 기본 45회 호출 (15 시나리오 × 3회).
  - DB/RAG를 호출하거나 변경하지 않는다. lifecycle runner와 RAG 조회는
    결정론적 fixture로 교체한다.
  - 이 평가는 LLM 라우팅·응답 구조만 측정한다. 정책 검색 품질이나 실제
    RAG grounding(근거성) 지표로 사용하면 안 된다.
  - 측정되지 않은 결과를 임의로 작성하지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

logging.basicConfig(level=logging.WARNING)

# ─── API 키 사전 확인 ─────────────────────────────────────────────────────────

def _check_api_key() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "\n[live_eval] OPENAI_API_KEY 환경변수가 없습니다.\n"
            "실행 방법:\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "  python tests/eval/live_eval.py\n"
        )
        sys.exit(0)


# ─── DB·생명주기 Runner Stub ─────────────────────────────────────────────────

class _FakeDb:
    """DB 세션 stub — 어떤 쿼리도 실행하지 않는다."""
    async def execute(self, *a: Any, **kw: Any) -> Any:
        from unittest.mock import MagicMock
        return MagicMock()

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


_LIVE_EVAL_POLICY = {
    "policy_id": "live-eval-policy-001",
    "slug": "live-eval-policy-001",
    "policy_name": "[live-eval fixture] 정책",
    "summary": None,
    "tag": None,
    "tagTone": None,
}

_LIVE_EVAL_EVIDENCE = {
    "chunk_id": 990001,
    "snippet": "[live-eval fixture] 외부 정책 데이터에 의존하지 않는 평가용 근거입니다.",
    "source_title": "[live-eval fixture] 정책",
    "source_url": "fixture://live-eval/policy-001",
    "evidence_role": "fixture",
}


def _make_stub_recommend_result() -> tuple[Any, None]:
    # AiRequestSnapshot 필드를 SimpleNamespace로 모사
    from types import SimpleNamespace
    from app.common.ai_status import RequestStatus
    snapshot = SimpleNamespace(
        status=RequestStatus.COMPLETED.value,
        request_id=0,
        questions=[],
        result_json={
            "results": [
                {
                    "policy_id": "stub-policy-001",
                    "slug": "stub-policy-001",
                    "policy_name": "[stub] 추천 정책",
                    "summary": "stub 추천 결과입니다.",
                    "evidence": [],
                }
            ]
        },
        merged_condition_json=None,
    )
    return snapshot, None


def _make_stub_eligibility_result() -> dict[str, Any]:
    return {
        "status": "INELIGIBLE",
        "user_status": "ineligible",
        "assessment_status": "COMPLETED",
        "follow_up_questions": [],
        "summary": "[stub] 자격 확인 stub 결과입니다.",
        "request_id": 0,
        "criteria": [],
    }


def _make_stub_comparison_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **state,
        "branch_content": "[stub] 비교 stub 결과입니다.",
        "branch_policies": [
            {
                "policy_id": "stub-compare-001",
                "slug": "stub-compare-001",
                "policy_name": "[stub] 비교 정책 A",
                "summary": "stub 비교 결과입니다.",
            },
        ],
        "branch_evidences": [],
    }


def _make_stub_apply_result() -> tuple[Any, None]:
    from app.schemas.apply_schema import ApplyPreparationResponse
    response = ApplyPreparationResponse(
        apply_id=None,
        saved=False,
        policy_id="stub-policy",
        how_to_apply="온라인 신청",
        contact=None,
        official_url="https://example.com",
        checklist=[],
        caution=None,
        progress_percent=0,
    )
    return response, None


def _patch_lifecycle_runners(monkeypatch_dict: dict[str, Any]) -> None:
    """생명주기 runner를 stub으로 교체해 DB·외부 API 호출을 차단한다.

    패치 전략:
    - recommend: _handler_recommend가 로컬 임포트 → _handler_recommend 모듈 직접 패치
    - eligibility: chat_handlers._sync_legacy_patch_points()가 _chat_handlers._run_eligibility_lifecycle을
                   감지해 _lifecycle_runners.run_eligibility_lifecycle를 덮어쓰므로
                   chat_handlers 모듈 수준 변수를 패치해야 _sync_legacy_patch_points가 stub을 선택함
    - compare: chat_handlers.handle_compare가 참조하는 _run_comparison_branch를 직접 패치
    - apply: _handler_apply가 로컬 임포트 → _handler_apply 모듈 직접 패치
    """
    import app.services.chat.chat_handlers as _ch
    import app.services.chat.handlers._handler_apply as _ha
    import app.services.chat.handlers._handler_recommend as _hr

    monkeypatch_dict["_hr._run_recommendation_lifecycle"] = _hr._run_recommendation_lifecycle
    monkeypatch_dict["_ch._run_eligibility_lifecycle"] = _ch._run_eligibility_lifecycle
    monkeypatch_dict["_ch._run_comparison_branch"] = _ch._run_comparison_branch
    monkeypatch_dict["_ha._run_apply_preparation"] = _ha._run_apply_preparation

    async def _stub_recommend(*a: Any, **kw: Any) -> Any:
        return _make_stub_recommend_result()

    async def _stub_eligibility(*a: Any, **kw: Any) -> Any:
        return _make_stub_eligibility_result()

    async def _stub_comparison(**kw: Any) -> Any:
        return _make_stub_comparison_state(kw.get("state") or {})

    async def _stub_apply(*a: Any, **kw: Any) -> Any:
        return _make_stub_apply_result()

    _hr._run_recommendation_lifecycle = _stub_recommend  # type: ignore[assignment]
    _ch._run_eligibility_lifecycle = _stub_eligibility   # type: ignore[assignment]
    _ch._run_comparison_branch = _stub_comparison        # type: ignore[assignment]
    _ha._run_apply_preparation = _stub_apply             # type: ignore[assignment]


def _restore_lifecycle_runners(monkeypatch_dict: dict[str, Any]) -> None:
    import app.services.chat.chat_handlers as _ch
    import app.services.chat.handlers._handler_apply as _ha
    import app.services.chat.handlers._handler_recommend as _hr

    _hr._run_recommendation_lifecycle = monkeypatch_dict["_hr._run_recommendation_lifecycle"]
    _ch._run_eligibility_lifecycle = monkeypatch_dict["_ch._run_eligibility_lifecycle"]
    _ch._run_comparison_branch = monkeypatch_dict["_ch._run_comparison_branch"]
    _ha._run_apply_preparation = monkeypatch_dict["_ha._run_apply_preparation"]


def _patch_data_dependencies(monkeypatch_dict: dict[str, Any]) -> None:
    """라이브 평가가 실제 정책 DB/RAG로 새지 않도록 고정 fixture를 주입한다."""
    import app.services.chat.ai._policy_resolver as _resolver

    monkeypatch_dict["_resolver.rag_lookup"] = _resolver.rag_lookup
    monkeypatch_dict["_resolver._find_compare_targets_by_policy_names"] = (
        _resolver._find_compare_targets_by_policy_names
    )

    async def _stub_rag_lookup(_: str) -> tuple[list[dict], list[dict]]:
        return [dict(_LIVE_EVAL_POLICY)], [dict(_LIVE_EVAL_EVIDENCE)]

    async def _stub_find_compare_targets_by_policy_names(
        _: str,
    ) -> tuple[None, None]:
        # 이름 기반 SQL 조회를 건너뛰고, 아래 RAG fixture 경로로 통일한다.
        return None, None

    _resolver.rag_lookup = _stub_rag_lookup  # type: ignore[assignment]
    _resolver._find_compare_targets_by_policy_names = (  # type: ignore[assignment]
        _stub_find_compare_targets_by_policy_names
    )


def _restore_data_dependencies(monkeypatch_dict: dict[str, Any]) -> None:
    import app.services.chat.ai._policy_resolver as _resolver

    _resolver.rag_lookup = monkeypatch_dict["_resolver.rag_lookup"]
    _resolver._find_compare_targets_by_policy_names = monkeypatch_dict[
        "_resolver._find_compare_targets_by_policy_names"
    ]


# ─── 단일 시나리오 실행 ──────────────────────────────────────────────────────

async def _run_scenario_once(scenario: dict[str, Any]) -> dict[str, Any]:
    from app.services.chat._routing import run_chat

    db = _FakeDb()
    t0 = time.perf_counter()
    try:
        result = await run_chat(
            db=db,  # type: ignore[arg-type]
            user_id=9999,
            user_content=scenario["user_message"],
            history=scenario.get("conversation_history") or [],
            slot=scenario.get("initial_slot_state") or {},
            recent_assistant_policy=None,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return {
            "error": str(exc),
            "elapsed_ms": elapsed * 1000,
            "actual_intent": None,
            "actual_secondary_intents": [],
            "actual_response_type": None,
            "extracted_profile": None,
            "content_snippet": None,
        }
    elapsed = time.perf_counter() - t0

    decision = result.get("supervisor_decision") or {}
    payload = result.get("assistant_payload") or {}

    # 응답 타입 판별
    response_type = _detect_response_type(payload)

    # 프로필 추출 결과
    profile = result.get("profile") or {}

    return {
        "error": None,
        "elapsed_ms": elapsed * 1000,
        "actual_intent": decision.get("intent"),
        "actual_secondary_intents": decision.get("secondary_intents") or [],
        "actual_confidence": decision.get("confidence"),
        "actual_response_type": response_type,
        "actual_clarification": _is_clarification_result(result, payload),
        "extracted_profile": {k: v for k, v in profile.items() if k not in ("skipped", "db_profile_confirmed")},
        "content_snippet": (payload.get("content") or "")[:120],
        "has_policies": bool(payload.get("policies")),
        "has_apply_card": bool(payload.get("apply_card")),
        "has_eligibility_result": bool(payload.get("eligibility_result")),
        "has_policy_selection": bool(payload.get("policy_selection")),
        "suggested_actions": payload.get("suggested_actions") or [],
    }


def _is_clarification_result(
    result: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    pending = result.get("pending") or {}
    return bool(
        pending.get("kind") == "clarification"
        or payload.get("policy_selection")
        or payload.get("slot_request")
        or payload.get("profile_confirm")
    )


def _detect_response_type(payload: dict[str, Any]) -> str:
    if payload.get("slot_request"):
        return "slot_request"
    if payload.get("profile_confirm"):
        return "profile_confirm"
    if payload.get("policy_selection"):
        return "policy_selection"
    if payload.get("eligibility_result"):
        return "eligibility_result"
    if payload.get("apply_card"):
        return "apply_card"
    if payload.get("policies"):
        return "policy_list"
    return "text"


# ─── 지표 계산 ───────────────────────────────────────────────────────────────

def _compute_metrics(
    scenarios: list[dict[str, Any]],
    all_results: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    total_runs = 0
    intent_correct = 0
    secondary_intent_correct = 0
    strict_e2e_correct = 0
    response_type_correct = 0
    clarification_correct = 0
    consistency_correct = 0
    profile_field_matches = 0
    profile_field_total = 0
    latencies: list[float] = []
    error_count = 0

    for sc in scenarios:
        sid = sc["scenario_id"]
        runs = all_results.get(sid) or []
        expected_intent = sc["expected_primary_intent"]
        expected_secondary = set(sc.get("expected_secondary_intents") or [])
        expected_rt = sc["expected_response_type"]
        expected_clarification = sc.get("expected_clarification", False)
        expected_profile_changes = sc.get("expected_slot_changes") or {}

        intents_this = []
        for r in runs:
            total_runs += 1
            if r.get("error"):
                error_count += 1
                continue

            lat = r.get("elapsed_ms") or 0
            latencies.append(lat)

            actual_intent = r.get("actual_intent")
            actual_secondary = set(r.get("actual_secondary_intents") or [])
            actual_rt = r.get("actual_response_type")
            actual_clarification = bool(r.get("actual_clarification", False))

            intent_match = actual_intent == expected_intent
            secondary_match = actual_secondary == expected_secondary
            response_type_match = actual_rt == expected_rt
            clarification_match = actual_clarification == expected_clarification

            if intent_match:
                intent_correct += 1
            if secondary_match:
                secondary_intent_correct += 1
            if response_type_match:
                response_type_correct += 1
            if clarification_match:
                clarification_correct += 1

            # 프로필 추출 정확도
            actual_profile = r.get("extracted_profile") or {}
            profile_match = True
            for key, expected_val in expected_profile_changes.items():
                if key.startswith("profile."):
                    field = key[len("profile."):]
                    profile_field_total += 1
                    field_match = actual_profile.get(field) == expected_val
                    if field_match:
                        profile_field_matches += 1
                    else:
                        profile_match = False

            if (
                intent_match
                and secondary_match
                and response_type_match
                and clarification_match
                and profile_match
            ):
                strict_e2e_correct += 1

            intents_this.append(actual_intent)

        # 반복 일관성: 모든 실행에서 intent가 동일한지
        if intents_this and len(set(intents_this)) == 1:
            consistency_correct += 1

    n_sc = len(scenarios)
    successful_runs = total_runs - error_count
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else 0
    p95_idx = int(len(latencies_sorted) * 0.95)
    p95 = latencies_sorted[min(p95_idx, len(latencies_sorted) - 1)] if latencies_sorted else 0

    return {
        "total_runs": total_runs,
        "error_count": error_count,
        "error_rate_pct": round(error_count / total_runs * 100, 1) if total_runs else 0,
        "intent_accuracy_pct": round(intent_correct / successful_runs * 100, 1) if successful_runs else 0,
        "secondary_intent_accuracy_pct": round(secondary_intent_correct / successful_runs * 100, 1) if successful_runs else 0,
        "response_type_accuracy_pct": round(response_type_correct / successful_runs * 100, 1) if successful_runs else 0,
        "clarification_accuracy_pct": round(clarification_correct / successful_runs * 100, 1) if successful_runs else 0,
        "profile_extraction_accuracy_pct": round(profile_field_matches / profile_field_total * 100, 1) if profile_field_total else None,
        "strict_e2e_success_rate_pct": round(strict_e2e_correct / total_runs * 100, 1) if total_runs else 0,
        "e2e_success_rate_pct": round(strict_e2e_correct / total_runs * 100, 1) if total_runs else 0,
        "handler_accuracy_pct": None,
        "handler_accuracy_reason": "실행 결과에 handler 이름이 노출되지 않아 측정하지 않음",
        "consistency_rate_pct": round(consistency_correct / n_sc * 100, 1) if n_sc else 0,
        "latency_p50_ms": round(p50, 1),
        "latency_p95_ms": round(p95, 1),
    }


# ─── 결과 출력 ───────────────────────────────────────────────────────────────

def _print_individual_results(
    scenario: dict[str, Any],
    runs: list[dict[str, Any]],
) -> None:
    sid = scenario["scenario_id"]
    expected = scenario["expected_primary_intent"]
    print(f"\n{'─'*60}")
    print(f"[{sid}] {scenario['description']}")
    print(f"  user_message: {scenario['user_message']!r}")
    print(f"  expected_intent: {expected}")
    for i, r in enumerate(runs, 1):
        if r.get("error"):
            print(f"  run{i}: ERROR — {r['error']}")
            continue
        match = "✓" if r.get("actual_intent") == expected else "✗"
        conf = r.get("actual_confidence")
        conf_str = f"{conf:.2f}" if conf is not None else "?"
        print(
            f"  run{i}: {match} intent={r['actual_intent']} "
            f"rt={r['actual_response_type']} "
            f"conf={conf_str} "
            f"elapsed={r['elapsed_ms']:.0f}ms"
        )
        if r.get("extracted_profile"):
            print(f"         profile={r['extracted_profile']}")
        if r.get("actual_intent") != expected:
            print(f"         expected={expected}  actual={r.get('actual_intent')}")


def _print_failure_analysis(
    scenarios: list[dict[str, Any]],
    all_results: dict[str, list[dict[str, Any]]],
) -> None:
    failures = []
    for sc in scenarios:
        sid = sc["scenario_id"]
        expected_intent = sc["expected_primary_intent"]
        expected_secondary = set(sc.get("expected_secondary_intents") or [])
        expected_response_type = sc["expected_response_type"]
        expected_clarification = sc.get("expected_clarification", False)
        runs = all_results.get(sid) or []
        for i, r in enumerate(runs, 1):
            actual_secondary = set(r.get("actual_secondary_intents") or [])
            mismatches = []
            if r.get("actual_intent") != expected_intent:
                mismatches.append("primary_intent")
            if actual_secondary != expected_secondary:
                mismatches.append("secondary_intents")
            if r.get("actual_response_type") != expected_response_type:
                mismatches.append("response_type")
            if bool(r.get("actual_clarification", False)) != expected_clarification:
                mismatches.append("clarification")

            if r.get("error") or mismatches:
                failures.append({
                    "scenario_id": sid,
                    "run": i,
                    "expected_intent": expected_intent,
                    "actual_intent": r.get("actual_intent"),
                    "expected_secondary_intents": sorted(expected_secondary),
                    "actual_secondary_intents": sorted(actual_secondary),
                    "expected_response_type": expected_response_type,
                    "actual_response_type": r.get("actual_response_type"),
                    "mismatches": mismatches,
                    "error": r.get("error"),
                    "content_snippet": r.get("content_snippet"),
                    "description": sc["description"],
                })
    if not failures:
        print("\n[failures] 없음 — 모든 실행에서 기대 intent와 일치")
        return

    print(f"\n[failures] 총 {len(failures)}건")
    for f in failures:
        print(
            f"  {f['scenario_id']}-run{f['run']}: "
            f"expected={f['expected_intent']} actual={f['actual_intent']} "
            f"rt={f['actual_response_type']} mismatches={f['mismatches']}"
        )
        if "secondary_intents" in f["mismatches"]:
            print(
                "    secondary: "
                f"expected={f['expected_secondary_intents']} "
                f"actual={f['actual_secondary_intents']}"
            )
        if f["error"]:
            print(f"    error: {f['error']}")
        elif f.get("content_snippet"):
            print(f"    content: {f['content_snippet']!r}")


# ─── 메인 ────────────────────────────────────────────────────────────────────

async def _main(scenario_ids: list[str] | None, runs: int) -> None:
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))
    from tests.eval.scenarios import LIVE_SCENARIOS

    scenarios = LIVE_SCENARIOS
    if scenario_ids:
        scenarios = [s for s in scenarios if s["scenario_id"] in scenario_ids]
    if not scenarios:
        print("[live_eval] 실행할 시나리오가 없습니다.")
        return

    total_calls = len(scenarios) * runs
    print(f"[live_eval] 시나리오 {len(scenarios)}개 × {runs}회 = {total_calls}회 LLM 호출")
    print("[live_eval] lifecycle runner와 RAG 조회는 fixture로 교체됩니다.")
    print("[live_eval] 이 결과는 라우팅·응답 구조 평가이며, 검색 품질 평가는 아닙니다.")
    print()

    stubs: dict[str, Any] = {}
    _patch_lifecycle_runners(stubs)
    _patch_data_dependencies(stubs)

    all_results: dict[str, list[dict[str, Any]]] = {}
    try:
        for sc in scenarios:
            sid = sc["scenario_id"]
            all_results[sid] = []
            for _ in range(runs):
                r = await _run_scenario_once(sc)
                all_results[sid].append(r)
            _print_individual_results(sc, all_results[sid])
    finally:
        _restore_data_dependencies(stubs)
        _restore_lifecycle_runners(stubs)

    # ── 지표 계산 ──
    metrics = _compute_metrics(scenarios, all_results)

    print(f"\n{'='*60}")
    print("[지표 요약]")
    print(f"  총 실행: {metrics['total_runs']}회  오류: {metrics['error_count']}회 ({metrics['error_rate_pct']}%)")
    print(f"  LLM 의도 분류 정확도:   {metrics['intent_accuracy_pct']}%")
    print(f"  2차 의도 정확도:         {metrics['secondary_intent_accuracy_pct']}%")
    print(f"  응답 타입 정확도:        {metrics['response_type_accuracy_pct']}%")
    print(f"  Strict E2E 성공률:       {metrics['strict_e2e_success_rate_pct']}%")
    if metrics["clarification_accuracy_pct"] is not None:
        print(f"  모호성 처리 정확도:      {metrics['clarification_accuracy_pct']}%")
    if metrics["profile_extraction_accuracy_pct"] is not None:
        print(f"  프로필 추출 정확도:      {metrics['profile_extraction_accuracy_pct']}%")
    print(f"  Handler 정확도:          미측정 ({metrics['handler_accuracy_reason']})")
    print(f"  반복 일관성:             {metrics['consistency_rate_pct']}%")
    print(f"  응답시간 P50:            {metrics['latency_p50_ms']}ms")
    print(f"  응답시간 P95:            {metrics['latency_p95_ms']}ms")

    _print_failure_analysis(scenarios, all_results)

    # JSON 결과 저장
    output = {
        "evaluation_scope": "isolated_live_llm_routing_and_response_structure",
        "data_dependency": "deterministic_fixture",
        "not_measured": [
            "production_policy_retrieval_quality",
            "grounded_answer_correctness",
            "real_corpus_latency",
        ],
        "metrics": metrics,
        "results": all_results,
    }
    out_path = "tests/eval/live_eval_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n[live_eval] 결과 저장: {out_path}")


def main() -> None:
    _check_api_key()

    parser = argparse.ArgumentParser(description="챗봇 LLM E2E 평가")
    parser.add_argument("--ids", nargs="*", help="실행할 시나리오 ID (기본: 전체)")
    parser.add_argument("--runs", type=int, default=3, help="시나리오당 반복 횟수 (기본: 3)")
    args = parser.parse_args()

    asyncio.run(_main(args.ids, args.runs))


if __name__ == "__main__":
    main()
