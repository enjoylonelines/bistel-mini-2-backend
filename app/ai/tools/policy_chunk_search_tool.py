import re
from collections.abc import Sequence

from app.schemas.ai_contract import EvidenceChunk
from app.schemas.policy_rag_schema import PolicyRagSearchResponse, PolicyRagSearchResult
from app.services.policy_rag_service import PolicyRagService

_RAW_STRUCTURED_RE = re.compile(
    r"\b(?:operator|matchingstrength|confidence):\s*\S"
    r"|\bsourcetext:\s*\S.*\breason:\s*[A-Z_]{5}",
    re.IGNORECASE | re.DOTALL,
)

ROLE_BY_SECTION = {
    "기본 정보": "SUMMARY",
    "요약": "SUMMARY",
    "지원 대상": "TARGET",
    "지원 내용": "BENEFIT",
    "신청 방법": "APPLICATION",
    "신청 기간": "APPLICATION",
    "유의 사항": "CAUTION",
}

SECTION_BY_SUBTYPE = {
    "APPLICATION_METHOD": "신청 방법",
    "APPLICATION_PERIOD": "신청 기간",
}

SEMANTIC_SECTION_BY_HINT = {
    "TARGET": "TARGET",
    "BENEFIT": "BENEFIT",
    "APPLICATION_METHOD": "APPLICATION",
    "APPLICATION_PERIOD": "APPLICATION",
}

_DOCUMENT_RE = re.compile(
    r"(서류|증빙|증빙자료|준비물|제출자료|제출\s*서류|구비서류)"
)
_PERIOD_RE = re.compile(
    r"(언제\s*신청|신청\s*기간|접수\s*기간|모집\s*기간|기간\s*없이|"
    r"신청\s*(?:가능한\s*)?시기|접수\s*시기|마감|언제까지|"
    r"상시\s*(?:모집|신청)|신청\s*기한|접수\s*기한)"
)
_METHOD_RE = re.compile(
    r"(어디|어떤\s*기관|어떻게|신청|문의|온라인|주민센터|보건소|카드사|공단)"
)
_TARGET_RE = re.compile(
    r"(누가\s*(?:받|신청|대상)|지원\s*대상|대상자는?|받을\s*수\s*있|"
    r"자격|선정\s*기준|해당(?:되나요|하나요)?)"
)
_BENEFIT_RE = re.compile(
    r"(지원\s*내용|무엇(?:을|이)\s*지원|어떤\s*(?:도움|지원|혜택)|"
    r"얼마(?:를|나)?\s*(?:지원|받)|지원금|급여)"
)

def infer_application_section_subtype(query: str) -> str | None:
    normalized = " ".join(str(query or "").split())
    if _DOCUMENT_RE.search(normalized):
        return None
    if _PERIOD_RE.search(normalized):
        return "APPLICATION_PERIOD"
    if _METHOD_RE.search(normalized):
        return "APPLICATION_METHOD"
    return None


def infer_section_hint(query: str) -> str | None:
    """Infer an evidence section solely from wording in the user query.

    This intentionally does not accept a gold-set axis or expected section. It
    is a conservative ordering hint: ambiguous and document-oriented questions
    keep the vector ranking unchanged.
    """
    normalized = " ".join(str(query or "").split())
    if _DOCUMENT_RE.search(normalized):
        return None
    if _PERIOD_RE.search(normalized):
        return "APPLICATION_PERIOD"
    if _TARGET_RE.search(normalized):
        return "TARGET"
    if _BENEFIT_RE.search(normalized):
        return "BENEFIT"
    if _METHOD_RE.search(normalized):
        return "APPLICATION_METHOD"
    return None


def _matches_section_hint(result: PolicyRagSearchResult, hint: str) -> bool:
    target_section = SECTION_BY_SUBTYPE.get(hint)
    if target_section is not None:
        return result.section == target_section
    semantic_section = SEMANTIC_SECTION_BY_HINT.get(hint)
    return result.semantic_section == semantic_section


def _order_section_aware_results(
    results: Sequence[PolicyRagSearchResult],
    *,
    top_k: int,
    section_hint: str | None,
) -> list[PolicyRagSearchResult]:
    """Promote a matching retrieved section without inventing a result.

    The function only reorders the vector candidate pool. If no matching
    section was retrieved, the original vector order remains intact.
    """
    visible = list(results[:top_k])
    hint = str(section_hint or "").upper()
    if not hint:
        return visible

    target = next((item for item in results if _matches_section_hint(item, hint)), None)
    if target is None:
        return visible
    return [target, *(item for item in results if item is not target)][:top_k]


def _select_section_aware_results(
    results: Sequence[PolicyRagSearchResult],
    *,
    top_k: int,
    section_subtype: str | None,
) -> list[PolicyRagSearchResult]:
    return _order_section_aware_results(
        results,
        top_k=top_k,
        section_hint=section_subtype,
    )


async def search_policy_chunks(
    query: str,
    policy_ids: list[int | str] | None = None,
    top_k: int = 5,
    evidence_role: str | None = None,
    rag_service: PolicyRagService | None = None,
    section_subtype: str | None = None,
    candidate_k: int | None = None,
) -> list[EvidenceChunk]:
    service = rag_service or PolicyRagService()
    section_hint = section_subtype or infer_section_hint(query)
    requested_candidate_k = candidate_k or top_k
    if section_hint:
        requested_candidate_k = max(requested_candidate_k, top_k, 7)

    response = await service.search(
        query=query,
        k=requested_candidate_k,
        policy_ids=policy_ids,
    )
    selected_results = _select_section_aware_results(
        response.results,
        top_k=top_k,
        section_subtype=section_hint,
    )

    allowed_policy_ids = {str(policy_id) for policy_id in policy_ids or []}
    chunks: list[EvidenceChunk] = []
    audited_results: list[PolicyRagSearchResult] = []
    for result in selected_results:
        if result.chunk_id is None or result.policy_id is None:
            continue
        if _RAW_STRUCTURED_RE.search(result.chunk_text or ""):
            continue

        policy_keys = {str(result.policy_id)}
        if result.policy_code:
            policy_keys.add(result.policy_code)
        if allowed_policy_ids and not (policy_keys & allowed_policy_ids):
            continue

        audited_results.append(result)
        chunks.append(
            EvidenceChunk(
                chunk_id=result.chunk_id,
                policy_id=result.policy_id,
                snippet=result.chunk_text,
                source_title=(
                    result.source_title
                    or _source_title(result.policy_name, result.section)
                ),
                source_url=result.source_url or "",
                score=_distance_to_score(result.distance),
                evidence_role=(
                    result.evidence_role
                    or _evidence_role(result.section)
                    or evidence_role
                ),
            )
        )

    recorder = getattr(service, "record_search_evidence", None)
    if recorder is not None:
        await recorder(
            PolicyRagSearchResponse(
                query=response.query,
                result_count=len(audited_results),
                results=audited_results,
            )
        )
    return chunks


def _source_title(policy_name: str | None, section: str | None) -> str:
    if policy_name and section:
        return f"{policy_name} - {section}"
    return policy_name or section or "정책 근거"


def _evidence_role(section: str | None) -> str | None:
    if section is None:
        return None
    return ROLE_BY_SECTION.get(section)


def _distance_to_score(distance: float | None) -> float | None:
    if distance is None:
        return None
    return 1 / (1 + max(float(distance), 0.0))
