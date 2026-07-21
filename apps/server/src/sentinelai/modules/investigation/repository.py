"""investigation persistence + Unit of Work (guide Part 3). Persistence only.

Entities and relationships have no timestamp column (database-design.md §3.5), so
keyset pagination orders by the UUID primary key (stable, though not time-ordered).
The review queue is a ``status = 'proposed'`` filter (§7). Graph loading over an
evidence-id set is provided here (real); the case→evidence bridge that would feed it
is deferred (see service.get_case_graph).
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.investigation.models import (
    CorrelationRun,
    Entity,
    EntityEvidenceMention,
    EntityRevision,
    Relationship,
    RelationshipEvidence,
    RelationshipRevision,
)
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter

_SCHEMA = "investigation"


class EntityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, entity_id: UUID) -> Entity | None:
        result = await self._session.execute(select(Entity).where(Entity.entity_id == entity_id))
        return result.scalar_one_or_none()

    async def add(self, entity: Entity) -> None:
        self._session.add(entity)
        await self._session.flush()

    async def list_(
        self, *, status: str | None, limit: int, cursor_id: UUID | None
    ) -> Sequence[Entity]:
        stmt = select(Entity)
        if status is not None:
            stmt = stmt.where(Entity.status == status)
        if cursor_id is not None:
            stmt = stmt.where(Entity.entity_id > cursor_id)
        stmt = stmt.order_by(Entity.entity_id).limit(limit + 1)
        return (await self._session.execute(stmt)).scalars().all()

    async def list_by_evidence_ids(self, evidence_ids: Sequence[UUID]) -> Sequence[Entity]:
        if not evidence_ids:
            return []
        stmt = (
            select(Entity)
            .join(EntityEvidenceMention, EntityEvidenceMention.entity_id == Entity.entity_id)
            .where(EntityEvidenceMention.evidence_id.in_(evidence_ids))
            .distinct()
        )
        return (await self._session.execute(stmt)).scalars().all()


class EntityRevisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, revision: EntityRevision) -> None:
        self._session.add(revision)
        await self._session.flush()

    async def list_for_entity(self, entity_id: UUID) -> Sequence[EntityRevision]:
        result = await self._session.execute(
            select(EntityRevision)
            .where(EntityRevision.entity_id == entity_id)
            .order_by(EntityRevision.occurred_at.desc())
        )
        return result.scalars().all()


class RelationshipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, relationship_id: UUID) -> Relationship | None:
        result = await self._session.execute(
            select(Relationship).where(Relationship.relationship_id == relationship_id)
        )
        return result.scalar_one_or_none()

    async def add(self, relationship: Relationship) -> None:
        self._session.add(relationship)
        await self._session.flush()

    async def list_(
        self, *, status: str | None, limit: int, cursor_id: UUID | None
    ) -> Sequence[Relationship]:
        stmt = select(Relationship)
        if status is not None:
            stmt = stmt.where(Relationship.status == status)
        if cursor_id is not None:
            stmt = stmt.where(Relationship.relationship_id > cursor_id)
        stmt = stmt.order_by(Relationship.relationship_id).limit(limit + 1)
        return (await self._session.execute(stmt)).scalars().all()

    async def list_for_entity(self, entity_id: UUID) -> Sequence[Relationship]:
        result = await self._session.execute(
            select(Relationship).where(
                or_(
                    Relationship.from_entity_id == entity_id,
                    Relationship.to_entity_id == entity_id,
                )
            )
        )
        return result.scalars().all()

    async def list_by_evidence_ids(self, evidence_ids: Sequence[UUID]) -> Sequence[Relationship]:
        if not evidence_ids:
            return []
        stmt = (
            select(Relationship)
            .join(
                RelationshipEvidence,
                RelationshipEvidence.relationship_id == Relationship.relationship_id,
            )
            .where(RelationshipEvidence.evidence_id.in_(evidence_ids))
            .distinct()
        )
        return (await self._session.execute(stmt)).scalars().all()


class RelationshipRevisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, revision: RelationshipRevision) -> None:
        self._session.add(revision)
        await self._session.flush()


class RelationshipEvidenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, link: RelationshipEvidence) -> None:
        self._session.add(link)
        await self._session.flush()

    async def list_for_relationship(self, relationship_id: UUID) -> Sequence[RelationshipEvidence]:
        result = await self._session.execute(
            select(RelationshipEvidence).where(
                RelationshipEvidence.relationship_id == relationship_id
            )
        )
        return result.scalars().all()


class EntityMentionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, mention: EntityEvidenceMention) -> None:
        self._session.add(mention)
        await self._session.flush()

    async def list_for_entity(self, entity_id: UUID) -> Sequence[EntityEvidenceMention]:
        result = await self._session.execute(
            select(EntityEvidenceMention).where(EntityEvidenceMention.entity_id == entity_id)
        )
        return result.scalars().all()


class CorrelationRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, run_id: UUID) -> CorrelationRun | None:
        result = await self._session.execute(
            select(CorrelationRun).where(CorrelationRun.run_id == run_id)
        )
        return result.scalar_one_or_none()

    async def add(self, run: CorrelationRun) -> None:
        self._session.add(run)
        await self._session.flush()


class InvestigationUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.entities = EntityRepository(session)
        self.entity_revisions = EntityRevisionRepository(session)
        self.relationships = RelationshipRepository(session)
        self.relationship_revisions = RelationshipRevisionRepository(session)
        self.relationship_evidence = RelationshipEvidenceRepository(session)
        self.entity_mentions = EntityMentionRepository(session)
        self.correlation_runs = CorrelationRunRepository(session)
        self.outbox = OutboxWriter(session, schema=_SCHEMA)


async def get_investigation_uow(
    session: AsyncSession = Depends(get_session),
) -> InvestigationUnitOfWork:
    return InvestigationUnitOfWork(session)
