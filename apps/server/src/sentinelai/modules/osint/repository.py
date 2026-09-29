"""osint persistence + Unit of Work (guide Part 3). Persistence only.

No business rules here: whether a finding may be published, and what it maps to, is
``service.py``'s decision. This layer answers "store this" and "give me that".

Findings have no timestamp-ordered surrogate beyond ``collected_at``, which a connector supplies and
can therefore repeat or backdate. Keyset pagination orders by ``(collected_at, finding_id)`` — the
composite is what makes the page boundary stable when two findings from one poll share a timestamp,
which is the normal case for a batch pull rather than an edge case.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.osint.models import OsintConnectorState, OsintFinding, OsintSource
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter
from sentinelai.platform.events.signing import EventSigner

_SCHEMA = "osint"


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, source_id: UUID) -> OsintSource | None:
        result = await self._session.execute(
            select(OsintSource).where(OsintSource.source_id == source_id)
        )
        return result.scalar_one_or_none()

    async def add(self, source: OsintSource) -> None:
        self._session.add(source)
        await self._session.flush()

    async def list_(self) -> Sequence[OsintSource]:
        """Every configured source, active or not.

        Unpaginated, matching the route (`api-design.md` §4.3 gives `GET /osint/sources` no
        pagination): sources are operator-configured connector definitions, a list bounded by how
        many feeds an agency subscribes to rather than by data volume. Ordered by name so the
        console's list does not reshuffle between reads.
        """
        result = await self._session.execute(select(OsintSource).order_by(OsintSource.name.asc()))
        return result.scalars().all()


class FindingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, finding_id: UUID) -> OsintFinding | None:
        result = await self._session.execute(
            select(OsintFinding).where(OsintFinding.finding_id == finding_id)
        )
        return result.scalar_one_or_none()

    async def add(self, finding: OsintFinding) -> None:
        self._session.add(finding)
        await self._session.flush()

    def _page(
        self, *, limit: int, after: tuple[datetime, UUID] | None
    ) -> Select[tuple[OsintFinding]]:
        stmt = select(OsintFinding)
        if after is not None:
            collected_at, finding_id = after
            # Row-value comparison, so the composite key acts as one cursor rather than two
            # independent conditions — "strictly after this (timestamp, id) pair".
            stmt = stmt.where(
                tuple_(OsintFinding.collected_at, OsintFinding.finding_id)
                > (collected_at, finding_id)
            )
        return stmt.order_by(OsintFinding.collected_at.asc(), OsintFinding.finding_id.asc()).limit(
            limit
        )

    async def list_(
        self, *, limit: int, after: tuple[datetime, UUID] | None = None
    ) -> Sequence[OsintFinding]:
        """One page of findings, oldest first.

        Ascending because a reviewer works a backlog forward and because the cursor's meaning has to
        stay stable as new findings arrive: with newest-first, every poll shifts what "page 2"
        holds.
        """
        result = await self._session.execute(self._page(limit=limit, after=after))
        return result.scalars().all()


class ConnectorStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_source(self, source_id: UUID) -> OsintConnectorState | None:
        result = await self._session.execute(
            select(OsintConnectorState).where(OsintConnectorState.source_id == source_id)
        )
        return result.scalar_one_or_none()


class OsintUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession, *, kms: KeyManagementService | None = None) -> None:
        super().__init__(session)
        self.sources = SourceRepository(session)
        self.findings = FindingRepository(session)
        self.connector_state = ConnectorStateRepository(session)
        self.outbox = OutboxWriter(
            session,
            schema=_SCHEMA,
            # ADR-0007 §1: events this module publishes are signed under EVENT_ROOT. `None`
            # writes them unsigned, which the dispatcher's strict mode then refuses -- so an
            # unwired publisher fails loudly at consume time rather than silently here.
            signer=EventSigner(kms) if kms is not None else None,
        )


async def get_osint_uow(
    session: AsyncSession = Depends(get_session),
    kms: KeyManagementService = Depends(get_kms),
) -> OsintUnitOfWork:
    return OsintUnitOfWork(session, kms=kms)
