from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class PolicyDocument(Base):
    __tablename__ = "policy_document"

    document_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    policy_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("policy.policy_id", ondelete="CASCADE"),
        nullable=False,
    )
    condition_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("policy_condition_profile.condition_profile_id", ondelete="SET NULL"),
        nullable=True,
    )
    source_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingest_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="PENDING_TEXT"
    )
    embedded_metadata_version: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
