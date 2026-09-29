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

from sqlalchemy import ARRAY, Boolean, ForeignKey, Index, Integer, Numeric, String, Text
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

# The correlation-run state machine (api-design.md §6's `GET /correlation-runs/{run_id}` response,
# engineering-roadmap.md's "`queued` -> `running` -> `completed`/`failed`"). A **closed** four-value
# vocabulary: §6 publishes it as the enum a client switches on, so a fifth value would be a breaking
# API change, not an implementation detail.
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_STATUSES: frozenset[str] = frozenset({RUN_QUEUED, RUN_RUNNING, RUN_COMPLETED, RUN_FAILED})
# What "a run is already in progress for this case" means for §6's documented 409 on the trigger
# endpoint. `queued` counts: the row exists and a worker will pick it up, so a second trigger would
# put two runs over one case on the queue and double every finding they both propose.
RUN_IN_PROGRESS: frozenset[str] = frozenset({RUN_QUEUED, RUN_RUNNING})


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
    """CEM §11's MENTIONS edge: this evidence object references this entity.

    Mirrors `202609290012_investig_mention`. The unique index is declared here too so `create_all`
    in tests reproduces the real constraint — a suite whose schema lacked it would pass while
    production depended on it. `(entity_id, evidence_id)` is set membership, not a countable
    occurrence: §3.5 gives the table no offset, span or count column that could distinguish two rows
    for one pair.
    """

    __tablename__ = "entity_evidence_mentions"
    __table_args__ = (
        Index("uq_entity_mention_pair", "entity_id", "evidence_id", unique=True),
        {"schema": _SCHEMA},
    )

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
    # `queued`, not `pending`: api-design.md §6 publishes the trigger response as
    # `{ run_id, status: "queued" }` and the poll response's enum as
    # `queued|running|completed|failed`. A value outside that set is a contract violation a
    # client cannot switch on.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=RUN_QUEUED)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    findings_generated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Cooperative cancellation checkpoint (guide Part 12 "Cancellation").
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- aggregate behaviour (ADR-0011 §1): the run's state machine lives HERE ------------------
    #
    # The row IS the job's state (guide Part 12), so the legal transitions belong to the row rather
    # than to the job wrapper that drives them. Keeping them here is what makes the machine testable
    # with no database, no queue and no worker — a declarative instance is an ordinary Python object
    # until it meets a session — and it means the *second* driver of a run (a replay, an operator's
    # manual re-enqueue) cannot invent a transition the first one could not.

    @property
    def is_finished(self) -> bool:
        return self.status in (RUN_COMPLETED, RUN_FAILED)

    def claim(self, now: datetime) -> bool:
        """Move the run to ``running``; ``False`` if there is nothing left to do.

        ``False`` for an already-``completed`` run rather than an exception, because that is what a
        **redelivered job** looks like. arq retries on failure and re-runs on a worker restart, so a
        job function must be safe to invoke twice — and the second invocation of a finished run is
        ordinary, not a violation. Returning quietly is what stops it re-publishing
        ``correlation_run_completed`` and re-walking the case's evidence.

        A ``failed`` run *is* claimable: a failure is usually transient (the database went away
        mid-run), arq's next attempt is the retry, and a run that could never be retried would turn
        every blip into a case an analyst has to re-trigger by hand.

        ``started_at`` is set once and never moved. It is the first attempt's clock, which is what
        "how long has this run been going" means to somebody reading `GET /correlation-runs/{id}`;
        overwriting it on a retry would make a run that has been failing for an hour look fresh.
        """
        if self.status == RUN_COMPLETED:
            return False
        self.status = RUN_RUNNING
        if self.started_at is None:
            self.started_at = now
        return True

    def record_progress(self, findings_generated: int) -> None:
        """Publish interim progress on the row (guide Part 12 "Retries & Progress").

        Called at each batch boundary so `GET /correlation-runs/{run_id}` reflects real interim
        state rather than a stale ``running`` with a zero count. Monotonic by construction — the
        caller accumulates — and asserted, because a count that went backwards would be read as
        findings having been withdrawn, which never happens: a finding is rejected by review, never
        deleted.
        """
        if findings_generated < self.findings_generated_count:
            raise ValidationFailedError(
                [
                    {
                        "field": "findings_generated_count",
                        "message": "a run's finding count never decreases",
                    }
                ]
            )
        self.findings_generated_count = findings_generated

    def finish(self, now: datetime, *, failed: bool) -> None:
        """Stamp the terminal status and the completion clock.

        One method for both outcomes because they differ in exactly one field, and two would be two
        places to forget ``completed_at`` — the field a poller uses to tell "still going" from
        "over", which matters most on the failure path where nothing else will tell it.
        """
        self.status = RUN_FAILED if failed else RUN_COMPLETED
        self.completed_at = now
