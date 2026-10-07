import asyncio
import copy
import json
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any

from app.common.ai_status import AssessmentStatus
from app.core.config import settings
from app.schemas.recommendation_rerank_schema import (
    LlmRecommendationEvidenceItem,
    LlmRecommendationItem,
    LlmRecommendationRerankResult,
)
from app.services.recommendation_assessment_service import (
    RecommendationPolicyAssessment,
)
from app.services.recommendation_candidate_service import (
    CANDIDATE_STATUS_EXCLUDED,
    PolicyCandidate,
)
from app.services.recommendation_result_normalizer import (
    _strip_chunk_meta,
    normalize_card_text,
    normalize_recommendation_result_json,
)


# LLM rerank 입력 토큰을 제한해 타임아웃을 방지하기 위한 상한값.
LLM_EVIDENCE_MAX_ITEMS = 2
LLM_EVIDENCE_SNIPPET_LIMIT = 280


@dataclass
class RecommendationRerankOutput:
    result_json: dict[str, Any]
    rerank_scores: dict[int, float]
    fallback_used: bool
    error: str | None = None


class RecommendationRerankService:
    def __init__(
        self,
        model: str = "gpt-5.4-mini",
        timeout_seconds: float = 180,
        llm_invoker: Callable[
            [list[tuple[str, str]]],
            Awaitable[LlmRecommendationRerankResult | dict[str, Any]],
        ]
        | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        # Test/experiment seam. Production leaves this unset and uses the
        # configured provider below; it is not a provider selection setting.
        self._llm_invoker = llm_invoker
        self.logger = logging.getLogger(
            f"{__name__}.RecommendationRerankService"
        )

    def select_candidate_pool(
        self,
        candidates: list[PolicyCandidate],
        assessments: list[RecommendationPolicyAssessment],
        result_limit: int,
        follow_up_denials: list[dict[str, Any]] | None = None,
    ) -> list[PolicyCandidate]:
        assessment_by_policy = self._assessment_by_policy(assessments)
        # 최종 노출 수의 2배를 풀로 둬 고를 여지를 확보하되(예: 4 → 8),
        # 너무 크면 리랭크 LLM 입력 토큰/지연이 커지므로 상한(12)을 둔다.
        max_candidates = min(result_limit * 2, 12)
        eligible = [
            candidate
            for candidate in candidates
            if self._is_eligible_candidate(candidate, assessment_by_policy)
        ]
        eligible.sort(
            key=lambda candidate: self._candidate_sort_key(
                candidate,
                assessment_by_policy.get(str(candidate.policy.policy_id)),
                follow_up_denials or [],
            )
        )
        return eligible[:max_candidates]

    async def rerank(
        self,
        merged_condition_json: dict[str, Any],
        candidates: list[PolicyCandidate],
        assessments: list[RecommendationPolicyAssessment],
        base_result_json: dict[str, Any],
        result_limit: int,
        raw_query: str | None = None,
        selected_conditions: dict[str, Any] | None = None,
        follow_up_answers: list[dict[str, Any]] | None = None,
        follow_up_denials: list[dict[str, Any]] | None = None,
        input_issues: list[dict[str, Any]] | None = None,
        profile_conflict_json: list[dict[str, Any]] | None = None,
    ) -> RecommendationRerankOutput:
        user_context = self._user_context(
            merged_condition_json=merged_condition_json,
            raw_query=raw_query,
            selected_conditions=selected_conditions or {},
            follow_up_answers=follow_up_answers or [],
            follow_up_denials=follow_up_denials or [],
            input_issues=input_issues or [],
            profile_conflict_json=profile_conflict_json or [],
        )
        base_result_json = self._with_follow_up_denial_conflicts(
            base_result_json,
            user_context.get("follow_up_denials") or [],
        )
        # candidate_items는 candidates/assessments로 만들어진 base_result_json에서
        # 추출한다(둘의 정보가 result item에 이미 반영돼 있음).
        _ = candidates, assessments
        candidate_items = self._candidate_items(
            base_result_json,
            follow_up_denials=user_context.get("follow_up_denials") or [],
        )
        if not candidate_items:
            return self._fallback(
                base_result_json,
                result_limit,
                "LLM rerank candidate pool is empty",
            )
        try:
            llm_result = await self._call_llm(
                merged_condition_json=merged_condition_json,
                candidate_items=candidate_items,
                result_limit=result_limit,
                user_context=user_context,
            )
            sanitized = self._sanitize_llm_result(
                llm_result,
                candidate_items,
                result_limit,
            )
            if not sanitized.recommendations:
                return self._fallback(
                    base_result_json,
                    result_limit,
                    "LLM rerank returned no valid recommendations",
                )
            return self._apply_llm_result(
                base_result_json=base_result_json,
                llm_result=sanitized,
                result_limit=result_limit,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.logger.warning(
                "LLM rerank failed, using fallback: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return self._fallback(base_result_json, result_limit, error)

    async def _call_llm(
        self,
        merged_condition_json: dict[str, Any],
        candidate_items: list[dict[str, Any]],
        result_limit: int,
        user_context: dict[str, Any] | None = None,
    ) -> LlmRecommendationRerankResult:
        messages = [
            (
                "system",
                """
                너는 한국 복지정책 추천 서비스의 추천 설명 생성기다.

                너의 역할은 이미 DB 검색, Rule Filter, Policy Assessment를 통과한
                후보 정책 목록 안에서만 최종 추천 순서를 정하고, 사용자에게 보여줄
                추천 이유를 한글로 정리하는 것이다.

                === 추천의 최우선 기준(가장 중요) ===
                - 추천 순위의 1순위 기준은 사용자가 raw_query/needs에서 표현한 실제
                  목적(원하는 도움)과 정책 혜택의 직접 관련성이다.
                - 자격조건(target_description, matched_rules 등)은 "신청 가능성이 있는지"
                  확인하는 안전장치일 뿐, 사용자 목적과 직접 관련 없는 정책을 상위로
                  올리는 근거가 아니다. "조건은 맞으니까" 상위로 올리지 않는다.
                - 의도-혜택 매칭 예시:
                  · 양육비/현금지원/생활비를 원하면 → 현금성 급여·바우처·양육비 지원을 우선.
                  · 돌봄을 원하면 → 돌봄서비스·어린이집·아이돌봄 정책을 우선.
                  · 사용자가 법률 문제를 말하지 않았다면 → 무료법률상담은 자격이 맞아도 후순위.
                  · 사용자가 의료비/치료/검진을 말하지 않았다면 → 의료성 정책은 후순위.
                - 사용자가 명시한 목적이 없으면, 가구 상황(자녀 나이 등)에서 가장 보편적으로
                  체감되는 혜택(양육 부담 경감 등)을 우선한다.
                - user_context.follow_up_answers는 시스템이 물어본 추가질문과 사용자의 답변 쌍이다.
                  질문 문장 자체를 사용자의 관심사나 필요로 보지 말고, 반드시 answer를 우선한다.
                - user_context.follow_up_denials / 후보의 follow_up_denial_conflicts는 사용자가
                  "아니요/없어요/필요없어요"라고 부정한 주제다. 해당 주제를 추천 이유로 삼지 말고,
                  그 주제에 의존하는 후보는 다른 대안보다 아래로 둔다.
                  예: 수급 자격을 아니라고 답했으면 기초생활·차상위 수급이 핵심인 정책은 후순위,
                  법률 상담이 필요 없다고 답했으면 무료법률상담은 후순위다.
                  다만 후보를 완전히 버리기보다, 대안이 부족할 때만 낮은 우선순위/확인 필요로 포함한다.

                === 그다음 종합 순위 기준(의도 직접성 다음으로 고려) ===
                의도 직접성이 비슷한 후보들 사이의 순서는 아래를 종합해 정한다.
                이들은 의도 직접성보다 약한 보조 기준이며, 의도와 무관한 정책을 이 기준만으로
                상위로 올리지 않는다.
                - 혜택 크기: 같은 목적이면 체감 혜택이 큰 정책(지원 금액·기간·범위가 큰,
                  benefit_description 기준)을 위로 둔다.
                - 긴급성: 위기·시급한 상황을 다루는 정책(긴급돌봄·위기지원 등)이거나 사용자가
                  급하다고 표현했으면 위로 둔다.
                - 신청 난이도: 신청이 쉽고 빠른 정책(온라인 신청·간단한 서류)을 위로,
                  절차가 복잡하거나 방문 신청만 가능한 정책은 약간 아래로 둔다.
                - 확인 필요 부담: 추가 확인 항목(check_rules)이 적어 바로 신청 가능한 정책을
                  위로, 확인할 게 많은 정책은 약간 아래로 둔다.

                반드시 지켜야 할 규칙:
                1. 입력 후보 목록에 없는 policy_id는 절대 추천하지 않는다.
                2. evidence에 없는 내용을 정책 근거처럼 말하지 않는다.
                3. 지원 가능 여부를 확정적으로 단정하지 않는다.
                4. 추가 확인이 필요한 상태는 그 점을 설명한다.
                5. NOT_MATCH 또는 EXCLUDED 후보는 추천하지 않는다.
                6. priority_score는 사용자가 먼저 확인할 추천 우선순위 점수다.
                   위 "최우선 기준"(사용자 목적-정책 혜택의 직접 관련성)을 가장 크게
                   반영하고, 그다음 위 "종합 순위 기준"(혜택 크기, 긴급성, 신청 난이도,
                   확인 필요 부담)을 함께 고려해 0~1 사이로 준다. 자격조건이 맞는다는
                   이유만으로는 높은 점수를 주지 않는다(사용자 목적과 관련 없으면 후순위로 내린다).
                   모든 후보에 같은 점수나 1.0을 반복하지 말고, 위 기준의 차이가 점수에
                   드러나도록 순위 차이를 분명히 만든다.
                7. priority_label은 "가장 먼저 확인", "우선 확인", "조건 잘 맞음",
                   "추가 확인 필요", "함께 확인" 중 하나를 권장한다.
                8. why_recommended는 "사용자가 원한 도움(목적)"과 "이 정책의 혜택"이
                   어떻게 연결되는지를 먼저 설명하는 자연스러운 한국어 3문장이다.
                   (카드의 "AI 코멘트" 영역에 들어가므로 한두 문장으로 끝내지 말고
                   반드시 3문장으로 충분히 풀어 쓴다.)
                   서술 순서(각 1문장씩, 총 3문장)를 지킨다.
                   - (1문장) 사용자가 원한 도움/목적과 이 정책 혜택의 직접 연결을 말한다.
                     "자격조건이 맞다"가 아니라 "원하는 도움에 이 혜택이 맞다"가 중심이다.
                   - (2문장) 관련된 입력 조건(자녀 나이·가구 상황 등)과 혜택 내용을 덧붙인다.
                   - (3문장) 확인이 필요한 점이나, 이 정책으로 무엇이 도움되는지를 마무리한다.
                     상위로 추천한 정책이면, 왜 먼저 볼 만한지를 "종합 순위 기준"(큰 혜택·
                     긴급함·간편한 신청·적은 확인 부담 중 해당하는 것)으로 한 가지 자연스럽게
                     덧붙여 순위가 납득되게 한다(억지로 모든 기준을 나열하지 않는다).
                   예시(의도 중심, 3문장):
                   - "아이 양육비 부담을 줄이고 싶다는 요청에, 이 정책의 현금성 지원이
                     직접 맞아요. 0세 자녀가 지원 대상에 들어 월 지원금으로 바로 도움이
                     될 수 있어요. 신청 전 출생신고 여부만 확인하면 돼요."
                   - "돌봄이 필요하다는 상황에, 이 정책의 어린이집·아이돌봄 지원이 딱
                     맞아요. 입력하신 자녀 나이도 지원 대상과 맞아 보육 부담을 덜 수
                     있어요. 이용 가능 여부만 확인하면 우선 추천드릴 만해요."
                   다음은 절대 쓰지 않는다.
                   - "DB", "RAG", "policy_rule", "hard rule" 같은 시스템/내부 용어
                   - "REFUGEE_APPLICATION_PENDING_EXCLUDED"처럼 대문자 SNAKE_CASE로 된
                     내부 토큰이나 규칙 코드(사람이 읽는 일반 표현으로 바꿔 쓴다)
                   - "충족/미충족" 같은 판정표 문구
                   - 정책 근거 원문을 그대로 길게 복사하는 것
                9. check_before_apply는 신청 전 확인할 점이 있으면 한 문장으로 쓴다.
                10. reason_summary는 짧고 자연스러운 한글 1~2문장의 사용자 친화적 문장으로
                   작성한다. why_recommended와 동일한 금지 규칙(시스템/내부 용어, 대문자
                   SNAKE_CASE 내부 토큰, 판정표 문구, 근거 원문 복사 금지)을 따른다.
                11. evidences는 카드 UI에 보여줄 짧은 근거 문장이다.
                   각 evidence는 입력 evidence chunk 내용 안에서만 요약하고,
                   source_chunk_id는 반드시 입력 evidence에 존재하는 chunk_id를 사용한다.
                   정책마다 1개(많아도 2개)만 작성하고, snippet은 60~120자로 짧게 쓴다.
                12. used_evidence_chunk_ids는 입력 evidence에 존재하는 chunk_id만 사용한다.
                13. 출력은 지정된 JSON schema만 따른다.
                14. 입력 후보가 충분하면 recommendations는 가능하면 result_limit개를 반환한다.
                   단, 추천할 수 없는 후보를 억지로 포함하지는 않는다.
                15. user_context(raw_query, selected_conditions, merged_condition_json,
                   follow_up_answers, follow_up_denials, input_issues, profile_conflicts)를 참고해
                   사용자가 방금 입력한 조건과 추가질문 답변을 우선 반영한다.
                16. why_recommended는 benefit_description(혜택)과 사용자 목적의 연결을
                   중심으로 쓴다. matched_rules/check_rules/conflicting_conditions는
                   "추천 이유"가 아니라 check_before_apply(신청 전 확인사항)와
                   추가 확인 안내에 활용한다. check_rules(추가 확인/미입력 항목)가
                   있으면 check_before_apply에 반영한다.
                17. check_before_apply는 정책마다 다르게 쓴다. 모든 카드에 같은
                   "신청 기간은 정책 상세에서 확인해주세요." 같은 일반 문구를
                   반복하지 말고, 그 정책의 지원 대상/신청 방법/추가 확인 항목에
                   맞춰 구체적인 확인사항(예: 출생신고·주민등록 여부, 어린이집
                   이용/보육 자격 신청 상태, 보호자 신청 가능 여부)을 쓴다.
                """,
            ),
            (
                "user",
                json.dumps(
                    {
                        "result_limit": result_limit,
                        "merged_condition_json": merged_condition_json,
                        "user_context": user_context or {},
                        "candidate_policies": candidate_items,
                    },
                    ensure_ascii=False,
                ),
            ),
        ]
        if self._llm_invoker is not None:
            result = await asyncio.wait_for(
                self._llm_invoker(messages),
                timeout=self.timeout_seconds,
            )
        else:
            from langchain_openai import ChatOpenAI

            llm_kwargs: dict[str, Any] = {
                "model": self.model,
                "temperature": 0,
                "max_tokens": 3500,
                "timeout": self.timeout_seconds,
            }
            if settings.openai_api_key:
                llm_kwargs["api_key"] = settings.openai_api_key
            structured_llm = ChatOpenAI(**llm_kwargs).with_structured_output(
                LlmRecommendationRerankResult
            )
            result = await asyncio.wait_for(
                structured_llm.ainvoke(messages),
                timeout=self.timeout_seconds,
            )
        if isinstance(result, LlmRecommendationRerankResult):
            return result
        if isinstance(result, dict):
            return LlmRecommendationRerankResult.model_validate(result)
        raise ValueError("LLM rerank response did not match schema")

    def _sanitize_llm_result(
        self,
        llm_result: LlmRecommendationRerankResult,
        candidate_items: list[dict[str, Any]],
        result_limit: int,
    ) -> LlmRecommendationRerankResult:
        candidate_by_policy = {
            str(item["policy_id"]): item
            for item in candidate_items
            if item.get("policy_id") is not None
        }
        sanitized_items: list[LlmRecommendationItem] = []
        seen_policy_ids: set[str] = set()

        for item in llm_result.recommendations:
            policy_id = str(item.policy_id)
            candidate = candidate_by_policy.get(policy_id)
            if candidate is None or policy_id in seen_policy_ids:
                continue

            allowed_chunk_ids = self._candidate_evidence_chunk_ids(candidate)
            sanitized_evidences = self._sanitize_llm_evidences(
                item.evidences,
                allowed_chunk_ids,
            )
            used_chunk_ids = self._deduplicate_strings(
                [
                    str(chunk_id)
                    for chunk_id in item.used_evidence_chunk_ids
                    if str(chunk_id) in allowed_chunk_ids
                ]
                + [evidence.source_chunk_id for evidence in sanitized_evidences]
            )
            sanitized_items.append(
                item.model_copy(
                    update={
                        "policy_id": policy_id,
                        "used_evidence_chunk_ids": used_chunk_ids,
                        "evidences": sanitized_evidences,
                    }
                )
            )
            seen_policy_ids.add(policy_id)
            if len(sanitized_items) >= result_limit:
                break

        return LlmRecommendationRerankResult(
            recommendations=sanitized_items,
            summary_message=llm_result.summary_message,
        )

    def _candidate_evidence_chunk_ids(
        self,
        candidate: dict[str, Any],
    ) -> set[str]:
        return {
            str(evidence.get("chunk_id"))
            for evidence in candidate.get("evidence", [])
            if isinstance(evidence, dict) and evidence.get("chunk_id") not in (None, "")
        }

    def _sanitize_llm_evidences(
        self,
        evidences: list[LlmRecommendationEvidenceItem],
        allowed_chunk_ids: set[str],
    ) -> list[LlmRecommendationEvidenceItem]:
        sanitized: list[LlmRecommendationEvidenceItem] = []
        seen_chunk_ids: set[str] = set()
        for evidence in evidences:
            source_chunk_id = str(evidence.source_chunk_id)
            if (
                source_chunk_id not in allowed_chunk_ids
                or source_chunk_id in seen_chunk_ids
            ):
                continue
            snippet = normalize_card_text(evidence.snippet, limit=160)
            if not snippet:
                continue
            sanitized.append(
                evidence.model_copy(
                    update={
                        "source_chunk_id": source_chunk_id,
                        "snippet": snippet,
                        "evidence_role": (
                            str(evidence.evidence_role).lower()
                            if evidence.evidence_role is not None
                            else None
                        ),
                    }
                )
            )
            seen_chunk_ids.add(source_chunk_id)
            if len(sanitized) >= 3:
                break
        return sanitized

    def _apply_llm_result(
        self,
        base_result_json: dict[str, Any],
        llm_result: LlmRecommendationRerankResult,
        result_limit: int,
    ) -> RecommendationRerankOutput:
        result_json = copy.deepcopy(base_result_json)
        base_results = self._base_results(result_json)
        result_by_policy = {
            str(item.get("policy_id")): item
            for item in base_results
            if item.get("policy_id") is not None
        }
        final_results: list[dict[str, Any]] = []
        rerank_scores: dict[int, float] = {}
        selected_policy_ids: set[str] = set()

        for recommendation in llm_result.recommendations[:result_limit]:
            result_item = copy.deepcopy(result_by_policy.get(recommendation.policy_id))
            if result_item is None:
                continue
            base_reason = (
                result_item.get("recommendation_reason")
                or result_item.get("reason_summary")
                or result_item.get("reason")
            )
            reason_summary = normalize_card_text(
                recommendation.reason_summary or base_reason,
                limit=220,
                max_sentences=2,
            )
            recommendation_reason = normalize_card_text(
                recommendation.recommendation_reason
                or recommendation.reason_summary
                or base_reason,
                limit=220,
                max_sentences=2,
            )
            # 카드 AI 코멘트 본문: 카드가 꽉 차 보이도록 2~3문장까지 허용한다.
            why_recommended = normalize_card_text(
                recommendation.why_recommended
                or recommendation_reason
                or reason_summary,
                limit=300,
                max_sentences=3,
            )
            check_before_apply = normalize_card_text(
                recommendation.check_before_apply
                or recommendation.manual_check_summary,
                limit=180,
                max_sentences=1,
            )
            llm_card_evidences = self._llm_card_evidences(
                recommendation,
                result_item,
            )
            raw_match_score = self._to_float_or_none(result_item.get("match_score"))
            result_item.update(
                {
                    "rerank_score": recommendation.rerank_score,
                    "priority_score": recommendation.priority_score,
                    "priority_label": normalize_card_text(
                        recommendation.priority_label,
                        limit=40,
                    ),
                    "raw_match_score": raw_match_score,
                    "reason_summary": reason_summary,
                    "reason": reason_summary,
                    "recommendation_reason": recommendation_reason,
                    "why_recommended": why_recommended,
                    "check_before_apply": check_before_apply,
                    "manual_check_summary": recommendation.manual_check_summary,
                    "used_evidence_chunk_ids": (
                        recommendation.used_evidence_chunk_ids
                    ),
                }
            )
            if llm_card_evidences:
                result_item["evidences"] = llm_card_evidences
                result_item["evidence"] = llm_card_evidences
                result_item["llm_evidence_used"] = True
            final_results.append(result_item)
            selected_policy_ids.add(recommendation.policy_id)
            try:
                rerank_scores[int(recommendation.policy_id)] = (
                    recommendation.rerank_score
                )
            except ValueError:
                continue

        if not final_results:
            return self._fallback(
                base_result_json,
                result_limit,
                "LLM rerank result became empty after applying recommendations",
            )

        llm_selected_count = len(final_results)
        for base_item in base_results:
            if len(final_results) >= result_limit:
                break
            policy_id = str(base_item.get("policy_id"))
            if not policy_id or policy_id in selected_policy_ids:
                continue
            backfilled_item = copy.deepcopy(base_item)
            backfilled_item["llm_backfilled"] = True
            final_results.append(backfilled_item)
            selected_policy_ids.add(policy_id)
        llm_backfilled_count = len(final_results) - llm_selected_count
        final_results = self._demote_follow_up_denial_conflicts(final_results)
        self._apply_priority_presentation(final_results)

        summary = dict(result_json.get("summary") or {})
        summary.update(
            {
                "candidate_count": len(final_results),
                "result_count": len(final_results),
                "llm_rerank_used": True,
                "llm_fallback_used": False,
                "llm_error": None,
                "llm_summary_message": llm_result.summary_message,
                "llm_selected_count": llm_selected_count,
                "llm_backfilled_count": llm_backfilled_count,
                "llm_candidate_pool_count": len(base_results),
                "priority_scoring_used": True,
            }
        )
        result_json["results"] = final_results
        result_json["recommendations"] = final_results
        result_json["summary"] = summary
        result_json = normalize_recommendation_result_json(result_json)
        return RecommendationRerankOutput(
            result_json=result_json,
            rerank_scores=rerank_scores,
            fallback_used=False,
            error=None,
        )

    def _demote_follow_up_denial_conflicts(
        self,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            item
            for _, item in sorted(
                enumerate(results),
                key=lambda pair: (
                    1 if pair[1].get("follow_up_denial_conflicts") else 0,
                    pair[0],
                ),
            )
        ]

    def _llm_card_evidences(
        self,
        recommendation: LlmRecommendationItem,
        result_item: dict[str, Any],
    ) -> list[dict[str, Any]]:
        source_by_chunk = self._result_evidence_by_chunk(result_item)
        policy_id = str(result_item.get("policy_id") or "")
        card_evidences: list[dict[str, Any]] = []
        for evidence in recommendation.evidences:
            source = source_by_chunk.get(evidence.source_chunk_id)
            if source is None:
                continue
            role = evidence.evidence_role or source.get("evidence_role")
            card_evidences.append(
                {
                    "chunk_id": evidence.source_chunk_id,
                    "policy_id": policy_id or source.get("policy_id") or "",
                    "snippet": normalize_card_text(evidence.snippet, limit=160),
                    "source_title": str(source.get("source_title") or ""),
                    "source_url": str(source.get("source_url") or ""),
                    "score": source.get("score") or source.get("similarity_score"),
                    "evidence_role": str(role).lower() if role is not None else None,
                }
            )
            if len(card_evidences) >= 3:
                break
        return card_evidences

    def _result_evidence_by_chunk(
        self,
        result_item: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        evidences = (
            result_item.get("raw_evidences")
            or result_item.get("evidence")
            or result_item.get("evidences")
            or []
        )
        if not isinstance(evidences, list):
            return {}
        return {
            str(evidence.get("chunk_id")): evidence
            for evidence in evidences
            if isinstance(evidence, dict) and evidence.get("chunk_id") not in (None, "")
        }

    def _apply_priority_presentation(
        self,
        results: list[dict[str, Any]],
    ) -> None:
        previous_score: float | None = None
        for index, item in enumerate(results):
            raw_match_score = self._to_float_or_none(item.get("raw_match_score"))
            if raw_match_score is None:
                raw_match_score = self._to_float_or_none(item.get("match_score"))
            item["raw_match_score"] = raw_match_score

            source_score = (
                self._to_float_or_none(item.get("priority_score"))
                or self._to_float_or_none(item.get("rerank_score"))
                or raw_match_score
                or 0.5
            )
            priority_score = self._rank_adjusted_score(
                source_score,
                index,
                previous_score,
            )
            # priority_score는 순위 정렬용 합성 점수다. 표시용 적합도
            # (condition_match_score/confidence_score)와 분리하기 위해
            # match_score를 이 합성값으로 덮지 않는다(원래 룰 매칭 점수 유지).
            item["priority_score"] = priority_score
            item["recommendation_rank"] = index + 1

            if not item.get("priority_label"):
                item["priority_label"] = self._default_priority_label(item, index)
            if not item.get("why_recommended"):
                # LLM이 why_recommended를 비워 보낸 경우의 fallback도
                # 카드 AI 코멘트 본문 기준(2~3문장)에 맞춘다.
                item["why_recommended"] = normalize_card_text(
                    item.get("recommendation_reason")
                    or item.get("reason_summary")
                    or item.get("reason"),
                    limit=300,
                    max_sentences=3,
                )
            if not item.get("check_before_apply"):
                item["check_before_apply"] = self._default_check_before_apply(item)
            previous_score = priority_score

    def _rank_adjusted_score(
        self,
        source_score: float,
        rank_index: int,
        previous_score: float | None,
    ) -> float:
        upper_bound = max(0.72, 0.97 - rank_index * 0.04)
        score = min(max(source_score, 0.45), upper_bound)
        if previous_score is not None and score >= previous_score:
            score = max(0.45, previous_score - 0.03)
        return round(score, 2)

    def _default_priority_label(
        self,
        item: dict[str, Any],
        rank_index: int,
    ) -> str:
        if item.get("llm_backfilled"):
            return "함께 확인"
        if str(item.get("user_status") or "") == "NEEDS_CONFIRMATION":
            return "추가 확인 필요"
        if rank_index == 0:
            return "가장 먼저 확인"
        if rank_index <= 2:
            return "우선 확인"
        return "조건 잘 맞음"

    _FIELD_LABELS = {
        "stage": "생애주기",
        "childAge": "자녀 연령",
        "child_age": "자녀 연령",
        "region": "거주 지역",
        "income": "소득/수급 자격",
        "special": "가구 특성",
        "needs": "관심 지원",
    }

    def _default_check_before_apply(self, item: dict[str, Any]) -> str:
        # LLM이 check_before_apply를 주지 않은 경우, 정책별 정보로 구체적 확인사항 생성.
        summary = normalize_card_text(
            item.get("manual_check_summary"),
            limit=180,
            max_sentences=1,
        )
        if summary:
            return summary

        reasons = self._check_reasons(item.get("manual_check_points"))
        if not reasons:
            reasons = self._check_reasons(item.get("missing_conditions"))
        if reasons:
            return normalize_card_text(reasons[0], limit=180, max_sentences=1)

        labels = self._check_field_labels(
            item.get("manual_check_points") or item.get("missing_conditions") or []
        )
        if labels:
            return f"{', '.join(labels)} 조건 해당 여부를 확인해 주세요."

        # target_description 원문은 문장이 잘려 어색하므로 직접 삽입하지 않는다.
        application = normalize_card_text(
            item.get("application_method"), limit=60, max_sentences=1
        )
        if application:
            return f"신청 방법({application})과 제출 서류를 신청 전 확인해 주세요."

        policy_name = str(item.get("policy_name") or "").strip()
        if policy_name:
            return f"{policy_name}의 지원 대상 조건 해당 여부와 제출 서류를 확인해 주세요."
        return "지원 대상 조건 해당 여부와 제출 서류를 신청 전 확인해 주세요."

    def _check_reasons(self, rows: Any) -> list[str]:
        if not isinstance(rows, list):
            return []
        reasons: list[str] = []
        for row in rows:
            if isinstance(row, dict) and row.get("reason"):
                text = str(row["reason"]).strip()
                # 내부 규칙 코드(예: "FOSTERCAREPARTICIPATIONNOTMODELED")가 그대로
                # 확인사항으로 노출되지 않도록 사람이 읽는 한글 문구만 사용한다.
                if self._is_human_reason(text):
                    reasons.append(text)
        return reasons

    def _is_human_reason(self, text: str) -> bool:
        """한글이 포함된 사람이 읽는 문구인지(내부 영문 토큰/규칙 코드 제외)."""
        return any("가" <= ch <= "힣" for ch in text)

    def _check_field_labels(self, rows: Any) -> list[str]:
        if not isinstance(rows, list):
            return []
        labels: list[str] = []
        for row in rows:
            field_name = (
                str(row.get("field") or row.get("field_name") or "")
                if isinstance(row, dict)
                else ""
            )
            label = self._FIELD_LABELS.get(field_name)
            if label and label not in labels:
                labels.append(label)
            if len(labels) >= 3:
                break
        return labels

    def _to_float_or_none(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _fallback(
        self,
        base_result_json: dict[str, Any],
        result_limit: int,
        error: str,
    ) -> RecommendationRerankOutput:
        result_json = copy.deepcopy(base_result_json)
        fallback_results = self._demote_follow_up_denial_conflicts(
            self._base_results(result_json)
        )[:result_limit]
        base_results = self._base_results(result_json)
        self._apply_priority_presentation(fallback_results)
        summary = dict(result_json.get("summary") or {})
        summary.update(
            {
                "candidate_count": len(fallback_results),
                "result_count": len(fallback_results),
                "llm_candidate_pool_count": len(base_results),
                "llm_selected_count": 0,
                "llm_backfilled_count": 0,
                "llm_rerank_used": False,
                "llm_fallback_used": True,
                "llm_error": error,
                "priority_scoring_used": True,
            }
        )
        result_json["results"] = fallback_results
        result_json["recommendations"] = fallback_results
        result_json["summary"] = summary
        result_json = normalize_recommendation_result_json(result_json)
        return RecommendationRerankOutput(
            result_json=result_json,
            rerank_scores={},
            fallback_used=True,
            error=error,
        )

    def _candidate_items(
        self,
        base_result_json: dict[str, Any],
        follow_up_denials: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in self._base_results(base_result_json):
            if item.get("candidate_status") == CANDIDATE_STATUS_EXCLUDED:
                continue
            if item.get("assessment_status") == AssessmentStatus.NOT_MATCH.value:
                continue
            filter_match_json = item.get("filter_match_json") or {}
            items.append(
                {
                    "policy_id": str(item.get("policy_id")),
                    "policy_name": item.get("policy_name"),
                    "summary": self._short(item.get("summary"), 120),
                    "target_description": self._short(
                        item.get("target_description"), 140
                    ),
                    "benefit_description": self._short(
                        item.get("benefit_description"), 140
                    ),
                    "application_method": self._short(
                        item.get("application_method"), 80
                    ),
                    "retrieval_score": item.get("retrieval_score"),
                    "user_status": item.get("user_status"),
                    "confidence_score": item.get("confidence_score"),
                    "matched_rules": self._compact_rules(
                        filter_match_json.get("matched_rules"),
                        drop_fields=("candidate_search",),
                    ),
                    "check_rules": self._compact_rules(
                        (filter_match_json.get("uncertain_rules") or [])
                        + (item.get("manual_check_points") or [])
                        + (item.get("missing_conditions") or [])
                    ),
                    "conflicting_conditions": self._compact_rules(
                        item.get("conflicting_conditions")
                    ),
                    "follow_up_denial_conflicts": self._follow_up_denial_conflicts(
                        item,
                        follow_up_denials or [],
                    ),
                    "evidence": self._compact_evidences(
                        item.get("raw_evidences")
                        or item.get("evidence")
                        or item.get("evidences")
                        or []
                    ),
                }
            )
        return items

    def _with_follow_up_denial_conflicts(
        self,
        result_json: dict[str, Any],
        follow_up_denials: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not follow_up_denials:
            return result_json
        copied = copy.deepcopy(result_json)
        results = copied.get("results") or copied.get("recommendations") or []
        if not isinstance(results, list):
            return copied
        for item in results:
            if not isinstance(item, dict):
                continue
            conflicts = self._follow_up_denial_conflicts(item, follow_up_denials)
            if conflicts:
                item["follow_up_denial_conflicts"] = conflicts
        copied["results"] = results
        copied["recommendations"] = results
        return copied

    def _follow_up_denial_conflicts(
        self,
        item: dict[str, Any],
        follow_up_denials: list[dict[str, Any]],
    ) -> list[str]:
        if not follow_up_denials:
            return []
        conflicts: list[str] = []
        for denial in follow_up_denials:
            if not isinstance(denial, dict):
                continue
            topic = str(denial.get("topic") or "").strip()
            category = str(denial.get("category") or "").strip()
            if not topic or not self._candidate_matches_denied_topic(item, category):
                continue
            conflicts.append(f"사용자가 {topic}에 해당하지 않는다고 답함")
            if len(conflicts) >= 3:
                break
        return conflicts

    # 수급 자격(recipient) enum. 정책이 이 값을 '요구'할 때만 income 부정과 충돌로 본다.
    _INCOME_RECIPIENT_VALUES = (
        "basic_livelihood_recipient",
        "livelihood_benefit_recipient",
        "medical_benefit_recipient",
        "housing_benefit_recipient",
        "education_benefit_recipient",
        "near_poverty_class",
    )

    def _candidate_matches_denied_topic(
        self,
        item: dict[str, Any],
        category: str,
    ) -> bool:
        # 자유 텍스트 전체 키워드 매칭은 "수급자 제외", "법률 상담이 아닌 생활지원"처럼
        # 해당 조건을 요구하지 않는 문맥까지 충돌로 잡는다. 그래서 구조화된 '요구/확인'
        # 신호(룰 field/value, 대상·확인 맥락) 위주로 판정해 정상 후보 오탐을 줄인다.
        if category == "income_status":
            # 정책이 실제로 '수급 자격(recipient)'을 조건/확인 항목으로 요구할 때만.
            return self._requires_income_status_recipient(item)
        if category == "legal_need":
            # 법률은 룰 field가 없으므로 정책명·대상(요구 맥락)으로만 본다(혜택 본문 제외).
            text = self._candidate_requirement_text(item)
            return self._contains_any(
                text, ("법률상담", "법률 상담", "무료법률", "법률구조", "소송 지원")
            )
        if category == "birth_registration":
            text = self._candidate_requirement_text(item)
            return self._contains_any(text, ("출생신고", "주민등록"))
        # 모호한 기타 카테고리는 오탐 방지를 위해 충돌로 잡지 않는다.
        return False

    def _requires_income_status_recipient(self, item: dict[str, Any]) -> bool:
        """정책이 수급 자격(recipient)을 요구/확인하는 룰을 가졌는지(구조화 판정)."""
        filter_match_json = item.get("filter_match_json") or {}
        if not isinstance(filter_match_json, dict):
            return False
        for key in ("matched_rules", "uncertain_rules", "excluded_rules"):
            for rule in filter_match_json.get(key) or []:
                if not isinstance(rule, dict):
                    continue
                if str(rule.get("field") or "") != "income_status":
                    continue
                policy_value = str(rule.get("policy_value") or "")
                if any(rv in policy_value for rv in self._INCOME_RECIPIENT_VALUES):
                    return True
        return False

    def _candidate_requirement_text(self, item: dict[str, Any]) -> str:
        """정책의 '대상/요구/확인' 맥락만 모은다(혜택·요약 본문의 부수 언급은 제외)."""
        parts: list[str] = [
            str(item.get("policy_name") or ""),
            str(item.get("target_description") or ""),
            str(item.get("check_before_apply") or ""),
        ]
        filter_match_json = item.get("filter_match_json") or {}
        if isinstance(filter_match_json, dict):
            for key in ("matched_rules", "uncertain_rules", "excluded_rules"):
                for rule in filter_match_json.get(key) or []:
                    if isinstance(rule, dict):
                        parts.append(str(rule.get("reason") or ""))
        return " ".join(parts)

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)

    def _short(self, value: Any, limit: int) -> str:
        return normalize_card_text(value, limit=limit)

    def _compact_rules(
        self,
        rules: Any,
        limit: int = 5,
        drop_fields: tuple[str, ...] = (),
    ) -> list[dict[str, str]]:
        # 룰 dict를 field+reason 으로만 축약해 LLM 입력 토큰을 줄인다.
        if not isinstance(rules, list):
            return []
        compact: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            field_name = str(rule.get("field") or rule.get("field_name") or "")
            if field_name in drop_fields:
                continue
            reason = self._short(rule.get("reason"), 80)
            key = (field_name, reason)
            if key in seen:
                continue
            seen.add(key)
            compact.append({"field": field_name, "reason": reason})
            if len(compact) >= limit:
                break
        return compact

    def _user_context(
        self,
        merged_condition_json: dict[str, Any],
        raw_query: str | None,
        selected_conditions: dict[str, Any],
        follow_up_answers: list[dict[str, Any]],
        follow_up_denials: list[dict[str, Any]],
        input_issues: list[dict[str, Any]],
        profile_conflict_json: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "raw_query": raw_query or "",
            "selected_conditions": selected_conditions or {},
            "merged_condition_json": merged_condition_json or {},
            "follow_up_answers": follow_up_answers or [],
            "follow_up_denials": follow_up_denials or [],
            "input_issues": input_issues or [],
            "profile_conflicts": profile_conflict_json or [],
        }

    def _compact_evidences(
        self,
        evidences: list[Any],
    ) -> list[dict[str, Any]]:
        # LLM 입력 토큰을 줄이기 위해 개수/길이를 제한하고 인용에 필요한
        # chunk_id + snippet(+role)만 전달한다. source_title/url 은 카드 단계에서
        # raw_evidences 로 다시 채워지므로 LLM 입력에서는 생략한다.
        compacted: list[dict[str, Any]] = []
        for evidence in evidences[:LLM_EVIDENCE_MAX_ITEMS]:
            if isinstance(evidence, str):
                snippet = normalize_card_text(
                    _strip_chunk_meta(evidence), limit=LLM_EVIDENCE_SNIPPET_LIMIT
                )
                if not snippet:
                    continue
                compacted.append({"chunk_id": "", "snippet": snippet})
                continue
            if not isinstance(evidence, dict):
                continue
            snippet = normalize_card_text(
                _strip_chunk_meta(
                    evidence.get("snippet")
                    or evidence.get("content")
                    or evidence.get("text")
                    or evidence.get("quote")
                    or ""
                ),
                limit=LLM_EVIDENCE_SNIPPET_LIMIT,
            )
            if not snippet:
                continue
            compacted.append(
                {
                    "chunk_id": str(evidence.get("chunk_id")),
                    "snippet": snippet,
                    "evidence_role": evidence.get("evidence_role"),
                }
            )
        return compacted

    def _base_results(self, result_json: dict[str, Any]) -> list[dict[str, Any]]:
        value = result_json.get("results") or result_json.get("recommendations") or []
        return [item for item in value if isinstance(item, dict)]

    def _is_eligible_candidate(
        self,
        candidate: PolicyCandidate,
        assessment_by_policy: dict[str, RecommendationPolicyAssessment],
    ) -> bool:
        if candidate.candidate_status == CANDIDATE_STATUS_EXCLUDED:
            return False
        assessment = assessment_by_policy.get(str(candidate.policy.policy_id))
        if assessment is None:
            return True
        return assessment.assessment_status != AssessmentStatus.NOT_MATCH

    def _candidate_sort_key(
        self,
        candidate: PolicyCandidate,
        assessment: RecommendationPolicyAssessment | None,
        follow_up_denials: list[dict[str, Any]] | None = None,
    ) -> tuple[int, int, float]:
        priority_by_user_status = {
            "RECOMMENDABLE": 0,
            "NEEDS_CONFIRMATION": 1,
            "DIFFICULT_TO_RECOMMEND": 2,
        }
        user_status = assessment.user_status.value if assessment else ""
        denial_penalty = 1 if self._candidate_denial_conflicts(
            candidate,
            follow_up_denials or [],
        ) else 0
        return (
            denial_penalty,
            priority_by_user_status.get(user_status, 1),
            -candidate.retrieval_score,
        )

    def _candidate_denial_conflicts(
        self,
        candidate: PolicyCandidate,
        follow_up_denials: list[dict[str, Any]],
    ) -> list[str]:
        if not follow_up_denials:
            return []
        detail = candidate.detail
        # 후보를 result item과 같은 형태로 맞춰 구조화 매칭(_candidate_matches_denied_topic)에 넘긴다.
        item = {
            "policy_name": str(candidate.policy.policy_name or ""),
            "target_description": str(getattr(detail, "target_description", "") or ""),
            "check_before_apply": "",
            "filter_match_json": candidate.filter_match_json or {},
        }
        conflicts: list[str] = []
        for denial in follow_up_denials:
            if not isinstance(denial, dict):
                continue
            topic = str(denial.get("topic") or "").strip()
            category = str(denial.get("category") or "").strip()
            if topic and self._candidate_matches_denied_topic(item, category):
                conflicts.append(f"사용자가 {topic}에 해당하지 않는다고 답함")
        return conflicts

    def _assessment_by_policy(
        self,
        assessments: list[RecommendationPolicyAssessment],
    ) -> dict[str, RecommendationPolicyAssessment]:
        return {
            str(assessment.policy_id): assessment
            for assessment in assessments
        }

    def _deduplicate_strings(self, values: list[str]) -> list[str]:
        deduplicated: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            deduplicated.append(value)
        return deduplicated

    def _short_text(self, value: str, limit: int = 800) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[:limit].rstrip()}..."
