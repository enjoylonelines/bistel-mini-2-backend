import asyncio
import statistics
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from app.ai.tools.policy_chunk_search_tool import search_policy_chunks
from app.common.policy_types import LIFE_STAGE_TO_DB
from app.schemas.ai_contract import EvidenceChunk
from app.services.recommendation_assessment_service import (
    RecommendationPolicyAssessment,
)
from app.services.recommendation_candidate_service import (
    CANDIDATE_STATUS_EXCLUDED,
    CANDIDATE_STATUS_UNCERTAIN,
    PolicyCandidate,
)
from app.services.recommendation_result_normalizer import (
    normalize_card_evidences,
    normalize_card_text,
)
from app.services.policy_display_service import (
    PolicyDisplayAgent,
    assessment_status_display,
    user_status_display,
)
from app.core.config import settings
from app.services.recommendation_evidence_lane import (
    ProcessLocalEvidenceLane,
    get_process_local_evidence_lane,
)


PolicyChunkSearcher = Callable[..., Awaitable[list[EvidenceChunk]]]
RecommendationExecutionCheck = Callable[[], Awaitable[bool]]


class RecommendationService:
    def __init__(
        self,
        chunk_searcher: PolicyChunkSearcher = search_policy_chunks,
        # 최종 노출 추천 수. 모델이 제한적이라 응답시간을 줄이려 4로 둔다.
        # (풀/리랭크 출력도 이 값에 연동돼 함께 줄어든다.)
        result_limit: int = 4,
        evidence_timeout_seconds: float = 20,
        evidence_lane: ProcessLocalEvidenceLane | None = None,
    ) -> None:
        self.chunk_searcher = chunk_searcher
        self.result_limit = result_limit
        self.evidence_timeout_seconds = evidence_timeout_seconds
        self.evidence_lane = evidence_lane or (
            get_process_local_evidence_lane(settings.recommendation_evidence_max_in_flight)
            if settings.recommendation_evidence_max_in_flight is not None
            else None
        )

    async def build_result(
        self,
        merged_condition_json: dict[str, Any],
        candidates: list[PolicyCandidate],
        selected_candidates: list[PolicyCandidate] | None = None,
        assessments: list[RecommendationPolicyAssessment] | None = None,
        execution_is_active: RecommendationExecutionCheck | None = None,
    ) -> dict[str, Any]:
        assessments = assessments or []
        assessment_by_policy = self._assessment_by_policy(assessments)
        if selected_candidates is None:
            selected_candidates = self._select_candidates(
                candidates=candidates,
                assessment_by_policy=assessment_by_policy,
            )
        evidences, evidence_error, evidence_debug = await self._search_evidences(
            condition=merged_condition_json,
            candidates=selected_candidates,
            execution_is_active=execution_is_active,
        )
        evidence_by_policy = self._group_evidences(evidences)

        results = [
            self._to_result_item(
                candidate,
                evidence_by_policy.get(str(candidate.policy.policy_id), []),
                assessment_by_policy.get(str(candidate.policy.policy_id)),
            )
            for candidate in selected_candidates
        ]
        results.sort(key=self._result_item_sort_key)
        return {
            "results": results,
            "recommendations": results,
            "summary": {
                "candidate_count": len(selected_candidates),
                "result_count": len(results),
                "evidence_count": len(evidences),
                "evidence_error": evidence_error,
                "evidence_count_by_policy": evidence_debug.get("count_by_policy", {}),
                "evidence_query_by_policy": evidence_debug.get("query_by_policy", {}),
                "evidence_search_call_count": evidence_debug.get("search_call_count", 0),
                "evidence_search_avoided_call_count": evidence_debug.get(
                    "avoided_call_count", 0
                ),
                "evidence_estimated_embedding_input_tokens_avoided": (
                    evidence_debug.get("estimated_embedding_input_tokens_avoided")
                ),
                "evidence_token_estimate_model": evidence_debug.get(
                    "token_estimate_model"
                ),
                "evidence_lane": evidence_debug.get("lane"),
                "stored_candidate_count": len(candidates),
                "excluded_candidate_count": self._candidate_count(
                    candidates,
                    CANDIDATE_STATUS_EXCLUDED,
                ),
                "uncertain_candidate_count": self._candidate_count(
                    candidates,
                    CANDIDATE_STATUS_UNCERTAIN,
                ),
                "assessment_count": len(assessments),
                "recommendable_count": self._assessment_count(
                    assessments,
                    "RECOMMENDABLE",
                ),
                "needs_confirmation_count": self._assessment_count(
                    assessments,
                    "NEEDS_CONFIRMATION",
                ),
                "difficult_to_recommend_count": self._assessment_count(
                    assessments,
                    "DIFFICULT_TO_RECOMMEND",
                ),
            },
        }

    async def _search_evidences(
        self,
        condition: dict[str, Any],
        candidates: list[PolicyCandidate],
        execution_is_active: RecommendationExecutionCheck | None = None,
    ) -> tuple[list[EvidenceChunk], str | None, dict[str, Any]]:
        # 정책마다 정책명/지원대상/혜택 + 사용자 조건을 반영한 전용 쿼리를 만들어
        # 공통 일반 쿼리보다 정책별 근거 품질을 높인다.
        query_by_policy = {
            str(candidate.policy.policy_id): self._policy_evidence_query(
                condition, candidate
            )
            for candidate in candidates
        }
        debug: dict[str, Any] = {
            "query_by_policy": query_by_policy,
            "count_by_policy": {},
            "search_call_count": 0,
            "avoided_call_count": 0,
            "estimated_embedding_input_tokens_avoided": None,
            "token_estimate_model": None,
            "lane": None,
        }
        if not candidates:
            return [], None, debug

        async def search_one(policy_id: int, query: str):
            async def invoke() -> list[EvidenceChunk]:
                return await asyncio.wait_for(
                    self.chunk_searcher(
                        query=query,
                        policy_ids=[policy_id],
                        top_k=3,
                        evidence_role="recommendation_reason",
                    ),
                    timeout=max(min(self.evidence_timeout_seconds / 2, 10), 5),
                )

            async def can_start_search() -> bool:
                return execution_is_active is None or await execution_is_active()

            async def run_or_skip(admission: object | None = None):
                if not await can_start_search():
                    return [], admission, self._skipped_search_usage(query), False, None
                try:
                    return await invoke(), admission, None, True, None
                except Exception as exc:
                    # The provider call has started even though its result did
                    # not arrive. Preserve that distinction from a pre-call
                    # cancellation for call-waste telemetry.
                    return [], admission, None, True, exc

            if self.evidence_lane is None:
                return await run_or_skip()
            async with self.evidence_lane.admit() as admission:
                return await run_or_skip(admission)

        tasks = [
            search_one(int(candidate.policy.policy_id), query_by_policy[
                str(candidate.policy.policy_id)
            ])
            for candidate in candidates
        ]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.evidence_timeout_seconds,
            )
        except Exception as exc:
            return [], str(exc), debug

        evidences: list[EvidenceChunk] = []
        errors: list[str] = []
        admissions = []
        skipped_searches: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, BaseException):
                errors.append(str(result))
                continue
            chunks, admission, skipped_search, provider_call_started, search_error = result
            if provider_call_started:
                debug["search_call_count"] += 1
            if admission is not None:
                admissions.append(admission)
            if skipped_search is not None:
                skipped_searches.append(skipped_search)
            if search_error is not None:
                errors.append(str(search_error))
                continue
            evidences.extend(chunks)

        evidences = self._deduplicate_evidences(evidences)
        debug["count_by_policy"] = {
            policy_id: len(chunks)
            for policy_id, chunks in self._group_evidences(evidences).items()
        }
        if admissions:
            waits = [admission.queue_wait_ms for admission in admissions]
            debug["lane"] = {
                "capacity": self.evidence_lane.capacity,
                "max_in_flight": max(admission.max_in_flight for admission in admissions),
                "queue_wait_p50_ms": round(statistics.median(waits), 3),
                "queue_wait_p95_ms": round(max(waits), 3),
            }
        if skipped_searches:
            token_estimates = [
                skipped["estimated_input_tokens"]
                for skipped in skipped_searches
                if skipped.get("estimated_input_tokens") is not None
            ]
            debug["avoided_call_count"] = len(skipped_searches)
            debug["estimated_embedding_input_tokens_avoided"] = (
                sum(token_estimates) if token_estimates else None
            )
            debug["token_estimate_model"] = "text-embedding-3-large"
        return evidences, "; ".join(errors) or None, debug

    @staticmethod
    def _skipped_search_usage(query: str) -> dict[str, int | None]:
        """Estimate only the embedding input of a call that never started.

        This is a tokenizer estimate for a prevented call, not provider-reported
        usage or a monetary saving.
        """
        try:
            import tiktoken

            encoding = tiktoken.encoding_for_model("text-embedding-3-large")
            return {"estimated_input_tokens": len(encoding.encode(query))}
        except Exception:
            return {"estimated_input_tokens": None}

    def _to_result_item(
        self,
        candidate: PolicyCandidate,
        evidences: list[EvidenceChunk],
        assessment: RecommendationPolicyAssessment | None = None,
    ) -> dict[str, Any]:
        raw_evidence_items = [
            evidence.model_dump(mode="json") for evidence in evidences
        ]
        card_evidence_items = normalize_card_evidences(raw_evidence_items)
        match_score = min(
            round(candidate.match_score + (0.05 if raw_evidence_items else 0), 4),
            1.0,
        )
        reason = normalize_card_text(
            (
                assessment.reason_summary
                if assessment is not None
                else self._reason(candidate.matched_rules, bool(raw_evidence_items))
            ),
            limit=220,
            max_sentences=2,
        )
        display_row = {
            "target_description": (
                candidate.detail.target_description if candidate.detail else None
            ),
            "benefit_description": (
                candidate.detail.benefit_description if candidate.detail else None
            ),
            "application_method": (
                candidate.detail.application_method if candidate.detail else None
            ),
            "application_status": None,
            "application_period_text": (
                candidate.detail.application_period_text if candidate.detail else None
            ),
            "contact": None,
            "official_url": None,
            "caution": candidate.detail.caution if candidate.detail else None,
        }
        target_summary = PolicyDisplayAgent.summarize_target(display_row)
        benefit_summary_display = PolicyDisplayAgent.summarize_benefit(display_row)
        application_guide = PolicyDisplayAgent.build_application_guide(display_row)
        item = {
            "policy_id": str(candidate.policy.policy_id),
            "policy_code": candidate.policy.policy_code,
            "slug": candidate.policy.policy_code,
            "policy_name": candidate.policy.policy_name,
            "summary": self._summary(candidate.detail, candidate.policy),
            "benefit_summary": benefit_summary_display
            or self._short_text(
                candidate.detail.benefit_description if candidate.detail else None
            ),
            "benefit_summary_display": benefit_summary_display,
            "target_summary": target_summary,
            "target_description": self._short_text(
                candidate.detail.target_description if candidate.detail else None
            ),
            "benefit_description": self._short_text(
                candidate.detail.benefit_description if candidate.detail else None
            ),
            "application_method": self._short_text(
                candidate.detail.application_method if candidate.detail else None
            ),
            "application_summary": application_guide.summary,
            "match_score": match_score,
            "retrieval_score": candidate.retrieval_score,
            # 표시용 적합도(판정 기반 신뢰도). assessment가 있으면 아래에서 정직한 값으로
            # 덮어쓰고, 없을 때도 카드에 빈 값이 나가지 않도록 상태 기반 기본값을 둔다.
            # condition_match_score는 "표시 전용 적합도" 별칭(priority_score=정렬 분리).
            "confidence_score": self._fallback_confidence(candidate),
            "condition_match_score": self._fallback_confidence(candidate),
            "candidate_status": candidate.candidate_status,
            "filter_match_json": candidate.filter_match_json,
            "reason": reason,
            "reason_summary": reason,
            "reasons": candidate.filter_match_json.get(
                "reasons", {"matched": [], "uncertain": [], "excluded": []}
            ),
            "evidence": card_evidence_items,
            "evidences": card_evidence_items,
            "raw_evidences": raw_evidence_items,
        }
        if assessment is not None:
            item.update(
                {
                    "user_status": assessment.user_status.value,
                    "assessment_status": assessment.assessment_status.value,
                    "user_status_display": user_status_display(
                        assessment.user_status.value
                    ),
                    "assessment_status_display": assessment_status_display(
                        assessment.assessment_status.value
                    ),
                    "confidence_score": assessment.confidence_score,
                    "condition_match_score": assessment.confidence_score,
                    "missing_information": assessment.missing_information,
                    "matched_conditions": assessment.matched_conditions_json,
                    "missing_conditions": assessment.missing_conditions_json,
                    "conflicting_conditions": (
                        assessment.conflicting_conditions_json
                    ),
                    "manual_check_points": assessment.manual_check_points_json,
                }
            )
        return item

    def _fallback_confidence(self, candidate: PolicyCandidate) -> float:
        # assessment가 없을 때 카드에 표시할 신뢰도 기본값(상태 기반).
        # assessment._confidence_score의 구간과 대략 맞춰 톤을 일관되게 한다.
        retrieval = candidate.retrieval_score or 0.0
        if candidate.candidate_status == CANDIDATE_STATUS_EXCLUDED:
            return round(min(retrieval, 0.3), 2)
        if candidate.candidate_status == CANDIDATE_STATUS_UNCERTAIN:
            return round(min(max(retrieval, 0.45), 0.6), 2)
        return round(min(max(retrieval, 0.55), 0.7), 2)

    def _policy_evidence_query(
        self,
        condition: dict[str, Any],
        candidate: PolicyCandidate,
    ) -> str:
        policy = candidate.policy
        detail = candidate.detail
        stage = self._first(condition, "stage", "life_stage", "target_stage")
        stage_label = LIFE_STAGE_TO_DB.get(str(stage)) if stage else ""
        income = self._first(condition, "income", "income_level", "income_bracket")
        income_hint = "소득 중위소득" if income and income != "unknown" else ""

        parts = [
            policy.policy_name or "",
            self._short_text(
                detail.target_description if detail else None, limit=60
            ),
            *self._string_list(condition.get("needs")),
            stage_label,
            *self._string_list(condition.get("special")),
            income_hint,
            "지원대상 선정기준 신청조건",
        ]
        return " ".join(self._deduplicate([part for part in parts if part]))

    def _reason(self, matched_rules: list[str], has_evidence: bool) -> str:
        rules = self._deduplicate(matched_rules)
        if has_evidence:
            rules.append("정책 문서 근거가 확인됨")
        if not rules:
            return "현재 입력 조건과 비교 가능한 정책입니다."
        return ", ".join(rules[:4])

    def _summary(
        self,
        detail: Any,
        policy: Any,
    ) -> str:
        if detail is not None:
            summary = self._short_text(
                detail.easy_summary
                or detail.benefit_description
                or detail.target_description
            )
            if summary:
                return summary
        return policy.benefit_type or policy.policy_name

    def _short_text(self, value: str | None, limit: int = 180) -> str:
        if not value:
            return ""
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[:limit].rstrip()}..."

    def _group_evidences(
        self,
        evidences: list[EvidenceChunk],
    ) -> dict[str, list[EvidenceChunk]]:
        grouped: dict[str, list[EvidenceChunk]] = defaultdict(list)
        for evidence in evidences:
            grouped[str(evidence.policy_id)].append(evidence)
        return grouped

    def _deduplicate_evidences(
        self,
        evidences: list[EvidenceChunk],
    ) -> list[EvidenceChunk]:
        deduplicated: list[EvidenceChunk] = []
        seen: set[tuple[str, str, str | None]] = set()
        for evidence in evidences:
            key = (
                str(evidence.policy_id),
                str(evidence.chunk_id),
                evidence.evidence_role,
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(evidence)
        return deduplicated

    def _first(self, condition: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = condition.get(key)
            if value not in (None, "", []):
                return value
        return None

    def _string_list(self, value: Any) -> list[str]:
        if value in (None, "", []):
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value if item not in (None, "")]
        return [str(value)]

    def _deduplicate(self, values: list[str]) -> list[str]:
        deduplicated: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduplicated.append(normalized)
        return deduplicated

    def _candidate_count(
        self,
        candidates: list[PolicyCandidate],
        candidate_status: str,
    ) -> int:
        return sum(
            1
            for candidate in candidates
            if candidate.candidate_status == candidate_status
        )

    def _assessment_by_policy(
        self,
        assessments: list[RecommendationPolicyAssessment],
    ) -> dict[str, RecommendationPolicyAssessment]:
        return {
            str(assessment.policy_id): assessment
            for assessment in assessments
        }

    def _select_candidates(
        self,
        candidates: list[PolicyCandidate],
        assessment_by_policy: dict[str, RecommendationPolicyAssessment],
    ) -> list[PolicyCandidate]:
        if not assessment_by_policy:
            return [
                candidate
                for candidate in candidates
                if candidate.candidate_status != CANDIDATE_STATUS_EXCLUDED
            ][: self.result_limit]

        selected = [
            candidate
            for candidate in candidates
            if (
                assessment_by_policy.get(str(candidate.policy.policy_id))
                and assessment_by_policy[str(candidate.policy.policy_id)].selected_for_result
            )
        ]
        selected.sort(
            key=lambda candidate: self._result_sort_key(
                candidate,
                assessment_by_policy[str(candidate.policy.policy_id)],
            )
        )
        return selected[: self.result_limit]

    def _result_sort_key(
        self,
        candidate: PolicyCandidate,
        assessment: RecommendationPolicyAssessment,
    ) -> tuple[int, float]:
        priority_by_user_status = {
            "RECOMMENDABLE": 0,
            "NEEDS_CONFIRMATION": 1,
            "DIFFICULT_TO_RECOMMEND": 2,
        }
        return (
            priority_by_user_status[assessment.user_status.value],
            -candidate.retrieval_score,
        )

    def _result_item_sort_key(self, item: dict[str, Any]) -> tuple[int, float]:
        priority_by_user_status = {
            "RECOMMENDABLE": 0,
            "NEEDS_CONFIRMATION": 1,
            "DIFFICULT_TO_RECOMMEND": 2,
        }
        return (
            priority_by_user_status.get(str(item.get("user_status") or ""), 1),
            -float(item.get("retrieval_score") or item.get("match_score") or 0),
        )

    def _assessment_count(
        self,
        assessments: list[RecommendationPolicyAssessment],
        user_status: str,
    ) -> int:
        return sum(
            1
            for assessment in assessments
            if assessment.user_status.value == user_status
        )
