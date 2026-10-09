import asyncio
from app.ai.tools.policy_chunk_search_tool import (
    _select_section_aware_results,
    infer_application_section_subtype,
    search_policy_chunks,
)
from app.schemas.policy_rag_schema import (
    PolicyRagSearchResponse,
    PolicyRagSearchResult,
)


def _result(rank: int, section: str) -> PolicyRagSearchResult:
    return PolicyRagSearchResult(
        chunk_id=100 + rank,
        document_id=10,
        policy_id=242,
        policy_code="WLF00006308",
        policy_name="무료법률상담",
        section=section,
        source_type="POLICY_DETAIL",
        source_title="무료법률상담 정책 상세",
        source_url="local://policy/242",
        chunk_text=f"{section} 근거",
        distance=float(rank) / 10,
    )


def test_infer_application_section_subtype_distinguishes_method_period() -> None:
    assert (
        infer_application_section_subtype("어디에서 신청하면 되나요?")
        == "APPLICATION_METHOD"
    )
    assert (
        infer_application_section_subtype("신청 기간은 언제까지인가요?")
        == "APPLICATION_PERIOD"
    )
    assert (
        infer_application_section_subtype("신청 가능한 시기가 따로 정해져 있나요?")
        == "APPLICATION_PERIOD"
    )
    assert infer_application_section_subtype("신청할 때 어떤 서류가 필요한가요?") is None
    assert infer_application_section_subtype("지원 금액이 얼마인가요?") is None


def test_infer_application_section_subtype_ignores_non_application_period_word() -> None:
    assert (
        infer_application_section_subtype("휴직 기간 급여는 어디에 신청하나요?")
        == "APPLICATION_METHOD"
    )


def test_section_aware_selection_guarantees_target_from_larger_pool() -> None:
    results = [
        _result(1, "조건 구조"),
        _result(2, "공식 지원대상 원문"),
        _result(3, "정리된 지원 조건"),
        _result(4, "지원 내용"),
        _result(5, "유의 사항"),
        _result(6, "신청 방법"),
        _result(7, "신청 기간"),
    ]

    selected = _select_section_aware_results(
        results,
        top_k=5,
        section_subtype="APPLICATION_PERIOD",
    )

    assert len(selected) == 5
    assert [item.section for item in selected] == [
        "조건 구조",
        "공식 지원대상 원문",
        "정리된 지원 조건",
        "지원 내용",
        "신청 기간",
    ]


def test_section_aware_selection_keeps_existing_target_order() -> None:
    results = [
        _result(1, "조건 구조"),
        _result(2, "신청 방법"),
        _result(3, "지원 내용"),
        _result(4, "신청 기간"),
        _result(5, "유의 사항"),
        _result(6, "공식 지원대상 원문"),
        _result(7, "정리된 지원 조건"),
    ]

    selected = _select_section_aware_results(
        results,
        top_k=5,
        section_subtype="APPLICATION_METHOD",
    )

    assert [item.section for item in selected] == [
        "조건 구조",
        "신청 방법",
        "지원 내용",
        "신청 기간",
        "유의 사항",
    ]


def test_search_policy_chunks_uses_larger_candidate_pool_only_when_requested() -> None:
    calls: list[dict] = []
    response = PolicyRagSearchResponse(
        query="질문",
        result_count=7,
        results=[
            _result(1, "조건 구조"),
            _result(2, "공식 지원대상 원문"),
            _result(3, "정리된 지원 조건"),
            _result(4, "지원 내용"),
            _result(5, "유의 사항"),
            _result(6, "신청 방법"),
            _result(7, "신청 기간"),
        ],
    )

    class FakeRagService:
        async def search(self, **kwargs):
            calls.append(kwargs)
            return response

    chunks = asyncio.run(
        search_policy_chunks(
            query="신청 기간은 언제인가요?",
            policy_ids=[242],
            top_k=5,
            section_subtype="APPLICATION_PERIOD",
            rag_service=FakeRagService(),
        )
    )

    assert calls == [{"query": "신청 기간은 언제인가요?", "k": 7, "policy_ids": [242]}]
    assert len(chunks) == 5
    assert chunks[-1].chunk_id == 107


def test_search_policy_chunks_preserves_baseline_k_without_section_hint() -> None:
    calls: list[dict] = []
    response = PolicyRagSearchResponse(
        query="질문",
        result_count=5,
        results=[_result(i, section) for i, section in enumerate(
            ["조건 구조", "지원 내용", "유의 사항", "신청 방법", "신청 기간"],
            start=1,
        )],
    )

    class FakeRagService:
        async def search(self, **kwargs):
            calls.append(kwargs)
            return response

    chunks = asyncio.run(
        search_policy_chunks(
            query="지원 내용 알려줘",
            policy_ids=[242],
            top_k=5,
            rag_service=FakeRagService(),
        )
    )

    assert calls == [{"query": "지원 내용 알려줘", "k": 5, "policy_ids": [242]}]
    assert len(chunks) == 5


def test_search_policy_chunks_records_only_returned_evidence() -> None:
    recorded: list[PolicyRagSearchResponse] = []
    response = PolicyRagSearchResponse(
        query="신청 기간은 언제인가요?",
        result_count=7,
        results=[
            _result(1, "조건 구조"),
            _result(2, "지원 내용"),
            _result(3, "유의 사항"),
            _result(4, "신청 방법"),
            _result(5, "공식 지원대상 원문"),
            _result(6, "정리된 지원 조건"),
            _result(7, "신청 기간"),
        ],
    )

    class FakeRagService:
        async def search(self, **kwargs):
            return response

        async def record_search_evidence(self, evidence):
            recorded.append(evidence)

    chunks = asyncio.run(
        search_policy_chunks(
            query="신청 기간은 언제인가요?",
            policy_ids=[242],
            top_k=5,
            section_subtype="APPLICATION_PERIOD",
            rag_service=FakeRagService(),
        )
    )

    assert [chunk.chunk_id for chunk in chunks] == [101, 102, 103, 104, 107]
    assert len(recorded) == 1
    assert [result.chunk_id for result in recorded[0].results] == [
        101,
        102,
        103,
        104,
        107,
    ]
