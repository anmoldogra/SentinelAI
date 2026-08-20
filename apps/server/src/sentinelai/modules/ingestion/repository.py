"""ingestion persistence + Unit of Work (guide Part 3). Persistence only.

The custody ledger is append-only and hash-chained per ``evidence_id`` (CEM §4);
the repository exposes the primitives the service needs to extend a chain (last
entry → prev hash + next sequence number) but never mutates a prior row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
        result = await self._session.execute(
            select(Evidence).where(Evidence.evidence_id == evidence_id)
        )
        return result.scalar_one_or_none()

    async def add(self, evidence: Evidence) -> None:
        self._session.add(evidence)
        await self._session.flush()

    async def exists(self, evidence_id: UUID) -> bool:
        result = await self._session.execute(
            select(Evidence.evidence_id).where(Evidence.evidence_id == evidence_id)
        )
        return result.scalar_one_or_none() is not None

    async def has_replacement(self, evidence_id: UUID) -> bool:
        """Whether a replacement row supersedes ``evidence_id`` (derived `status`, ADR-0015)."""
        result = await self._session.execute(
            select(Evidence.evidence_id)
            .where(Evidence.supersedes_evidence_id == evidence_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_(
        self,
        *,
        category: str | None,
        artifact_type: str | None,
        status: str | None,
        text: str | None,
        limit: int,
        cursor_ingested_at: datetime | None,
        cursor_evidence_id: UUID | None,
    ) -> Sequence[Evidence]:
        stmt = select(Evidence)
        if category is not None:
            stmt = stmt.where(Evidence.category == category)
        if artifact_type is not None:
            stmt = stmt.where(Evidence.artifact_type == artifact_type)
        if status is not None:
            # `status` is derived, not stored (ADR-0015): `superseded` means a replacement row
            # exists; the genesis column only distinguishes the remaining values.
            replacement = aliased(Evidence)
            superseded = (
                select(replacement.evidence_id)
                .where(replacement.supersedes_evidence_id == Evidence.evidence_id)
                .exists()
            )
            if status == "superseded":
                stmt = stmt.where(superseded)
            else:
                stmt = stmt.where(Evidence.status == status, ~superseded)
        if text:
            like = f"%{text}%"
            stmt = stmt.where(or_(Evidence.title.ilike(like), Evidence.description.ilike(like)))
        if cursor_ingested_at is not None and cursor_evidence_id is not None:
            stmt = stmt.where(
                tuple_(Evidence.ingested_at, Evidence.evidence_id)
                < (cursor_ingested_at, cursor_evidence_id)
            )
        stmt = stmt.order_by(Evidence.ingested_at.desc(), Evidence.evidence_id.desc()).limit(
            limit + 1
        )
        return (await self._session.execute(stmt)).scalars().all()


class CustodyEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: EvidenceCustodyEvent) -> None:
        self._session.add(event)
        await self._session.flush()

    async def list_for_evidence(self, evidence_id: UUID) -> Sequence[EvidenceCustodyEvent]:
        result = await self._session.execute(
            select(EvidenceCustodyEvent)
            .where(EvidenceCustodyEvent.evidence_id == evidence_id)
            .order_by(EvidenceCustodyEvent.sequence_number.asc())
        )
        return result.scalars().all()

    async def last_entry(self, evidence_id: UUID) -> EvidenceCustodyEvent | None:
        result = await self._session.execute(
            select(EvidenceCustodyEvent)
            .where(EvidenceCustodyEvent.evidence_id == evidence_id)
            .order_by(EvidenceCustodyEvent.sequence_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def last_of_types(
        self, evidence_id: UUID, event_types: Sequence[str]
    ) -> EvidenceCustodyEvent | None:
        """Latest ledger entry of the given types — the derivation primitive for ADR-0015
        (`legal_hold` from hold events, `integrity_verification_status` from reverifications)."""
        result = await self._session.execute(
            select(EvidenceCustodyEvent)
            .where(
                EvidenceCustodyEvent.evidence_id == evidence_id,
                EvidenceCustodyEvent.event_type.in_(event_types),
            )
            .order_by(EvidenceCustodyEvent.sequence_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class IntakeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: IntakeRecord) -> None:
        self._session.add(record)
        await self._session.flush()


class ConnectorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, connector: ConnectorRegistry) -> None:
        self._session.add(connector)
        await self._session.flush()

    async def get_by_id(self, connector_id: UUID) -> ConnectorRegistry | None:
        result = await self._session.execute(
            select(ConnectorRegistry).where(ConnectorRegistry.connector_id == connector_id)
        )
        return result.scalar_one_or_none()

    async def list_(self) -> Sequence[ConnectorRegistry]:
        result = await self._session.execute(
            select(ConnectorRegistry).order_by(ConnectorRegistry.name)
        )
        return result.scalars().all()


class AttributeSchemaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_(self) -> Sequence[AttributeSchemaRegistry]:
        result = await self._session.execute(select(AttributeSchemaRegistry))
        return result.scalars().all()

    async def is_registered(self, schema_version: str, category: str, artifact_type: str) -> bool:
        result = await self._session.execute(
            select(AttributeSchemaRegistry.registry_id).where(
                AttributeSchemaRegistry.schema_version == schema_version,
                AttributeSchemaRegistry.category == category,
                AttributeSchemaRegistry.artifact_type == artifact_type,
            )
        )
        return result.scalar_one_or_none() is not None


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
