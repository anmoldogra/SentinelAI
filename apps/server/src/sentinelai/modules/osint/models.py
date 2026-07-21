"""osint ORM models — schema ``osint`` (database-design.md §3.3 domain-producer).

Rich record table (``osint_findings``) + supporting config/state tables.
``evidence_id`` is a nullable app-ref to ``ingestion.evidence`` (a finding exists
pre-normalization; it is set on publish).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

_SCHEMA = "osint"


class OsintSource(Base):
    __tablename__ = "osint_sources"
    __table_args__ = ({"schema": _SCHEMA},)

    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    connector_type: Mapped[str] = mapped_column(Text, nullable=False)
    reliability_baseline: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class OsintFinding(Base):
    __tablename__ = "osint_findings"
    __table_args__ = ({"schema": _SCHEMA},)

    finding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.osint_sources.source_id"), nullable=False
    )
    evidence_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)  # app-ref
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    raw_attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reliability_rating: Mapped[str | None] = mapped_column(Text, nullable=True)


class OsintConnectorState(Base):
    __tablename__ = "osint_connector_state"
    __table_args__ = ({"schema": _SCHEMA},)

    state_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.osint_sources.source_id"), nullable=False
    )
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
