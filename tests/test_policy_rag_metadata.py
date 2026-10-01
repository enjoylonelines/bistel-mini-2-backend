from types import SimpleNamespace

from app.repositories.policy_rag_repository import POLICY_RAG_METADATA_VERSION
from app.services.policy_rag_service import PolicyRagService


def test_build_metadata_adds_policy_and_reference_fields():
    service = PolicyRagService()

    metadata = service._build_metadata(
        {
            "chunk_id": 1,
            "document_id": 10,
            "chunk_index": 0,
            "chunk_text": "제출서류: 지원신청서와 동의서를 제출합니다.",
            "metadata_json": {
                "section": "관련 문서",
                "file_type": "PDF",
            },
            "source_title": "지원신청서 작성 안내.pdf",
            "source_url": "https://example.com/file.pdf",
            "source_type": "POLICY_REFERENCE",
            "policy_id": 100,
            "condition_profile_id": 500,
            "policy_code": "WLF00000001",
            "policy_name": "테스트 정책",
            "main_category": "임신·출산",
            "sub_category": "출산지원",
            "provider_name": "보건복지부",
            "provider_type": "중앙부처",
            "region_scope": "전국",
            "region_code": "ALL",
            "benefit_type": "현금",
            "application_status": "상시",
            "application_start_date": None,
            "application_end_date": None,
            "chunk_hash": "hash",
        }
    )

    assert metadata["metadata_version"] == POLICY_RAG_METADATA_VERSION
    assert metadata["main_category"] == "임신·출산"
    assert metadata["region_code"] == "ALL"
    assert metadata["semantic_section"] == "APPLICATION"
    assert metadata["reference_document_type"] == "application_form"
    assert metadata["source_title"] == "지원신청서 작성 안내.pdf"


def test_build_metadata_adds_detail_semantic_section():
    service = PolicyRagService()

    metadata = service._build_metadata(
        {
            "chunk_id": 2,
            "document_id": 20,
            "chunk_index": 1,
            "chunk_text": "지원 대상 내용",
            "metadata_json": {
                "section": "지원 대상",
                "evidence_role": "target",
                "condition_profile_id": 500,
                "source_basis": "policy_condition_profile",
            },
            "source_title": "테스트 정책 상세 데이터",
            "source_url": "https://example.com/policy",
            "source_type": "POLICY_DETAIL",
            "policy_id": 101,
            "policy_code": "WLF00000002",
            "policy_name": "상세 정책",
            "main_category": None,
            "sub_category": None,
            "provider_name": None,
            "provider_type": None,
            "region_scope": None,
            "region_code": None,
            "benefit_type": None,
            "application_status": None,
            "application_start_date": None,
            "application_end_date": None,
            "chunk_hash": "hash2",
        }
    )

    assert metadata["semantic_section"] == "TARGET"
    assert metadata["evidence_role"] == "target"
    assert metadata["condition_profile_id"] == 500
    assert metadata["source_basis"] == "policy_condition_profile"
    assert "reference_document_type" not in metadata


def test_build_metadata_adds_application_section_subtype() -> None:
    service = PolicyRagService()

    method_metadata = service._build_metadata(
        {
            "chunk_id": 3,
            "document_id": 30,
            "chunk_index": 1,
            "chunk_text": "주민센터에서 신청합니다.",
            "metadata_json": {"section": "신청 방법"},
            "source_title": "테스트 정책 상세 데이터",
            "source_url": "https://example.com/policy",
            "source_type": "POLICY_DETAIL",
            "policy_id": 102,
            "policy_code": "WLF00000003",
            "policy_name": "신청 방법 정책",
            "main_category": None,
            "sub_category": None,
            "provider_name": None,
            "provider_type": None,
            "region_scope": None,
            "region_code": None,
            "benefit_type": None,
            "application_status": None,
            "application_start_date": None,
            "application_end_date": None,
            "chunk_hash": "hash3",
        }
    )
    period_metadata = service._build_metadata(
        {
            "chunk_id": 4,
            "document_id": 40,
            "chunk_index": 1,
            "chunk_text": "신청 기간은 수시입니다.",
            "metadata_json": {"section": "신청 기간"},
            "source_title": "테스트 정책 상세 데이터",
            "source_url": "https://example.com/policy",
            "source_type": "POLICY_DETAIL",
            "policy_id": 103,
            "policy_code": "WLF00000004",
            "policy_name": "신청 기간 정책",
            "main_category": None,
            "sub_category": None,
            "provider_name": None,
            "provider_type": None,
            "region_scope": None,
            "region_code": None,
            "benefit_type": None,
            "application_status": None,
            "application_start_date": None,
            "application_end_date": None,
            "chunk_hash": "hash4",
        }
    )

    assert method_metadata["evidence_role"] == "application"
    assert method_metadata["section_subtype"] == "APPLICATION_METHOD"
    assert period_metadata["evidence_role"] == "application"
    assert period_metadata["section_subtype"] == "APPLICATION_PERIOD"


def test_to_search_result_derives_section_subtype_for_legacy_metadata() -> None:
    service = PolicyRagService()
    document = SimpleNamespace(
        page_content="주민센터에서 신청합니다.",
        metadata={
            "chunk_id": 5,
            "document_id": 50,
            "policy_id": 104,
            "policy_code": "WLF00000005",
            "policy_name": "레거시 메타데이터 정책",
            "section": "신청 방법",
            "semantic_section": "APPLICATION",
            "source_type": "POLICY_DETAIL",
            "source_title": "정책 상세",
            "source_url": "https://example.com/policy",
            "evidence_role": "application",
            "metadata_version": POLICY_RAG_METADATA_VERSION,
        },
    )

    result = service._to_search_result(document, 0.2)

    assert result.section_subtype == "APPLICATION_METHOD"


def test_to_search_result_preserves_metadata_fields():
    service = PolicyRagService()
    document = SimpleNamespace(
        page_content="근거 내용",
        metadata={
            "chunk_id": 1,
            "document_id": 10,
            "policy_id": 100,
            "condition_profile_id": 500,
            "policy_code": "WLF00000001",
            "policy_name": "테스트 정책",
            "section": "관련 문서",
            "semantic_section": "DOCUMENT",
            "section_subtype": "APPLICATION_METHOD",
            "source_type": "POLICY_REFERENCE",
            "source_title": "신청서.pdf",
            "source_url": "https://example.com/file.pdf",
            "evidence_role": "application",
            "metadata_version": POLICY_RAG_METADATA_VERSION,
        },
    )

    result = service._to_search_result(document, 0.25)

    assert result.source_title == "신청서.pdf"
    assert result.condition_profile_id == 500
    assert result.evidence_role == "application"
    assert result.semantic_section == "DOCUMENT"
    assert result.section_subtype == "APPLICATION_METHOD"
    assert result.metadata["metadata_version"] == POLICY_RAG_METADATA_VERSION
