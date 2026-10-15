import asyncio

from app.services.policy_rag_query import expand_policy_retrieval_query
from app.services.policy_rag_service import PolicyRagService


def test_expands_reviewed_infertility_treatment_paraphrase() -> None:
    query = "임신 준비 시술로 쉰 기간의 임금 보전은 어디에 신청하나요?"

    assert expand_policy_retrieval_query(query) == (
        f"{query} 난임치료 휴가 급여"
    )


def test_expands_newborn_lump_sum_without_matching_other_newborn_questions() -> None:
    assert expand_policy_retrieval_query("신생아 목돈 지원은 누가 받을 수 있나요?") == (
        "신생아 목돈 지원은 누가 받을 수 있나요? 출생아 첫만남 이용권"
    )
    assert expand_policy_retrieval_query("신생아 예방접종은 어디에서 하나요?") == (
        "신생아 예방접종은 어디에서 하나요?"
    )


def test_preserves_unrelated_query() -> None:
    assert expand_policy_retrieval_query("무료 법률상담은 어디서 받나요?") == (
        "무료 법률상담은 어디서 받나요?"
    )


def test_search_expands_vector_query_but_preserves_audit_query() -> None:
    captured: dict[str, str] = {}

    class FakeVectorStore:
        async def asimilarity_search_with_score(self, *, query, k, filter):
            captured["query"] = query
            return []

    service = PolicyRagService()
    service._vectorstore = lambda: FakeVectorStore()

    response = asyncio.run(
        service.search(
            query="신생아 목돈 지원은 누가 받을 수 있나요?",
            k=3,
        )
    )

    assert captured["query"].endswith("출생아 첫만남 이용권")
    assert response.query == "신생아 목돈 지원은 누가 받을 수 있나요?"
