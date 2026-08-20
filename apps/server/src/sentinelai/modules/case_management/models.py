"""case_management ORM models — schema ``case_management`` (database-design.md §3.4).

Intra-schema foreign keys are real (``case_id`` → ``cases``); every cross-module
reference (``owning_user_id`` → ``platform.users``, ``evidence_id`` →
``ingestion.evidence``) is a plain UUID app-ref with NO ForeignKey (§5).
``case_status_history`` is append-only.

``Case`` is a rich aggregate (ADR-0011 §1): it owns the ``open → closed → archived``
machine and refuses invalid transitions itself — the service orchestrates (history
row, outbox event, audit) but cannot put a case into an illegal state.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.modules.case_management.exceptions import InvalidCaseStatusTransitionError
from sentinelai.platform.db.base import Base
from sentinelai.shared.exceptions import ValidationFailedError

_SCHEMA = "case_management"

# The case lifecycle (api-design.md §5, ADR-0011 §1). archived is terminal.
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_ARCHIVED = "archived"
VALID_STATUSES = frozenset({STATUS_OPEN, STATUS_CLOSED, STATUS_ARCHIVED})
TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_OPEN: frozenset({STATUS_CLOSED, STATUS_ARCHIVED}),
    STATUS_CLOSED: frozenset({STATUS_OPEN, STATUS_ARCHIVED}),
    STATUS_ARCHIVED: frozenset(),
}

# Report job states — api-design.md §7's documented polling vocabulary for
# ``GET /api/v1/reports/{report_id}``. Not a case status; a job-state-row status.
REPORT_QUEUED = "queued"
REPORT_RUNNING = "running"
REPORT_COMPLETED = "completed"
REPORT_FAILED = "failed"
REPORT_STATUSES = frozenset({REPORT_QUEUED, REPORT_RUNNING, REPORT_COMPLETED, REPORT_FAILED})


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

    # -- aggregate behaviour (ADR-0011 §1): the machine lives HERE, not in the service --

    def transition_to(self, new_status: str, *, at: datetime) -> str:
        """Move the case to ``new_status``, enforcing the machine; returns the previous status.

        Raises ``ValidationFailedError`` for a status outside the vocabulary and
        ``InvalidCaseStatusTransitionError`` for a known-but-illegal move (409, per
        api-design.md §2.4). ``closed_at`` is maintained as part of the same invariant —
        it is set exactly while the case is closed.
        """
        if new_status not in VALID_STATUSES:
            raise ValidationFailedError(
                [{"field": "new_status", "message": f"unknown status '{new_status}'"}]
            )
        if new_status not in TRANSITIONS[self.status]:
            raise InvalidCaseStatusTransitionError(
                f"cannot transition case from '{self.status}' to '{new_status}'"
            )
        previous = self.status
        self.status = new_status
        if new_status == STATUS_CLOSED:
            self.closed_at = at
        elif new_status == STATUS_OPEN:
            self.closed_at = None
        return previous

    def close(self, *, at: datetime) -> str:
        """Close an open case."""
        return self.transition_to(STATUS_CLOSED, at=at)

    def reopen(self, *, at: datetime) -> str:
        """Reopen a closed case (clears ``closed_at``)."""
        return self.transition_to(STATUS_OPEN, at=at)

    def archive(self, *, at: datetime) -> str:
        """Archive the case — terminal; no transition leaves ``archived``."""
        return self.transition_to(STATUS_ARCHIVED, at=at)


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
    # NULL until the job finishes: the row is created `queued` at request time (api-design.md §7)
    # so the client has something to poll, and only a completed report has an object to point at.
    storage_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_by_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # api-design.md §7's polling vocabulary: queued | running | completed | failed.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=REPORT_QUEUED)
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
