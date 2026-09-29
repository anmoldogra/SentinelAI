"""forensics persistence + Unit of Work (guide Part 3). Persistence only."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.forensics.models import Artifact
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter
from sentinelai.platform.events.signing import EventSigner

_SCHEMA = "forensics"


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, artifact_id: UUID) -> Artifact | None:
        result = await self._session.execute(
            select(Artifact).where(Artifact.artifact_id == artifact_id)
        )
        return result.scalar_one_or_none()

    async def add(self, artifact: Artifact) -> None:
        self._session.add(artifact)
        await self._session.flush()

    def _page(self, *, limit: int, after: tuple[datetime, UUID] | None) -> Select[tuple[Artifact]]:
        stmt = select(Artifact)
        if after is not None:
            collected_at, artifact_id = after
            # Row-value comparison, so the composite key acts as one cursor rather than two
            # independent conditions — "strictly after this (timestamp, id) pair".
            stmt = stmt.where(
                tuple_(Artifact.collected_at, Artifact.artifact_id) > (collected_at, artifact_id)
            )
        return stmt.order_by(Artifact.collected_at.asc(), Artifact.artifact_id.asc()).limit(limit)

    async def list_(
        self, *, limit: int, after: tuple[datetime, UUID] | None = None
    ) -> Sequence[Artifact]:
        """One page of artifacts, oldest first.

        Ascending for the reason `osint` gives for findings: an examiner works a backlog forward,
        and with newest-first every poll changes what "page 2" holds. ``collected_at`` is the
        acquisition time an examiner recognises, with ``artifact_id`` breaking ties — two artifacts
        pulled from one device in the same acquisition share a timestamp, so the id is not
        decoration.
        """
        result = await self._session.execute(self._page(limit=limit, after=after))
        return result.scalars().all()


class ForensicsUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession, *, kms: KeyManagementService | None = None) -> None:
        super().__init__(session)
        self.artifacts = ArtifactRepository(session)
        self.outbox = OutboxWriter(
            session,
            schema=_SCHEMA,
            # ADR-0007 §1: events this module publishes are signed under EVENT_ROOT. `None`
            # writes them unsigned, which the dispatcher's strict mode then refuses -- so an
            # unwired publisher fails loudly at consume time rather than silently here.
            signer=EventSigner(kms) if kms is not None else None,
        )


async def get_forensics_uow(
    session: AsyncSession = Depends(get_session),
    kms: KeyManagementService = Depends(get_kms),
) -> ForensicsUnitOfWork:
    return ForensicsUnitOfWork(session, kms=kms)
