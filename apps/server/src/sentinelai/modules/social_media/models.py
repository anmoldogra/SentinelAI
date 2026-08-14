"""social_media ORM models — schema ``social_media`` (database-design.md §3.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

_SCHEMA = "social_media"


class CapturedContent(Base):
    __tablename__ = "captured_content"
    __table_args__ = ({"schema": _SCHEMA},)

    content_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    evidence_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)  # app-ref
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    account_handle: Mapped[str] = mapped_column(Text, nullable=False)
    content_kind: Mapped[str] = mapped_column(Text, nullable=False)
    raw_attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SocialAccountObserved(Base):
    __tablename__ = "social_accounts_observed"
    __table_args__ = ({"schema": _SCHEMA},)

    account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    handle: Mapped[str] = mapped_column(Text, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
