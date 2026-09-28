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

from sentinelai.modules.investigation.exceptions import FindingAlreadyReviewedError
from sentinelai.platform.db.base import Base
from sentinelai.shared.exceptions import ValidationFailedError

_SCHEMA = "investigation"


# The AI-finding review machine (database-design.md §3.5, PRD FR-7.3, ADR-0011 §1).
# `proposed` is where AI output lands; only an explicit analyst action moves it, and both
# dispositions are terminal — a confirmed finding is not re-openable by a second review, because the
# audit trail of "who decided what" is the point of the human-in-the-loop guarantee.
STATUS_PROPOSED = "proposed"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
REVIEW_DISPOSITIONS: frozenset[str] = frozenset({STATUS_CONFIRMED, STATUS_REJECTED})


class _Reviewable:
    """The review invariant, shared by the two finding kinds — ADR-0011 §1.

    ``Entity`` and ``Relationship`` are separate aggregates with separate tables and separate
    revision ledgers, but the *rule* is one rule: a finding is reviewed exactly once, from
    ``proposed``, to a disposition that is either ``confirmed`` or ``rejected``. Writing it twice
    would be two chances for the two paths to drift on a guarantee PRD FR-7.3 makes explicitly.

    A mixin rather than a base class because both already inherit ``Base``, and because this carries
    behaviour only — no table, no columns, no mapped state of its own.
    """

    status: Mapped[str]

    def review(self, disposition: str) -> str:
        """Record an analyst's disposition; returns the previous status.

        Raises ``ValidationFailedError`` for a disposition outside the vocabulary (422) and
        ``FindingAlreadyReviewedError`` for a second review of an already-decided finding (409).

        The order matters: the vocabulary check comes first, so a caller sending nonsense gets "that
        is not a disposition" rather than "already reviewed", which would be a confusing answer to a
        request that was malformed regardless of state.

        ETag/concurrency is deliberately NOT checked here. It is an HTTP-level concern with no
        domain meaning — the aggregate's job is that the *transition* is legal, not
        that the caller held a fresh representation.
        """
        if disposition not in REVIEW_DISPOSITIONS:
            raise ValidationFailedError(
                [
                    {
                        "field": "status",
                        "message": f"disposition must be one of {sorted(REVIEW_DISPOSITIONS)}",
                    }
                ]
            )
        if self.status != STATUS_PROPOSED:
            raise FindingAlreadyReviewedError(f"finding is already {self.status}")
        previous = self.status
        self.status = disposition
        return previous

    @classmethod
    def assert_supporting_evidence(cls, count: int, *, field: str = "evidence_ids") -> None:
        """CEM §13: a finding must not **exist** without ≥1 supporting evidence reference.

        A creation-time rule, and the placement is the point. CEM §1.6 says "No Entity or
        Relationship may exist without at least one supporting evidence reference", and §13's
        validation table says *Reject* — an existence invariant, not a review gate. Checking it at
        confirmation instead would be both too late (the unsupported row already exists) and
        wrong: refusing to let an analyst *reject* an unsupported finding is perverse, and rejection
        is exactly what should happen to one.

        A classmethod because it is asked before the instance exists, and the count is passed in
        because the supporting rows are written in the same transaction as the finding — there is
        nothing to query yet.

        CEM §13 grants entities an explicit exception (an analyst-pre-registered entity needs no
        MENTIONS edge), so ``Entity`` creation does not call this; only ``Relationship`` does.
        """
        if count < 1:
            raise ValidationFailedError(
                [{"field": field, "message": "a relationship requires ≥1 supporting evidence"}]
            )


class Entity(Base, _Reviewable):
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


class Relationship(Base, _Reviewable):
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
