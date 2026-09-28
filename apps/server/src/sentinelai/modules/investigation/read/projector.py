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

from sentinelai.modules.investigation.read.repository import GraphProjectionRepository
from sentinelai.modules.investigation.repository import InvestigationUnitOfWork
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.logging import log


def _uuid(value: object) -> UUID | None:
    """Parse a payload id, returning ``None`` rather than raising on anything unusable.

    A projector must not dead-letter an event over a malformed field: the fact already happened on
    the write side, and a poisoned projection row is recoverable by rebuild while a dead-lettered
    event is not replayed automatically. A skipped projection is logged and converges on the next
    rebuild; a crashed handler blocks the aggregate's whole queue (ADR-0006's per-aggregate order).
    """
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


async def project_correlation_generated(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Project a newly-generated relationship and its endpoints into a case's graph.

    ``investigation.correlation_generated`` carries ``case_id`` beside ``relationship_id``
    (§25.8), and that pairing is the whole reason this projection can exist: it is the case→entity
    mapping `service.get_case_graph` was deferred for, supplied by the event stream rather than by a
    cross-schema join `database-design.md` §5 forbids.

    The relationship and both entities are read from **this module's own transactional tables** — a
    projector may read the write side it projects, and the event deliberately carries an id rather
    than a copy of the row (§25.8's "fetched via GET if needed" convention), so the projection
    always
    reflects the row as it stands rather than as it was when the event was minted.
    """
    payload = event.payload
    case_id = _uuid(payload.get("case_id"))
    relationship_id = _uuid(payload.get("relationship_id"))
    if case_id is None or relationship_id is None:
        # An `entity_id`-only variant is permitted by §25.8 and is not produced by any current
        # publisher; it would need a case-scoped entity projection, which §25.8 does not define.
        log.info(
            "graph_projection_skipped",
            reason="no case_id/relationship_id in payload",
            event_type=event.event_type,
        )
        return

    relationship = await uow.relationships.get_by_id(relationship_id)
    if relationship is None:
        # The relationship was deleted between publish and projection. Nothing to project, and
        # nothing wrong — the projection is a view, and a view of an absent row is absence.
        log.info("graph_projection_skipped", reason="relationship not found", case_id=str(case_id))
        return

    projection = GraphProjectionRepository(uow.session)
    now = datetime.now(UTC)

    for entity_id in (relationship.from_entity_id, relationship.to_entity_id):
        entity = await uow.entities.get_by_id(entity_id)
        if entity is None:
            # A dangling endpoint. The edge is still projected below; the read query drops an edge
            # whose endpoints are absent, which keeps §6's "endpoints guaranteed present in
            # entities" true without this layer having to enforce it.
            log.info(
                "graph_projection_missing_endpoint",
                case_id=str(case_id),
                entity_id=str(entity_id),
            )
            continue
        await projection.upsert_node(
            case_id=case_id,
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            canonical_name=entity.canonical_name,
            status=entity.status,
            confidence=entity.confidence,
            # Directly evidenced: this relationship was generated for this case.
            is_seed=True,
            projected_at=now,
        )

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
    relationship_id = _uuid(event.payload.get("relationship_id"))
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


__all__ = ["project_correlation_generated", "project_finding_reviewed"]
