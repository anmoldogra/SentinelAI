"""osint persistence + Unit of Work (guide Part 3). Persistence only."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends
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
        raise NotImplementedError

    async def add(self, source: OsintSource) -> None:
        raise NotImplementedError

    async def list_(self) -> Sequence[OsintSource]:
        raise NotImplementedError


class FindingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, finding_id: UUID) -> OsintFinding | None:
        raise NotImplementedError

    async def add(self, finding: OsintFinding) -> None:
        raise NotImplementedError

    async def list_(self, *, limit: int, cursor: str | None) -> Sequence[OsintFinding]:
        raise NotImplementedError


class ConnectorStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_source(self, source_id: UUID) -> OsintConnectorState | None:
        raise NotImplementedError


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
