"""investigation ORM models — schema ``investigation`` (database-design.md §3.5).

The entity/relationship knowledge graph, their append-only revision ledgers, the
mandatory relationship↔evidence and entity↔evidence link tables (every relationship
must have ≥1 supporting evidence row, CEM §13), and the correlation-run job records.
``evidence_id``/``case_id`` are app-refs (no FK, §5).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, Boolean, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

_SCHEMA = "investigation"


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = ({"schema": _SCHEMA},)

    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    confidence: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)


class EntityRevision(Base):
    __tablename__ = "entity_revisions"
    __table_args__ = ({"schema": _SCHEMA},)

    revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.entities.entity_id"), nullable=False
    )
    field_changed: Mapped[str] = mapped_column(Text, nullable=False)
    previous_value: Mapped[str] = mapped_column(Text, nullable=False)
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class Relationship(Base):
    __tablename__ = "relationships"
    __table_args__ = ({"schema": _SCHEMA},)

    relationship_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    from_entity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.entities.entity_id"), nullable=False
    )
    to_entity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.entities.entity_id"), nullable=False
    )
    directional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    created_by_type: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)


class RelationshipRevision(Base):
    __tablename__ = "relationship_revisions"
    __table_args__ = ({"schema": _SCHEMA},)

    revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    relationship_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.relationships.relationship_id"), nullable=False
    )
    previous_status: Mapped[str] = mapped_column(String(20), nullable=False)
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)


class RelationshipEvidence(Base):
    __tablename__ = "relationship_evidence"
    __table_args__ = ({"schema": _SCHEMA},)

    relationship_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{_SCHEMA}.relationships.relationship_id"),
        primary_key=True,
    )
    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)  # app-ref


class EntityEvidenceMention(Base):
    __tablename__ = "entity_evidence_mentions"
    __table_args__ = ({"schema": _SCHEMA},)

    mention_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.entities.entity_id"), nullable=False
    )
    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)  # app-ref


class CorrelationRun(Base):
    __tablename__ = "correlation_runs"
    __table_args__ = ({"schema": _SCHEMA},)

    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)  # app-ref
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    findings_generated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Cooperative cancellation checkpoint (guide Part 12 "Cancellation").
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
