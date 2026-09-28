"""Audit-log query persistence — api-design.md §10, database-design.md §10.

Read-only by construction. The audit log is append-only at the database level (ADR-0004) and has no
API-level erasure path at all (api-design.md §10: "no `DELETE` anywhere in this endpoint group"), so
this repository exposes one method and no way to write.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.models import AuditLog


@dataclass(frozen=True, slots=True)
class AuditLogFilters:
    """The query parameters api-design.md §10 documents. Every one optional."""

    actor_user_id: UUID | None = None
    action: str | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None


class AuditLogRepository:
    """Reads ``platform.audit_log``. There is deliberately no write path here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _apply(
        self, stmt: Select[tuple[AuditLog]], filters: AuditLogFilters
    ) -> Select[tuple[AuditLog]]:
        """Add one WHERE clause per supplied filter, and none for the others.

        Every comparison is a bound parameter — the values are reviewer-supplied query strings, and
        security-architecture.md §27 makes parameterized queries unconditional.
        """
        if filters.actor_user_id is not None:
            stmt = stmt.where(AuditLog.actor_user_id == filters.actor_user_id)
        if filters.action is not None:
            stmt = stmt.where(AuditLog.action == filters.action)
        if filters.target_type is not None:
            stmt = stmt.where(AuditLog.target_type == filters.target_type)
        if filters.target_id is not None:
            stmt = stmt.where(AuditLog.target_id == filters.target_id)
        if filters.occurred_after is not None:
            # Exclusive, so paging by `occurred_after = <last seen>` cannot re-read the boundary
            # row — the same half-open convention the cursor uses.
            stmt = stmt.where(AuditLog.occurred_at > filters.occurred_after)
        if filters.occurred_before is not None:
            stmt = stmt.where(AuditLog.occurred_at < filters.occurred_before)
        return stmt

    async def page(
        self,
        filters: AuditLogFilters,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> Sequence[AuditLog]:
        """Return up to ``limit`` entries in chain order, starting after ``after``.

        **Ordered by ``(occurred_at, audit_id)``, ascending.** Ascending because an export is read
        as a chain and a reviewer verifying `entry_hash` links needs them in the order they were
        written, not newest-first; the tuple because `occurred_at` is not unique — two entries
        written in the same transaction can share a timestamp, and ordering by it alone would let
        the page boundary drop or duplicate one of them.

        Keyset pagination rather than OFFSET: the audit log only grows, and an OFFSET scan deep into
        it gets slower the more history there is to export — exactly the wrong scaling for the
        endpoint an oversight body uses.
        """
        stmt = self._apply(select(AuditLog), filters)
        if after is not None:
            occurred_at, audit_id = after
            # The row-value comparison is what makes the composite key work as one cursor: it means
            # "strictly after this (timestamp, id) pair" rather than two independent conditions.
            stmt = stmt.where(
                (AuditLog.occurred_at, AuditLog.audit_id) > (occurred_at, audit_id)  # type: ignore[operator]
            )
        stmt = stmt.order_by(AuditLog.occurred_at.asc(), AuditLog.audit_id.asc()).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()


__all__ = ["AuditLogFilters", "AuditLogRepository"]
