"""investigation persistence + Unit of Work (guide Part 3). Persistence only.

Entities and relationships have no timestamp column (database-design.md §3.5), so
keyset pagination orders by the UUID primary key (stable, though not time-ordered).
The review queue is a ``status = 'proposed'`` filter (§7). Graph loading over an
evidence-id set feeds the projectors; the case→evidence bridge that supplies it is the
event stream, not a join (ADR-0013). ``CorrelationRunRepository``'s two extra queries
serve the run: one answers api-design.md §6's 409, the other re-reads the cancellation
flag that only another transaction can set.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.investigation.models import (
    RUN_IN_PROGRESS,
    CorrelationRun,
    Entity,
    EntityEvidenceMention,
    EntityRevision,
    Relationship,
    RelationshipEvidence,
    RelationshipRevision,
)
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter
from sentinelai.platform.events.signing import EventSigner

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

    async def find_by_type_and_name(self, entity_type: str, canonical_name: str) -> Entity | None:
        """The one entity of this type with this canonical name, or ``None``.

        Entity resolution for the machine-created path: a `digital_asset` *is* its value, so the
        same indicator arriving from two feeds, or matching two evidence items, must converge on one
        node rather than littering the graph with duplicates of one domain.

        Deliberately **not** backed by a unique constraint. Canonical names are not unique in
        general — two people can both be "John Smith", and CEM §7's entity taxonomy has no
        identifier that would separate them — so uniqueness belongs to the caller's type, not the
        table. ``limit(1)`` rather than ``scalar_one_or_none`` for that reason: a pre-existing pair
        of duplicates must not turn a match into a 500.
        """
        result = await self._session.execute(
            select(Entity)
            .where(Entity.entity_type == entity_type, Entity.canonical_name == canonical_name)
            .order_by(Entity.entity_id)
            .limit(1)
        )
        return result.scalars().first()

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

    async def find_between(
        self, *, rel_type: str, first_entity_id: UUID, second_entity_id: UUID
    ) -> Relationship | None:
        """An existing relationship of this type between these two entities, either way round.

        Direction-insensitive because the caller creates ``associated_with``, which CEM §8 types
        "Any to Any" and which this module stores with ``directional = False``: A-to-B and B-to-A
        are one edge, and matching only on the stored order would produce a second one.
        """
        result = await self._session.execute(
            select(Relationship)
            .where(
                Relationship.type == rel_type,
                or_(
                    and_(
                        Relationship.from_entity_id == first_entity_id,
                        Relationship.to_entity_id == second_entity_id,
                    ),
                    and_(
                        Relationship.from_entity_id == second_entity_id,
                        Relationship.to_entity_id == first_entity_id,
                    ),
                ),
            )
            .order_by(Relationship.relationship_id)
            .limit(1)
        )
        return result.scalars().first()

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

    async def exists_for_pair(self, *, entity_id: UUID, evidence_id: UUID) -> bool:
        """Whether this evidence already mentions this entity — §25.8's business idempotency key.

        The cheap half of the guarantee ``uq_entity_mention_pair`` enforces: this keeps a
        redelivered or replayed match quiet, the index makes a concurrent one impossible.
        """
        result = await self._session.execute(
            select(EntityEvidenceMention.mention_id)
            .where(
                EntityEvidenceMention.entity_id == entity_id,
                EntityEvidenceMention.evidence_id == evidence_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

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

    async def find_in_progress_for_case(self, case_id: UUID) -> CorrelationRun | None:
        """A run for this case that is ``queued`` or ``running`` — api-design.md §6's 409.

        ``queued`` counts: the row exists and a worker will claim it, so a second trigger would put
        two runs over one case on the queue. Ordered by ``run_id`` and limited, because the answer
        is "is there one" and a pre-existing pair must not turn a trigger into a 500.
        """
        result = await self._session.execute(
            select(CorrelationRun)
            .where(
                CorrelationRun.case_id == case_id,
                CorrelationRun.status.in_(sorted(RUN_IN_PROGRESS)),
            )
            .order_by(CorrelationRun.run_id)
            .limit(1)
        )
        return result.scalars().first()

    async def is_cancellation_requested(self, run_id: UUID) -> bool:
        """Whether cancellation has been requested — read fresh, every time it is asked.

        A dedicated query rather than the in-memory attribute on purpose. The flag is set by
        *somebody else* while the run is in flight, which is the only way it can ever become true,
        and the session was configured ``expire_on_commit=False`` — so the loaded instance would
        keep answering ``False`` no matter what the row said. Reading the column is what makes guide
        Part 12's cooperative cancellation actually cooperative.
        """
        result = await self._session.execute(
            select(CorrelationRun.cancellation_requested).where(CorrelationRun.run_id == run_id)
        )
        return bool(result.scalar_one_or_none())


class InvestigationUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession, *, kms: KeyManagementService | None = None) -> None:
        super().__init__(session)
        self.entities = EntityRepository(session)
        self.entity_revisions = EntityRevisionRepository(session)
        self.relationships = RelationshipRepository(session)
        self.relationship_revisions = RelationshipRevisionRepository(session)
        self.relationship_evidence = RelationshipEvidenceRepository(session)
        self.entity_mentions = EntityMentionRepository(session)
        self.correlation_runs = CorrelationRunRepository(session)
        self.outbox = OutboxWriter(
            session,
            schema=_SCHEMA,
            # ADR-0007 §1: events this module publishes are signed under EVENT_ROOT. `None`
            # writes them unsigned, which the dispatcher's strict mode then refuses -- so an
            # unwired publisher fails loudly at consume time rather than silently here.
            signer=EventSigner(kms) if kms is not None else None,
        )


async def get_investigation_uow(
    session: AsyncSession = Depends(get_session),
    kms: KeyManagementService = Depends(get_kms),
) -> InvestigationUnitOfWork:
    return InvestigationUnitOfWork(session, kms=kms)
