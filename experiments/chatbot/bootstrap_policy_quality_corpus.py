"""Rebuild the isolated, source-backed corpus used by the policy-quality gold set.

This creates a disposable local corpus only.  It fetches the seven policy
details already approved in ``dodam_policy_quality_v1.jsonl`` from data.go.kr,
stores their three labeled sections, and embeds them through the application's
current PGVector path.  It is not an import of the complete production corpus.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault(
    "PSYCOPG_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55432/dodam",
)
os.environ.setdefault("JWT_SECRET_KEY", "local-deep-dive-only")

import app.db.models  # noqa: E402,F401 - register mapped tables
from app.common.psycopg_pool_conf import psycopg_pool  # noqa: E402
from app.db.session import Base, engine  # noqa: E402
from app.services.policy_data_service import PolicyDataService  # noqa: E402
from app.services.policy_rag_service import PolicyRagService  # noqa: E402


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    policy_id: int
    service_id: str


SOURCE_POLICIES = (
    SourcePolicy(240, "WLF00006313"),
    SourcePolicy(242, "WLF00006308"),
    SourcePolicy(246, "WLF00006292"),
    SourcePolicy(251, "WLF00003213"),
    SourcePolicy(255, "WLF00004656"),
    SourcePolicy(289, "WLF00000072"),
    SourcePolicy(292, "WLF00000076"),
)


def _detail(service_id: str) -> dict[str, Any]:
    xml_text = PolicyDataService.get_welfare_detail_xml("D", service_id)
    detail = PolicyDataService._xml_to_dict(  # noqa: SLF001 - source parser reuse
        __import__("xml.etree.ElementTree", fromlist=["ET"]).fromstring(
            xml_text.strip()
        )
    )
    detail["applmetList"] = PolicyDataService._find_xml_items(  # noqa: SLF001
        xml_text, "applmetList"
    )
    return detail


def _application_method(items: list[dict[str, Any]]) -> str:
    methods = []
    for item in items:
        name = str(item.get("servSeDetailNm") or "").strip()
        url = str(item.get("servSeDetailLink") or "").strip()
        rendered = ": ".join(part for part in (name, url) if part)
        if rendered:
            methods.append(rendered)
    return "\n".join(methods)


def _chunk_text(policy_name: str, section: str, content: str) -> str:
    return f"정책명: {policy_name}\n섹션: {section}\n{content.strip()}"


async def _store_policy(policy: SourcePolicy, detail: dict[str, Any]) -> None:
    policy_name = str(detail.get("servNm") or policy.service_id).strip()
    target = str(detail.get("tgtrDtlCn") or "").strip()
    benefit = str(detail.get("alwServCn") or "").strip()
    application = _application_method(list(detail.get("applmetList") or []))
    sections = (
        ("공식 지원대상 원문", target),
        ("지원 내용", benefit),
        ("신청 방법", application),
    )
    if any(not content for _, content in sections):
        raise ValueError(f"{policy.service_id} has an empty required source field")

    raw_text = "\n\n".join(
        _chunk_text(policy_name, section, content) for section, content in sections
    )
    source_url = (
        "https://apis.data.go.kr/B554287/NationalWelfareInformationsV001/"
        f"NationalWelfaredetailedV001?servId={policy.service_id}"
    )

    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO policy (
                    policy_id, policy_code, policy_name, provider_name,
                    benefit_type, application_status, official_url, is_active
                ) VALUES (
                    :policy_id, :policy_code, :policy_name, :provider_name,
                    :benefit_type, :application_status, :official_url, TRUE
                )
                """
            ),
            {
                "policy_id": policy.policy_id,
                "policy_code": policy.service_id,
                "policy_name": policy_name,
                "provider_name": detail.get("jurMnofNm"),
                "benefit_type": detail.get("srvPvsnNm"),
                "application_status": "SOURCE_BACKED_FIXTURE",
                "official_url": source_url,
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO policy_detail (
                    policy_id, easy_summary, target_description,
                    benefit_description, application_method
                ) VALUES (
                    :policy_id, :easy_summary, :target_description,
                    :benefit_description, :application_method
                )
                """
            ),
            {
                "policy_id": policy.policy_id,
                "easy_summary": detail.get("wlfareInfoOutlCn"),
                "target_description": target,
                "benefit_description": benefit,
                "application_method": application,
            },
        )
        document_id = (
            await conn.execute(
                text(
                    """
                    INSERT INTO policy_document (
                        policy_id, source_title, source_url, source_type,
                        raw_text, ingest_status, is_current
                    ) VALUES (
                        :policy_id, :source_title, :source_url, 'POLICY_DETAIL',
                        :raw_text, 'CHUNK_READY', TRUE
                    )
                    RETURNING document_id
                    """
                ),
                {
                    "policy_id": policy.policy_id,
                    "source_title": f"{policy_name} data.go.kr 정책 상세",
                    "source_url": source_url,
                    "raw_text": raw_text,
                },
            )
        ).scalar_one()
        for index, (section, content) in enumerate(sections):
            await conn.execute(
                text(
                    """
                    INSERT INTO policy_document_chunk (
                        document_id, chunk_index, chunk_text, metadata_json
                    ) VALUES (
                        :document_id, :chunk_index, :chunk_text,
                        jsonb_build_object(
                            'section', CAST(:section AS TEXT),
                            'source_type', 'POLICY_DETAIL',
                            'policy_id', CAST(:policy_id AS TEXT),
                            'policy_code', CAST(:policy_code AS TEXT)
                        )
                    )
                    """
                ),
                {
                    "document_id": document_id,
                    "chunk_index": index,
                    "chunk_text": _chunk_text(policy_name, section, content),
                    "section": section,
                    "policy_id": policy.policy_id,
                    "policy_code": policy.service_id,
                },
            )


async def run() -> dict[str, int]:
    details = [(policy, _detail(policy.service_id)) for policy in SOURCE_POLICIES]
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)

    for policy, detail in details:
        await _store_policy(policy, detail)

    await psycopg_pool.open()
    try:
        embedding_result = await PolicyRagService().ingest_embeddings(limit=100)
    finally:
        await psycopg_pool.close()
    await engine.dispose()
    return {
        "policy_count": len(details),
        "chunk_count": len(details) * 3,
        "embedded_count": embedding_result.embedded_count,
    }


if __name__ == "__main__":
    print(asyncio.run(run()))
