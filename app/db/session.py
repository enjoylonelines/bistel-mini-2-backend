from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def vector_database_url(database_url: str) -> str:
    """Use psycopg for PGVector's extension initialization.

    langchain-postgres executes an advisory lock and ``CREATE EXTENSION`` in
    one statement. asyncpg rejects that multi-statement initialization while
    psycopg accepts it. Keep the application's normal asyncpg ORM engine
    unchanged and isolate the compatibility choice to the vector store.
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
)

vector_engine = create_async_engine(
    vector_database_url(settings.database_url),
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(AsyncAttrs, DeclarativeBase):
    pass


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
