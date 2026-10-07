from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


load_dotenv(override=True)

from app.api.apply_controller import (
    checklist_router as apply_checklist_router,
    router as apply_router,
)
from app.api.ai_request_controller import eligibility_router, recommendation_router
from app.api.auth_controller import auth_router, users_router
from app.api.chat_controller import router as chat_router
from app.api.compare_controller import (
    history_router as compare_history_router,
    public_router as compare_public_router,
    router as compare_router,
)
from app.api.family_profile_controller import router as family_profile_router
from app.api.favorite_controller import (
    favorites_router,
    user_favorites_router,
)
from app.api.policy_controller import router as policy_router
from app.api.policy_condition_profile_controller import (
    router as policy_condition_profile_router,
)
from app.api.policy_rule_ingest_controller import (
    router as policy_rule_ingest_router,
)
from app.api.policy_data_controller import router as policy_data_router
from app.api.policy_document_controller import router as policy_document_router
from app.api.policy_import_controller import router as policy_import_router
from app.api.policy_judgement_controller import router as policy_judgement_router
from app.api.policy_rag_controller import router as policy_rag_router
from app.common.exceptions import register_exception_handlers
from app.common.psycopg_pool_conf import psycopg_pool
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine, vector_engine
from app.repositories.chat_request_repository import ChatRequestRepository
from app.repositories.policy_repository import PolicyRepository
from app.utils.logger import setup_logging


setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await psycopg_pool.open()
    async with psycopg_pool.connection() as conn:
        await PolicyRepository.ensure_search_indexes(conn)
    async with AsyncSessionLocal() as db:
        await ChatRequestRepository.mark_stale_processing_failed(db)
        await db.commit()
    try:
        yield
    finally:
        await psycopg_pool.close()
        await engine.dispose()
        await vector_engine.dispose()


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(family_profile_router)
app.include_router(favorites_router)
app.include_router(user_favorites_router)
app.include_router(policy_router)
app.include_router(policy_condition_profile_router)
app.include_router(policy_rule_ingest_router)
app.include_router(policy_data_router)
app.include_router(policy_import_router)
app.include_router(policy_document_router)
app.include_router(policy_rag_router)
app.include_router(policy_judgement_router)
app.include_router(recommendation_router)
app.include_router(eligibility_router)
app.include_router(apply_router)
app.include_router(apply_checklist_router)
app.include_router(compare_router)
app.include_router(compare_public_router)
app.include_router(compare_history_router)
app.include_router(chat_router)
register_exception_handlers(app)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="localhost",
        port=8000,
        reload=True,
        access_log=True,
    )
