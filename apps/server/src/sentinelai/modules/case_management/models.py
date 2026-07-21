"""case_management ORM models — schema ``case_management`` (database-design.md §3.4).

Intra-schema foreign keys are real (``case_id`` → ``cases``); every cross-module
reference (``owning_user_id`` → ``platform.users``, ``evidence_id`` →
``ingestion.evidence``) is a plain UUID app-ref with NO ForeignKey (§5).
``case_status_history`` is append-only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

_SCHEMA = "case_management"


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = ({"schema": _SCHEMA},)

    case_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    owning_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)  # app-ref
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class CaseEvidenceLink(Base):
    __tablename__ = "case_evidence_links"
    __table_args__ = ({"schema": _SCHEMA},)

    link_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.cases.case_id"), nullable=False
    )
    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)  # app-ref
    linked_by_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class CaseStatusHistory(Base):
    __tablename__ = "case_status_history"
    __table_args__ = ({"schema": _SCHEMA},)

    history_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.cases.case_id"), nullable=False
    )
    previous_status: Mapped[str] = mapped_column(String(50), nullable=False)
    new_status: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)  # app-ref
    changed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class CaseReport(Base):
    __tablename__ = "case_reports"
    __table_args__ = ({"schema": _SCHEMA},)

    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.cases.case_id"), nullable=False
    )
    report_type: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
