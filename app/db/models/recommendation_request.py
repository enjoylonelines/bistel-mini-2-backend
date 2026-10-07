from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.schemas.ai_contract import RequestStatus


class RecommendationRequest(Base):
    __tablename__ = "recommendation_request"

    request_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="FORM",
        server_default="FORM",
    )
    source_ref_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_query_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    merged_condition_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    profile_conflict_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A short-lived runner ownership fence.  It is deliberately not a queue or
    # a cross-process concurrency limit: it only prevents a stale runner from
    # persisting a later result for the same request.
    execution_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    request_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=RequestStatus.READY.value,
        server_default=RequestStatus.READY.value,
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
