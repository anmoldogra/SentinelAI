"""investigation business logic (guide Part 5) — real implementation.

The human-in-the-loop review surface (PRD FR-7.3): an AI-proposed entity/relationship
only becomes ``confirmed``/``rejected`` by an explicit analyst action, guarded by
``If-Match`` optimistic concurrency, recorded as an append-only revision, audited, and
(for relationships) announced via ``investigation.finding_reviewed``.

Lifecycle (database-design.md §3.5): ``proposed → confirmed | rejected``; a finding
already dispositioned returns 409 (api-design.md §6 — "explicit re-open flow", which
has no documented endpoint yet). Cross-domain correlation itself (``run_correlation``)
is AI/LLM-driven and deferred; the write-paths it will call (``create_relationship``)
are implemented here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.investigation.events import (
    EVENT_CORRELATION_GENERATED,
    EVENT_FINDING_REVIEWED,
)
from sentinelai.modules.investigation.exceptions import (
    CorrelationRunNotFoundError,
    EntityNotFoundError,
    RelationshipNotFoundError,
)
from sentinelai.modules.investigation.models import (
    STATUS_CONFIRMED as _STATUS_CONFIRMED,
)
from sentinelai.modules.investigation.models import (
    STATUS_PROPOSED as _STATUS_PROPOSED,
)
from sentinelai.modules.investigation.models import (
    STATUS_REJECTED as _STATUS_REJECTED,
)
from sentinelai.modules.investigation.models import (
    CorrelationRun,
    Entity,
    EntityEvidenceMention,
    EntityRevision,
    Relationship,
    RelationshipEvidence,
    RelationshipRevision,
)
from sentinelai.modules.investigation.read.models import CaseGraphEdge, CaseGraphNode
from sentinelai.modules.investigation.read.repository import GraphProjectionRepository
from sentinelai.modules.investigation.repository import (
    InvestigationUnitOfWork,
    get_investigation_uow,
)
from sentinelai.modules.investigation.schemas import EntityCreate
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.tasks import TaskQueue
from sentinelai.shared.exceptions import PreconditionFailedError
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor

# The status vocabulary and the review machine belong to the finding aggregates (ADR-0011 §1,
# models.py). Re-exported here because the router and schemas already import these names.
STATUS_PROPOSED = _STATUS_PROPOSED
STATUS_CONFIRMED = _STATUS_CONFIRMED
STATUS_REJECTED = _STATUS_REJECTED


def entity_etag(entity: Entity) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "canonical_name": entity.canonical_name,
                "aliases": entity.aliases,
                "status": entity.status,
                "confidence": str(entity.confidence),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f'W/"{digest}"'


def relationship_etag(relationship: Relationship) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "type": relationship.type,
                "status": relationship.status,
                "confidence": str(relationship.confidence),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f'W/"{digest}"'


def _normalize_etag(value: str) -> str:
    return value.strip().removeprefix("W/").strip('"')


def _actor_role(actor: CurrentUser) -> str:
    return actor.roles[0] if actor.roles else "unknown"


class InvestigationService:
    def __init__(self, uow: InvestigationUnitOfWork, *, kms: KeyManagementService) -> None:
        self._uow = uow
        # The read half (ADR-0013). Built on the same session, so a graph read participates in the
        # request's transaction like any other query — it simply touches a different schema.
        self._graph = GraphProjectionRepository(uow.session)
        # Required, not optional: every audit write this service makes must be signed
        # (ADR-0003 §1), and an optional KMS would make an unsigned one reachable.
        self._kms = kms

    async def _audit(
        self, actor: CurrentUser, action: str, target_id: UUID, details: dict[str, object]
    ) -> None:
        await record_audit_event(
            self._uow.session,
            kms=self._kms,
            actor_user_id=actor.user_id,
            actor_role=_actor_role(actor),
            action=action,
            module="investigation",
            target_type="finding",
            target_id=target_id,
            details=details,
        )

    # -- entities -----------------------------------------------------------
    async def create_entity(
        self, data: EntityCreate, actor: CurrentUser, correlation_id: str
    ) -> Entity:
        # An analyst pre-registering a KNOWN entity vouches for it → confirmed
        # (assumption: api-design.md doesn't fix the resulting status).
        entity = Entity(
            entity_type=data.entity_type,
            canonical_name=data.canonical_name,
            aliases=data.aliases,
            status=STATUS_CONFIRMED,
            confidence=data.confidence,
            created_by_type="analyst",
            created_by_ref=actor.user_id,
        )
        await self._uow.entities.add(entity)
        await self._audit(actor, "entity.created", entity.entity_id, {"type": data.entity_type})
        return entity

    async def get_entity(self, entity_id: UUID, actor: CurrentUser) -> Entity:
        entity = await self._uow.entities.get_by_id(entity_id)
        if entity is None:
            raise EntityNotFoundError()
        return entity

    async def list_entities(
        self, actor: CurrentUser, status: str | None, page: PageParams
    ) -> tuple[list[Entity], str | None, bool]:
        cursor_id = _decode_id_cursor(page.cursor)
        rows = await self._uow.entities.list_(status=status, limit=page.limit, cursor_id=cursor_id)
        return _paginate(rows, page.limit, lambda e: e.entity_id)

    async def review_entity_status(
        self,
        entity_id: UUID,
        disposition: str,
        actor: CurrentUser,
        correlation_id: str,
        expected_etag: str,
    ) -> Entity:
        entity = await self._uow.entities.get_by_id(entity_id)
        if entity is None:
            raise EntityNotFoundError()
        # ETag stays here: optimistic concurrency is an HTTP concern with no domain meaning, and
        # it must be checked before the aggregate mutates anything.
        if _normalize_etag(expected_etag) != _normalize_etag(entity_etag(entity)):
            raise PreconditionFailedError("entity was modified concurrently (ETag mismatch)")
        # ADR-0011 §1: the vocabulary and the review-once rule are the aggregate's.
        previous = entity.review(disposition)
        await self._uow.entity_revisions.add(
            EntityRevision(
                entity_id=entity_id,
                field_changed="status",
                previous_value=previous,
                new_value=disposition,
                changed_by_ref=actor.user_id,
                occurred_at=datetime.now(UTC),
            )
        )
        # No entity-review event is documented (§25.8's finding_reviewed is relationship-
        # specific); audit only.
        await self._audit(actor, "entity.reviewed", entity_id, {"disposition": disposition})
        return entity

    async def list_entity_relationships(
        self, entity_id: UUID, actor: CurrentUser
    ) -> Sequence[Relationship]:
        await self.get_entity(entity_id, actor)
        return await self._uow.relationships.list_for_entity(entity_id)

    async def list_entity_evidence(
        self, entity_id: UUID, actor: CurrentUser
    ) -> Sequence[EntityEvidenceMention]:
        await self.get_entity(entity_id, actor)
        return await self._uow.entity_mentions.list_for_entity(entity_id)

    # -- relationships ------------------------------------------------------
    async def create_relationship(
        self,
        *,
        case_id: UUID,
        rel_type: str,
        from_entity_id: UUID,
        to_entity_id: UUID,
        directional: bool,
        confidence: float,
        evidence_ids: Sequence[UUID],
        created_by_ref: UUID,
        correlation_id: str,
        case_owner_user_id: UUID | None = None,
    ) -> Relationship:
        """Persist an AI-proposed relationship + its mandatory supporting evidence
        (CEM §13, ≥1) and announce it. Called by the correlation job (deferred).

        ``case_owner_user_id`` is the investigator to notify (§25.8's ``recipient_user_id``).
        It is a parameter rather than a lookup because this module must not reach into
        ``case_management`` on a write path; the correlation job already loads the case to
        select its eligible evidence, so it has the owner to hand. Omitting it publishes the
        event without a recipient, and the notification consumer ignores it rather than failing.
        """
        # ADR-0011 §1: the aggregate owns the invariant; this is where CEM §13 puts it.
        Relationship.assert_supporting_evidence(len(evidence_ids))
        relationship = Relationship(
            type=rel_type,
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            directional=directional,
            confidence=confidence,
            status=STATUS_PROPOSED,
            created_by_type="ai",
            created_by_ref=created_by_ref,
        )
        await self._uow.relationships.add(relationship)
        for evidence_id in evidence_ids:
            await self._uow.relationship_evidence.add(
                RelationshipEvidence(
                    relationship_id=relationship.relationship_id, evidence_id=evidence_id
                )
            )
        await self._uow.outbox.publish(
            event_type=EVENT_CORRELATION_GENERATED,
            aggregate_type="relationship",
            aggregate_id=relationship.relationship_id,
            payload={
                "case_id": str(case_id),
                "relationship_id": str(relationship.relationship_id),
                "confidence": str(confidence),
                "recipient_user_id": (
                    str(case_owner_user_id) if case_owner_user_id is not None else None
                ),
            },
            correlation_id=correlation_id,
            actor_type="system",
        )
        return relationship

    async def get_relationship(self, relationship_id: UUID, actor: CurrentUser) -> Relationship:
        relationship = await self._uow.relationships.get_by_id(relationship_id)
        if relationship is None:
            raise RelationshipNotFoundError()
        return relationship

    async def list_relationships(
        self, actor: CurrentUser, status: str | None, page: PageParams
    ) -> tuple[list[Relationship], str | None, bool]:
        """List relationships. ``status='proposed'`` is the AI-findings review queue (§7)."""
        cursor_id = _decode_id_cursor(page.cursor)
        rows = await self._uow.relationships.list_(
            status=status, limit=page.limit, cursor_id=cursor_id
        )
        return _paginate(rows, page.limit, lambda r: r.relationship_id)

    async def review_relationship_status(
        self,
        relationship_id: UUID,
        disposition: str,
        note: str | None,
        actor: CurrentUser,
        correlation_id: str,
        expected_etag: str,
    ) -> Relationship:
        relationship = await self._uow.relationships.get_by_id(relationship_id)
        if relationship is None:
            raise RelationshipNotFoundError()
        if _normalize_etag(expected_etag) != _normalize_etag(relationship_etag(relationship)):
            raise PreconditionFailedError("relationship was modified concurrently (ETag mismatch)")
        # ADR-0011 §1: the aggregate owns the review machine. The ≥1-supporting-evidence rule is
        # NOT checked here — CEM §1.6/§13 make it an *existence* invariant enforced at creation, so
        # an unsupported finding cannot reach review, and rejecting one that somehow did is the
        # correct outcome rather than an error.
        previous = relationship.review(disposition)
        await self._uow.relationship_revisions.add(
            RelationshipRevision(
                relationship_id=relationship_id,
                previous_status=previous,
                new_status=disposition,
            )
        )
        # NOTE: §25.8's finding_reviewed payload specifies case_id, but a relationship
        # has no stored case link (database-design.md §3.5) and the case↔evidence bridge
        # is deferred (Q2) — so case_id cannot be derived at review time. Published with
        # the derivable fields; see the phase report's inconsistency list.
        await self._uow.outbox.publish(
            event_type=EVENT_FINDING_REVIEWED,
            aggregate_type="relationship",
            aggregate_id=relationship_id,
            payload={
                "relationship_id": str(relationship_id),
                "disposition": disposition,
                "reviewed_by": str(actor.user_id),
                "note": note,
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor, EVENT_FINDING_REVIEWED, relationship_id, {"disposition": disposition}
        )
        return relationship

    async def list_relationship_evidence(
        self, relationship_id: UUID, actor: CurrentUser
    ) -> Sequence[RelationshipEvidence]:
        await self.get_relationship(relationship_id, actor)
        return await self._uow.relationship_evidence.list_for_relationship(relationship_id)

    # -- correlation runs ---------------------------------------------------
    async def trigger_correlation_run(
        self, case_id: UUID, actor: CurrentUser, correlation_id: str, task_queue: TaskQueue
    ) -> CorrelationRun:
        run = CorrelationRun(
            case_id=case_id,
            status="pending",
            started_at=None,
            completed_at=None,
            findings_generated_count=0,
            cancellation_requested=False,
        )
        await self._uow.correlation_runs.add(run)
        await task_queue.enqueue_job("run_correlation", run.run_id)
        await self._audit(actor, "correlation.requested", run.run_id, {"case_id": str(case_id)})
        return run

    async def get_correlation_run(self, run_id: UUID, actor: CurrentUser) -> CorrelationRun:
        run = await self._uow.correlation_runs.get_by_id(run_id)
        if run is None:
            raise CorrelationRunNotFoundError()
        return run

    async def get_case_graph(
        self,
        case_id: UUID,
        actor: CurrentUser,
        *,
        statuses: Sequence[str] | None = None,
        entity_types: Sequence[str] | None = None,
        min_confidence: Decimal | None = None,
        depth: int = 1,
    ) -> tuple[Sequence[CaseGraphNode], Sequence[CaseGraphEdge]]:
        """Return a case's filtered subgraph, read **only** from the CQRS projection (ADR-0013).

        This was deferred for eight phases as "blocked on the case→evidence bridge": relationships
        reference evidence, cases reference evidence, and `database-design.md` §5 forbids the
        cross-schema foreign key that would join them. The projection resolves it without bending
        that rule — ``investigation.correlation_generated`` carries ``case_id`` beside
        ``relationship_id`` (§25.8), so the event stream supplies the mapping the schema cannot.

        **Reads touch no transactional table.** That is ADR-0013 §1's separation, and it is what the
        schema boundary makes checkable rather than aspirational. The returned rows are projection
        rows, not ORM entities: mapping them back to ``Entity``/``Relationship`` would reintroduce
        the coupling the projection exists to remove, and the two have deliberately different
        shapes.

        The caller is already authorized — ``require_case_access`` gates the route, so ABAC (owner
        or
        member, ADR-0017) runs before this method is reached. The projection is queried by
        ``case_id`` alone and holds no access rules of its own; a read model that made authorization
        decisions would be a second place for them to drift from `platform`'s.

        Staleness is bounded by dispatcher latency (§2, seconds) and is visible: every projected row
        carries ``projected_at``.
        """
        return await self._graph.read_subgraph(
            case_id,
            statuses=statuses if statuses else (STATUS_PROPOSED, STATUS_CONFIRMED),
            entity_types=entity_types,
            min_confidence=min_confidence,
            depth=depth,
        )


def _decode_id_cursor(cursor: str | None) -> UUID | None:
    if not cursor:
        return None
    _sort_value, id_value = decode_cursor(cursor)
    return id_value


def _paginate[T](
    rows: Sequence[T], limit: int, id_getter: Callable[[T], UUID]
) -> tuple[list[T], str | None, bool]:
    has_more = len(rows) > limit
    items = list(rows[:limit])
    next_cursor: str | None = None
    if has_more and items:
        last_id = id_getter(items[-1])
        next_cursor = encode_cursor(str(last_id), last_id)
    return items, next_cursor, has_more


def get_investigation_service(
    uow: InvestigationUnitOfWork = Depends(get_investigation_uow),
    kms: KeyManagementService = Depends(get_kms),
) -> InvestigationService:
    return InvestigationService(uow, kms=kms)


__all__ = [
    "InvestigationService",
    "entity_etag",
    "get_investigation_service",
    "relationship_etag",
]
