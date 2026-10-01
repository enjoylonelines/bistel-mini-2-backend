import logging
from typing import Annotated, Any

from fastapi import Depends
from langchain.embeddings import init_embeddings
from langchain_postgres import PGVector

from app.common.psycopg_pool_conf import psycopg_pool
from app.core.config import settings
from app.db.session import engine
from app.repositories.policy_rag_repository import PolicyRagRepository
from app.repositories.policy_rag_repository import POLICY_RAG_METADATA_VERSION
from app.schemas.policy_rag_schema import (
    PolicyRagEmbeddingItem,
    PolicyRagEmbeddingResponse,
    PolicyRagSearchResponse,
    PolicyRagSearchResult,
)


POLICY_RAG_COLLECTION_NAME = "policy_documents"

REFERENCE_DOCUMENT_TYPE_KEYWORDS = {
    "application_form": ("신청서", "신청 양식", "지원신청서"),
    "consent_form": ("동의서",),
    "certificate": ("증명서", "확인서", "진단서"),
    "delegation_form": ("위임장",),
    "notice": ("공고", "안내"),
    "law": ("법령", "고시", "예규", "훈령", "규정"),
    "guideline": ("지침", "가이드", "매뉴얼", "업무처리"),
}

SEMANTIC_SECTION_KEYWORDS = {
    "TARGET": ("지원대상", "지원 대상", "대상자", "선정기준", "자격", "수급권자"),
    "BENEFIT": ("지원내용", "지원 내용", "급여", "지원금", "서비스 내용"),
    "APPLICATION": ("신청방법", "신청 방법", "신청기간", "신청 기간", "접수", "제출"),
    "DOCUMENT": ("구비서류", "제출서류", "필요서류", "첨부서류", "신청서", "동의서"),
    "CAUTION": ("유의사항", "주의사항", "제외", "제한", "환수", "중복"),
    "LEGAL_BASIS": ("제1조", "제2조", "시행", "법령", "고시", "예규", "훈령"),
}

EVIDENCE_ROLE_BY_SEMANTIC_SECTION = {
    "TARGET": "target",
    "BENEFIT": "benefit",
    "APPLICATION": "application",
    "DOCUMENT": "application",
    "CAUTION": "caution",
    "LEGAL_BASIS": "reference",
}

SECTION_SUBTYPE_BY_DETAIL_SECTION = {
    "신청 방법": "APPLICATION_METHOD",
    "신청 기간": "APPLICATION_PERIOD",
}


