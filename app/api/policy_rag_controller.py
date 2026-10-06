import logging
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.common.psycopg_pool_conf import psycopg_pool
from app.common.response import success_response
from app.repositories.policy_rag_repository import PolicyRagRepository
from app.services.policy_rag_service import PolicyRagServiceDep


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/policies/rag", tags=["Policy RAG"])


@router.post("/embeddings/ingest")
async def ingest_policy_rag_embeddings(
    service: PolicyRagServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    source_type: str | None = None,
) -> JSONResponse:
    logger.info("정책 RAG embedding 저장 시작")
    result = await service.ingest_embeddings(limit=limit, source_type=source_type)
    async with psycopg_pool.connection() as conn:
        async with conn.transaction():
            await PolicyRagRepository.mark_documents_embedded(
                conn,
                list({item.document_id for item in result.items}),
            )
    return success_response(data=result)


@router.get("/search")
async def search_policy_rag(
    service: PolicyRagServiceDep,
    query: Annotated[str, Query(min_length=1)],
    k: Annotated[int, Query(ge=1, le=20)] = 5,
    source_type: str | None = None,
) -> JSONResponse:
    result = await service.search(query=query, k=k, source_type=source_type)
    return success_response(data=result)
