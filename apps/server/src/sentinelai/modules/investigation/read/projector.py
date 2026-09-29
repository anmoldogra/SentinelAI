"""The graph projectors — ADR-0013 §1/§2, event-driven §17.

One job: turn integration events into rows in ``investigation_read``. No HTTP, no business rules, no
decisions the write side has not already made — a projector that decided anything would be a second
source of truth, which §2 exists to forbid.

**Idempotency is doubled on purpose.** Every handler performs the Inbox claim before any side effect
(§17), *and* every write is an ``ON CONFLICT DO UPDATE`` that converges. Either would handle
ordinary
redelivery; together they mean the projection survives a replay that clears the inbox (§ Replay), a
handler that a future change forgets to guard, and the same event arriving twice on two workers.

**What a seed is, and why it bounds what ``depth`` can mean.** ``api-design.md`` §6 defines
``depth``
as "hops from directly-evidenced entities". A relationship announced by
``investigation.correlation_generated`` was generated *for* a case from evidence linked to it, so
its
two endpoints are directly evidenced: hop zero, ``is_seed = True``.

Today that is the only event that adds nodes, so **every projected node is a seed** and a read at
``depth`` 1, 2 or 3 returns the same subgraph — the case's own findings, which is exactly what §6's
default (``depth=1``) should return. ``depth`` starts discriminating the moment the projection also
holds edges reaching *outside* the case's findings, and feeding those needs an entity-level
projection event that `event-driven-architecture.md` §25.8 does not define. That gap is recorded in
ADR-0013 rather than closed by inventing an event (`CLAUDE.md` rule 1); the traversal is built now
because it is §3's decision and because it is what makes ``depth`` correct on the day those edges
arrive, rather than a migration away from it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sentinelai.modules.investigation.models import Relationship
from sentinelai.modules.investigation.payloads import payload_uuid
from sentinelai.modules.investigation.read.repository import GraphProjectionRepository
from sentinelai.modules.investigation.repository import InvestigationUnitOfWork
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.logging import log


async def _project_entity(
    projection: GraphProjectionRepository,
    uow: InvestigationUnitOfWork,
    *,
    case_id: UUID,
    entity_id: UUID,
    now: datetime,
) -> bool:
    """Upsert one entity as a seed node of a case's graph. ``False`` if the entity is gone.

    Read from **this module's own transactional tables** — a projector may read the write side it
    projects, and every event here carries an id rather than a copy of the row (§25.8's "fetched via
    GET if needed" convention), so the projection reflects the row as it stands rather than as it
    was when the event was minted.
    """
    entity = await uow.entities.get_by_id(entity_id)
    if entity is None:
        log.info(
            "graph_projection_missing_endpoint", case_id=str(case_id), entity_id=str(entity_id)
        )
        return False
    await projection.upsert_node(
        case_id=case_id,
        entity_id=entity.entity_id,
        entity_type=entity.entity_type,
        canonical_name=entity.canonical_name,
        status=entity.status,
        confidence=entity.confidence,
        # Directly evidenced: every path into this function arrives from evidence that belongs to
        # this case — a relationship generated for it, or an evidence item linked to it.
        is_seed=True,
        projected_at=now,
    )
    return True


async def _project_relationship(
    projection: GraphProjectionRepository,
    uow: InvestigationUnitOfWork,
    *,
    case_id: UUID,
    relationship: Relationship,
    now: datetime,
) -> None:
    """Upsert one relationship and both its endpoints into a case's graph."""
    for entity_id in (relationship.from_entity_id, relationship.to_entity_id):
        # A dangling endpoint does not stop the edge: the read query drops an edge whose endpoints
        # are absent, which keeps §6's "endpoints guaranteed present in entities" true without this
        # layer having to enforce it.
        await _project_entity(projection, uow, case_id=case_id, entity_id=entity_id, now=now)
    await projection.upsert_edge(
        case_id=case_id,
        relationship_id=relationship.relationship_id,
        rel_type=relationship.type,
        from_entity_id=relationship.from_entity_id,
        to_entity_id=relationship.to_entity_id,
        status=relationship.status,
        confidence=relationship.confidence,
        projected_at=now,
    )


async def project_correlation_generated(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Project a newly-generated finding and its endpoints into a case's graph.

    ``investigation.correlation_generated`` carries ``case_id`` beside ``relationship_id`` **or**
    ``entity_id`` (§25.8 — "one of"), and that pairing is the whole reason this projection can
    exist: it is the case→entity mapping `service.get_case_graph` was deferred for, supplied by the
    event stream rather than by a cross-schema join `database-design.md` §5 forbids.

    **Both variants are now produced.** The relationship variant comes from the correlation job; the
    ``entity_id`` variant comes from `events.on_ioc_matched`, because a threat-intel match grounds
    exactly one entity — the indicator — and an entity is a node, not an edge. Projecting it is what
    puts the match in front of an analyst.
    """
    payload = event.payload
    case_id = payload_uuid(payload.get("case_id"))
    relationship_id = payload_uuid(payload.get("relationship_id"))
    entity_id = payload_uuid(payload.get("entity_id"))
    if case_id is None or (relationship_id is None and entity_id is None):
        log.info(
            "graph_projection_skipped",
            reason="no case_id, and no relationship_id or entity_id, in payload",
            event_type=event.event_type,
        )
        return

    projection = GraphProjectionRepository(uow.session)
    now = datetime.now(UTC)

    if relationship_id is None:
        # The entity variant. The guard above establishes that `entity_id` is present.
        if entity_id is not None:
            await _project_entity(projection, uow, case_id=case_id, entity_id=entity_id, now=now)
        return

    relationship = await uow.relationships.get_by_id(relationship_id)
    if relationship is None:
        # The relationship was deleted between publish and projection. Nothing to project, and
        # nothing wrong — the projection is a view, and a view of an absent row is absence.
        log.info("graph_projection_skipped", reason="relationship not found", case_id=str(case_id))
        return

    await _project_relationship(
        projection, uow, case_id=case_id, relationship=relationship, now=now
    )


async def project_evidence_linked(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Project everything already grounded in an evidence item into the case it was just linked to.

    **This is the ordering half of the case→evidence bridge.** ``on_ioc_matched`` projects into the
    cases an evidence item belongs to *at match time*, and the common real order is the other way
    round: evidence is ingested and scanned within seconds, then an analyst links it to a case
    minutes or days later. Without this handler every match that arrived before the link would be
    invisible in that case's graph forever — recorded on the write side, absent from the read model,
    and with no event left to replay that would put it there.

    ``evidence.linked_to_case`` carries ``case_id`` and ``evidence_id``, which is exactly what the
    projection needs: the entities this evidence mentions (CEM §11's MENTIONS edges) and the
    relationships it supports (CEM §13's ``supporting_evidence_ids``) are this case's graph as far
    as this evidence is concerned.

    It writes the projection directly rather than publishing a finding, because nothing was found —
    the entities and relationships already existed and were already announced when they were
    created. A rebuild replays this same event from `case_management`'s outbox and reconstructs the
    same rows, which is the property ADR-0013 §2 requires.
    """
    case_id = payload_uuid(event.payload.get("case_id"))
    evidence_id = payload_uuid(event.payload.get("evidence_id"))
    if case_id is None or evidence_id is None:
        log.info(
            "graph_projection_skipped",
            reason="no case_id/evidence_id in payload",
            event_type=event.event_type,
        )
        return

    projection = GraphProjectionRepository(uow.session)
    now = datetime.now(UTC)
    evidence_ids = [evidence_id]

    for entity in await uow.entities.list_by_evidence_ids(evidence_ids):
        await _project_entity(projection, uow, case_id=case_id, entity_id=entity.entity_id, now=now)
    for relationship in await uow.relationships.list_by_evidence_ids(evidence_ids):
        await _project_relationship(
            projection, uow, case_id=case_id, relationship=relationship, now=now
        )


async def project_finding_reviewed(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Fold an analyst's disposition into every case graph showing that relationship.

    **Keyed on ``relationship_id`` alone.** §25.8 specifies ``case_id`` in this payload, and the
    publisher does not send one — `service.review_relationship_status` says why in a comment: a
    relationship has no stored case link, so the write side cannot derive it. The projection does
    not
    need it. It already knows which cases the relationship appears in, so a review lands on all of
    them, which is also the correct semantics: one disposition is one fact about one finding, not a
    per-case opinion.

    That makes this consumer a case where the projection *closes* a documented publisher gap rather
    than tripping over it. The gap itself is left recorded, not patched — deriving ``case_id`` on
    the
    write side by reading the read model would invert the dependency CQRS establishes.
    """
    relationship_id = payload_uuid(event.payload.get("relationship_id"))
    disposition = event.payload.get("disposition")
    if relationship_id is None or not isinstance(disposition, str):
        log.info("graph_projection_skipped", reason="malformed finding_reviewed payload")
        return

    touched = await GraphProjectionRepository(uow.session).update_edge_status(
        relationship_id=relationship_id,
        status=disposition,
        projected_at=datetime.now(UTC),
    )
    if touched == 0:
        # Ordinary, not an error: a relationship can be reviewed before its `correlation_generated`
        # event has been projected, or belong to no case graph at all. The later projection reads
        # the
        # relationship's current status from the write side, so the disposition is not lost.
        log.info(
            "graph_projection_no_edges",
            relationship_id=str(relationship_id),
            disposition=disposition,
        )


__all__ = [
    "project_correlation_generated",
    "project_evidence_linked",
    "project_finding_reviewed",
]