class PolicyRagService:
    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.PolicyRagService")

    async def ingest_embeddings(
        self,
        limit: int = 100,
        source_type: str | None = None,
    ) -> PolicyRagEmbeddingResponse:
        async with psycopg_pool.connection() as conn:
            targets = await PolicyRagRepository.find_embedding_targets(
                conn=conn,
                limit=limit,
                source_type=source_type,
            )

        if not targets:
            return PolicyRagEmbeddingResponse(
                requested_count=0,
                embedded_count=0,
                collection_name=POLICY_RAG_COLLECTION_NAME,
                items=[],
            )

        texts = [target["chunk_text"] for target in targets]
        metadatas = [self._build_metadata(target) for target in targets]
        ids = [str(target["chunk_id"]) for target in targets]

        vectorstore = self._vectorstore()
        await vectorstore.aadd_texts(
            texts=texts,
            metadatas=metadatas,
            ids=ids,
        )

        return PolicyRagEmbeddingResponse(
            requested_count=len(targets),
            embedded_count=len(ids),
            collection_name=POLICY_RAG_COLLECTION_NAME,
            items=[
                PolicyRagEmbeddingItem(
                    chunk_id=target["chunk_id"],
                    document_id=target["document_id"],
                    policy_id=target["policy_id"],
                    condition_profile_id=metadatas[index].get(
                        "condition_profile_id"
                    ),
                    policy_code=target["policy_code"],
                    policy_name=str(
                        metadatas[index].get("policy_name") or target["policy_name"]
                    ),
                )
                for index, target in enumerate(targets)
            ],
        )

    async def search(
        self,
        query: str,
        k: int = 5,
        source_type: str | None = None,
        policy_ids: list[int | str] | None = None,
    ) -> PolicyRagSearchResponse:
        filter_value = self._search_filter(source_type, policy_ids)
        results = await self._vectorstore().asimilarity_search_with_score(
            query=query,
            k=k,
            filter=filter_value,
        )

        search_results = [
            self._to_search_result(document=document, distance=distance)
            for document, distance in results
        ]
        return PolicyRagSearchResponse(
            query=query,
            result_count=len(search_results),
            results=search_results,
        )

    def _search_filter(
        self,
        source_type: str | None,
        policy_ids: list[int | str] | None,
    ) -> dict[str, Any] | None:
        conditions: list[dict[str, Any]] = []
        if source_type:
            conditions.append({"source_type": source_type})

        policy_id_values: list[int] = []
        policy_codes: list[str] = []
        for policy_id in policy_ids or []:
            if isinstance(policy_id, int):
                policy_id_values.append(policy_id)
                continue

            policy_key = str(policy_id).strip()
            if not policy_key:
                continue
            if policy_key.isdecimal():
                policy_id_values.append(int(policy_key))
            else:
                policy_codes.append(policy_key)

        policy_conditions: list[dict[str, Any]] = []
        if policy_id_values:
            policy_conditions.append(
                {"policy_id": {"$in": list(dict.fromkeys(policy_id_values))}}
            )
        if policy_codes:
            policy_conditions.append(
                {"policy_code": {"$in": list(dict.fromkeys(policy_codes))}}
            )
        if len(policy_conditions) == 1:
            conditions.append(policy_conditions[0])
        elif policy_conditions:
            conditions.append({"$or": policy_conditions})

        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    def _vectorstore(self) -> PGVector:
        embedding_kwargs = {}
        if settings.openai_api_key:
            embedding_kwargs["api_key"] = settings.openai_api_key

        return PGVector(
            embeddings=init_embeddings(
                model="openai:text-embedding-3-large",
                **embedding_kwargs,
            ),
            collection_name=POLICY_RAG_COLLECTION_NAME,
            connection=engine,
            async_mode=True,
            use_jsonb=True,
        )

    def _build_metadata(self, target: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(target.get("metadata_json") or {})
        source_type = target["source_type"]
        source_title = target["source_title"]
        chunk_text = str(target.get("chunk_text") or "")
        semantic_section = self._semantic_section(
            source_type=source_type,
            section=metadata.get("section"),
            source_title=source_title,
            chunk_text=chunk_text,
        )
        evidence_role = (
            metadata.get("evidence_role")
            or EVIDENCE_ROLE_BY_SEMANTIC_SECTION.get(semantic_section)
        )
        section_subtype = (
            metadata.get("section_subtype")
            or SECTION_SUBTYPE_BY_DETAIL_SECTION.get(
                str(metadata.get("section") or "")
            )
        )
        metadata.update(
            {
                "metadata_version": POLICY_RAG_METADATA_VERSION,
                "chunk_id": target["chunk_id"],
                "document_id": target["document_id"],
                "chunk_index": target["chunk_index"],
                "policy_id": target["policy_id"],
                "policy_code": target["policy_code"],
                "policy_name": metadata.get("policy_name") or target["policy_name"],
                "main_category": target.get("main_category"),
                "sub_category": target.get("sub_category"),
                "provider_name": target.get("provider_name"),
                "provider_type": target.get("provider_type"),
                "region_scope": target.get("region_scope"),
                "region_code": target.get("region_code"),
                "benefit_type": target.get("benefit_type"),
                "application_status": target.get("application_status"),
                "application_start_date": self._to_str(
                    target.get("application_start_date")
                ),
                "application_end_date": self._to_str(
                    target.get("application_end_date")
                ),
                "source_title": source_title,
                "source_url": target["source_url"],
                "source_type": source_type,
                "semantic_section": semantic_section,
                "evidence_role": evidence_role,
                "section_subtype": section_subtype,
                "reference_document_type": self._reference_document_type(
                    source_type=source_type,
                    source_title=source_title,
                    chunk_text=chunk_text,
                ),
                "chunk_hash": target["chunk_hash"],
            }
        )
        return {key: value for key, value in metadata.items() if value is not None}

    def _to_search_result(self, document, distance: float) -> PolicyRagSearchResult:
        metadata = document.metadata or {}
        return PolicyRagSearchResult(
            chunk_id=self._to_int(metadata.get("chunk_id")),
            document_id=self._to_int(metadata.get("document_id")),
            policy_id=self._to_int(metadata.get("policy_id")),
            condition_profile_id=self._to_int(metadata.get("condition_profile_id")),
            policy_code=self._to_str(metadata.get("policy_code")),
            policy_name=self._to_str(metadata.get("policy_name")),
            section=self._to_str(metadata.get("section")),
            semantic_section=self._to_str(metadata.get("semantic_section")),
            section_subtype=self._to_str(
                metadata.get("section_subtype")
                or SECTION_SUBTYPE_BY_DETAIL_SECTION.get(
                    str(metadata.get("section") or "")
                )
            ),
            source_type=self._to_str(metadata.get("source_type")),
            source_title=self._to_str(metadata.get("source_title")),
            source_url=self._to_str(metadata.get("source_url")),
            evidence_role=self._to_str(metadata.get("evidence_role")),
            metadata=metadata,
            chunk_text=document.page_content,
            distance=float(distance),
        )

    def _semantic_section(
        self,
        source_type: str | None,
        section: Any,
        source_title: str | None,
        chunk_text: str,
    ) -> str | None:
        section_value = self._to_str(section)
        if source_type == "POLICY_DETAIL":
            return self._detail_semantic_section(section_value)

        text = f"{source_title or ''}\n{chunk_text}"
        for semantic_section, keywords in SEMANTIC_SECTION_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return semantic_section
        return "REFERENCE"

    def _detail_semantic_section(self, section: str | None) -> str | None:
        section_map = {
            "기본 정보": "SUMMARY",
            "요약": "SUMMARY",
            "정리된 지원 조건": "TARGET",
            "조건 구조": "TARGET",
            "지원 대상": "TARGET",
            "공식 지원대상 원문": "TARGET",
            "지원 내용": "BENEFIT",
            "신청 방법": "APPLICATION",
            "신청 기간": "APPLICATION",
            "제외 조건": "CAUTION",
            "추가 확인 조건": "CAUTION",
            "유의 사항": "CAUTION",
        }
        return section_map.get(section)

    def _reference_document_type(
        self,
        source_type: str | None,
        source_title: str | None,
        chunk_text: str,
    ) -> str | None:
        if source_type != "POLICY_REFERENCE":
            return None

        text = f"{source_title or ''}\n{chunk_text}"
        for document_type, keywords in REFERENCE_DOCUMENT_TYPE_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return document_type
        return "policy_reference"

    def _to_int(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _to_str(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)


PolicyRagServiceDep = Annotated[
    PolicyRagService,
    Depends(PolicyRagService),
]
