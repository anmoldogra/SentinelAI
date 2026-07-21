"""ingestion persistence + Unit of Work (guide Part 3). Persistence only.

The custody ledger is append-only and hash-chained per ``evidence_id`` — the
repository exposes the primitives (next sequence number, last hash) the service
needs to extend a chain, but never mutates a prior row.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.ingestion.models import (
    AttributeSchemaRegistry,
    ConnectorRegistry,
    Evidence,
    EvidenceCustodyEvent,
    IntakeRecord,
)
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter

_SCHEMA = "ingestion"


class EvidenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, evidence_id: UUID) -> Evidence | None:
        raise NotImplementedError

    async def add(self, evidence: Evidence) -> None:
        raise NotImplementedError

    async def exists(self, evidence_id: UUID) -> bool:
        raise NotImplementedError

    async def list_(self, *, limit: int, cursor: str | None) -> Sequence[Evidence]:
        raise NotImplementedError


class CustodyEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: EvidenceCustodyEvent) -> None:
        raise NotImplementedError

    async def list_for_evidence(self, evidence_id: UUID) -> Sequence[EvidenceCustodyEvent]:
        raise NotImplementedError

    async def last_entry(self, evidence_id: UUID) -> EvidenceCustodyEvent | None:
        raise NotImplementedError


class IntakeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: IntakeRecord) -> None:
        raise NotImplementedError


class ConnectorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, connector: ConnectorRegistry) -> None:
        raise NotImplementedError

    async def get_by_id(self, connector_id: UUID) -> ConnectorRegistry | None:
        raise NotImplementedError

    async def list_(self) -> Sequence[ConnectorRegistry]:
        raise NotImplementedError


class AttributeSchemaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_(self) -> Sequence[AttributeSchemaRegistry]:
        raise NotImplementedError


class IngestionUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.evidence = EvidenceRepository(session)
        self.custody = CustodyEventRepository(session)
        self.intake = IntakeRepository(session)
        self.connectors = ConnectorRepository(session)
        self.attribute_schemas = AttributeSchemaRepository(session)
        self.outbox = OutboxWriter(session, schema=_SCHEMA)


async def get_ingestion_uow(session: AsyncSession = Depends(get_session)) -> IngestionUnitOfWork:
    return IngestionUnitOfWork(session)
