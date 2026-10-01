"""Bootstrap a disposable local pgvector fixture for Dodam RAG experiments.

This is intentionally synthetic and must not be used as production-quality
retrieval evidence. It exists to exercise the real FastAPI/PGVector/OpenAI-
compatible code path locally when the deployed DB is unavailable.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

import app.db.models  # noqa: F401 - register all mapped tables
from app.db.session import Base, engine


POLICY_ID = 242
DOCUMENT_ID = 242900000
POLICY_CODE = "WLF00006308"
POLICY_NAME = "무료법률상담"

CHUNKS = [
    (24290000000, 0, "정리된 지원 조건", "법률 상담이 필요한 국민을 지원 대상으로 합니다."),
    (24290000001, 1, "조건 구조", "법률 문제 상담이 필요한 국민이 이용할 수 있는 무료 법률상담 서비스입니다."),
    (24290000002, 2, "공식 지원대상 원문", "지원 대상은 법률 상담이 필요한 국민입니다."),
    (
        24290000003,
        3,
        "지원 내용",
        "민사소송, 손해배상, 계약, 임대, 개인회생 및 파산 등 법률 문제에 대한 무료 상담을 지원합니다.",
    ),
    (
        24290000004,
        4,
        "신청 방법",
        "신청은 거주지 읍면동 주민센터 또는 대한법률구조공단 등 지정 기관을 통해 오프라인으로 진행합니다.",
    ),
    (24290000005, 5, "신청 기간", "신청 기간은 수시입니다."),
    (
        24290000006,
        6,
        "유의 사항",
        "상담 분야와 이용 가능 기관은 상황에 따라 확인이 필요합니다.",
    ),
]


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)

        # The current ORM has no PolicyTag model, but application startup creates
        # an index for this table.
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS policy_tag (
                    policy_id BIGINT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
                    tag_name VARCHAR(100) NOT NULL
                )
                """
            )
        )

        await conn.execute(
            text(
                """
                INSERT INTO policy (
                    policy_id, policy_code, policy_name, provider_name,
                    benefit_type, application_status, is_active
                )
                VALUES (
                    :policy_id, :policy_code, :policy_name, :provider_name,
                    :benefit_type, :application_status, TRUE
                )
                ON CONFLICT (policy_id) DO UPDATE SET
                    policy_code = EXCLUDED.policy_code,
                    policy_name = EXCLUDED.policy_name,
                    provider_name = EXCLUDED.provider_name,
                    benefit_type = EXCLUDED.benefit_type,
                    application_status = EXCLUDED.application_status,
                    is_active = TRUE
                """
            ),
            {
                "policy_id": POLICY_ID,
                "policy_code": POLICY_CODE,
                "policy_name": POLICY_NAME,
                "provider_name": "대한법률구조공단",
                "benefit_type": "서비스",
                "application_status": "수시",
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO policy_detail (
                    policy_id, easy_summary, target_description,
                    benefit_description, application_method,
                    application_period_text, caution
                )
                VALUES (
                    :policy_id, :easy_summary, :target_description,
                    :benefit_description, :application_method,
                    :application_period_text, :caution
                )
                ON CONFLICT (policy_id) DO UPDATE SET
                    easy_summary = EXCLUDED.easy_summary,
                    target_description = EXCLUDED.target_description,
                    benefit_description = EXCLUDED.benefit_description,
                    application_method = EXCLUDED.application_method,
                    application_period_text = EXCLUDED.application_period_text,
                    caution = EXCLUDED.caution
                """
            ),
            {
                "policy_id": POLICY_ID,
                "easy_summary": "법률 상담이 필요한 국민에게 무료 법률상담을 제공합니다.",
                "target_description": "법률 상담이 필요한 국민",
                "benefit_description": "민사소송, 손해배상, 계약, 임대, 개인회생 및 파산 등 무료 법률상담",
                "application_method": "거주지 읍면동 주민센터 또는 대한법률구조공단 등 지정 기관에서 오프라인 신청",
                "application_period_text": "수시",
                "caution": "상담 분야와 기관별 이용 방법을 확인해야 합니다.",
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO policy_document (
                    document_id, policy_id, source_title, source_url,
                    source_type, raw_text
                )
                VALUES (
                    :document_id, :policy_id, :source_title, :source_url,
                    'POLICY_DETAIL', :raw_text
                )
                ON CONFLICT (document_id) DO UPDATE SET
                    policy_id = EXCLUDED.policy_id,
                    source_title = EXCLUDED.source_title,
                    source_url = EXCLUDED.source_url,
                    source_type = EXCLUDED.source_type,
                    raw_text = EXCLUDED.raw_text
                """
            ),
            {
                "document_id": DOCUMENT_ID,
                "policy_id": POLICY_ID,
                "source_title": f"{POLICY_NAME} 정책 상세",
                "source_url": "local://dodam-deep-dive/policy-242",
                "raw_text": "\n".join(text_value for _, _, _, text_value in CHUNKS),
            },
        )

        for chunk_id, chunk_index, section, chunk_text in CHUNKS:
            await conn.execute(
                text(
                    """
                    INSERT INTO policy_document_chunk (
                        chunk_id, document_id, chunk_index, chunk_text, metadata_json
                    )
                    VALUES (
                        :chunk_id, :document_id, :chunk_index, :chunk_text,
                        jsonb_build_object('section', CAST(:section AS TEXT))
                    )
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        document_id = EXCLUDED.document_id,
                        chunk_index = EXCLUDED.chunk_index,
                        chunk_text = EXCLUDED.chunk_text,
                        metadata_json = EXCLUDED.metadata_json
                    """
                ),
                {
                    "chunk_id": chunk_id,
                    "document_id": DOCUMENT_ID,
                    "chunk_index": chunk_index,
                    "chunk_text": chunk_text,
                    "section": section,
                },
            )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
