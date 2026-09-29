"""investigation event wiring — event-driven-architecture.md §25.8.

Published: correlation run/finding lifecycle facts, emitted from ``service.py``, the correlation
job, and — since the threat-intel loop closed — ``on_ioc_matched`` below. Consumed: four upstream
events. Every handler performs the Inbox claim BEFORE any side effect (§17).

**Three handlers now do real work**, and the rest are honest no-ops:

* ``investigation.correlation_generated`` and ``investigation.finding_reviewed`` feed this module's
  own graph projectors (ADR-0013). A module consuming its own published events is deliberate —
  routing the projection through the outbox is what makes it rebuildable by replay instead of a side
  effect welded to the write path.
* ``threat_intel.ioc_matched`` turns a match into a graph finding (below).
* ``evidence.linked_to_case`` projects what is already grounded in that evidence into the case's
  graph, which is the ordering half of the same bridge.

``evidence.ingested`` remains a deferred no-op: §25.8's action for it is "index new evidence for
correlation candidacy", and §3.5 defines no table to record that in — the (future) correlation job
reads eligible evidence live. ``evidence.unlinked_from_case`` is a no-op for a
different reason, recorded in its docstring. Both still claim and mark the inbox so redelivery is
absorbed and real events are not dead-lettered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sentinelai.modules.case_management.public import CaseEvidenceRef, read_cases_for_evidence
from sentinelai.modules.investigation.models import (
    STATUS_PROPOSED,
    Entity,
    EntityEvidenceMention,
    Relationship,
    RelationshipEvidence,
)
from sentinelai.modules.investigation.payloads import payload_decimal, payload_uuid
from sentinelai.modules.investigation.read.projector import (
    project_correlation_generated,
    project_evidence_linked,
    project_finding_reviewed,
)
from sentinelai.modules.investigation.repository import InvestigationUnitOfWork
from sentinelai.modules.threat_intel.public import IocRead, read_ioc
from sentinelai.platform.events.dispatcher import EventDispatcher
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import InboxGuard
from sentinelai.platform.logging import log

SCHEMA = "investigation"

# Published (§25.8).
EVENT_CORRELATION_RUN_COMPLETED = "investigation.correlation_run_completed"
EVENT_CORRELATION_RUN_FAILED = "investigation.correlation_run_failed"
EVENT_CORRELATION_GENERATED = "investigation.correlation_generated"
EVENT_FINDING_REVIEWED = "investigation.finding_reviewed"

# Consumed (§25.8).
EVENT_EVIDENCE_INGESTED = "evidence.ingested"
EVENT_EVIDENCE_LINKED_TO_CASE = "evidence.linked_to_case"
EVENT_EVIDENCE_UNLINKED_FROM_CASE = "evidence.unlinked_from_case"
EVENT_IOC_MATCHED = "threat_intel.ioc_matched"

# ADR-0013's projection handlers. Investigation consuming its own published events is deliberate
# and not a loop: the publisher writes the transactional row, the projector builds the read model
# from the committed fact, and routing that through the outbox is what makes the projection
# rebuildable by replay (§2) rather than a side effect of the write path.
_H_PROJECT_CORRELATION = "investigation.project_case_graph"
_H_PROJECT_REVIEW = "investigation.project_finding_review"

_H_INDEX = "investigation.index_evidence"
_H_ELIGIBLE = "investigation.mark_eligible"
_H_INELIGIBLE = "investigation.mark_ineligible"
_H_IOC = "investigation.consider_ioc_match"

# CEM §7's entity type for an indicator, verbatim: `digital_asset` is "A file, domain, IP, URL, or
# indicator". An IOC is not a new node type and must not become one — the taxonomy is closed, and
# `entity_types` is a documented filter on `GET /cases/{case_id}/graph` (api-design.md §6), so a
# value outside §7 would be unfilterable by any client written against the contract.
INDICATOR_ENTITY_TYPE = "digital_asset"

# CEM §8's type for "these two things turned up together": `associated_with`, "Any ↔ Any", "Generic,
# weighted association where a more specific type doesn't apply". §8's list is closed and holds no
# indicator-specific type, and CEM §10 names "co-occurrence within the same evidence item" as an
# extraction target — so this is the documented edge a match can ground, and the only one.
CO_OCCURRENCE_REL_TYPE = "associated_with"

# **An assumption, flagged as one: no document fixes this number.** The co-occurrence itself is
# certain — both indicators are present in the same evidence item, by exact match — but what it
# implies about the two being *related* is not, which is precisely why §8 calls `associated_with`
# "weighted" and why the finding is written `proposed` for an analyst to dispose of (PRD FR-7.3).
# 0.500 says "grounded, unweighted": high enough to be returned by default, low enough that
# api-design.md §6's `min_confidence` filter excludes it at any threshold above a half. A real
# weight belongs to the correlation run's model, which is where the number should come from once
# that exists.
CO_OCCURRENCE_CONFIDENCE = Decimal("0.500")

# `analyst` or `ai` are the two values database-design.md §3.5 documents for `created_by_type`, and
# CEM §8's `created_by` says the same ("Analyst, or AI + model/version if AI-generated"). A
# deterministic matcher is neither in spirit, but the distinction the column exists to draw is
# human-vouched versus machine-derived, and this is machine-derived — so it is `ai`, it is
# `proposed`, and it goes to human review like every other machine finding. Claiming `analyst` would
# assert that a person vouched for it, and inventing a third value would put data in the column that
# no reader of §3.5 is expecting.
CREATED_BY_MACHINE = "ai"

# How many co-mentioned entities one match may associate itself with. `attributes` are
# connector-supplied and a future extraction layer will write many mentions per evidence item, so
# without a bound one crowded evidence item could turn a single match into a review queue nobody can
# work through. Bounded and logged rather than silent, so the cap is visible when it bites.
_MAX_CO_OCCURRENCE = 25

# §25.8's payload carries "one of `relationship_id`/`entity_id`" — the two kinds of finding a
# correlation announces, and what `aggregate_type` names for each (§9).
FindingKind = Literal["entity", "relationship"]

IndicatorReader = Callable[[UUID], Awaitable[IocRead | None]]
CaseLinkReader = Callable[[UUID], Awaitable[Sequence[CaseEvidenceRef]]]


async def _claim_and_ack(event: EventEnvelope, uow: InvestigationUnitOfWork, handler: str) -> bool:
    """Inbox claim + mark-processed. Returns False on redelivery (already handled)."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=handler):
        return False
    # (Phase-1 correlation side-effect deferred — see module docstring.)
    await guard.mark_processed(event.event_id, handler_name=handler)
    return True


async def on_evidence_ingested(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Index new evidence for correlation candidacy (deferred no-op; dedup real)."""
    await _claim_and_ack(event, uow, _H_INDEX)


async def on_evidence_linked_to_case(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Project what this evidence already grounds into the case it was just linked to (ADR-0013).

    §25.8's action for this event is "mark evidence eligible for this case's correlation runs", and
    §3.5 defines no eligibility table — but in a CQRS world the useful half of that sentence is
    expressible: the case's graph should now show the entities this evidence mentions and the
    relationships it supports. See ``read/projector.project_evidence_linked`` for why that matters
    (it is what stops a match landing before the link from being invisible forever).
    """
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_H_ELIGIBLE):
        return
    await project_evidence_linked(event, uow)
    await guard.mark_processed(event.event_id, handler_name=_H_ELIGIBLE)


async def on_evidence_unlinked_from_case(
    event: EventEnvelope, uow: InvestigationUnitOfWork
) -> None:
    """Mark evidence ineligible (deferred no-op; dedup real).

    **Deliberately not the inverse of the handler above, and this is a known gap.** Retracting the
    nodes an unlinked evidence item contributed would need per-evidence provenance in the
    projection, and `investigation_read` holds none — a node can be grounded by several evidence
    items, so "drop what this one brought" is not answerable from the rows as ADR-0013 §1 models
    them. §25.8 also states the write-side rule that an unlink "does not retroactively invalidate
    already-`confirmed` relationships", so silently deleting projected rows would contradict it.
    Recorded rather than guessed at: the projection is rebuildable, so the correct fix is either a
    provenance column or a case rebuild, and that is a documented decision, not a handler detail.
    """
    await _claim_and_ack(event, uow, _H_INELIGIBLE)


async def on_ioc_matched(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Turn a threat-intel match into a graph finding (§25.8, ADR-0013).

    The inbox claim is load-bearing here, not tidy: without it a redelivery would re-run writes that
    create entities and publish findings. ``record_ioc_match`` is separately idempotent on §25.8's
    business key, which is what covers **replay** — clearing the inbox and re-delivering is a
    documented operation (§ Replay), and it must not double the graph.
    """
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_H_IOC):
        return
    await record_ioc_match(event, uow)
    await guard.mark_processed(event.event_id, handler_name=_H_IOC)


async def on_correlation_generated(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Project a generated finding into its case's graph (ADR-0013).

    Unlike the deferred no-op handlers above, this one has a **real** side effect, so the inbox
    claim is load-bearing rather than merely tidy: without it a redelivery would re-run the
    projection writes. Those writes are themselves upserts and would converge anyway — that
    redundancy is deliberate (see ``read/projector.py``), not a reason to drop the claim.
    """
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_H_PROJECT_CORRELATION):
        return
    await project_correlation_generated(event, uow)
    await guard.mark_processed(event.event_id, handler_name=_H_PROJECT_CORRELATION)


async def on_finding_reviewed(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Fold a disposition into every case graph showing the relationship (ADR-0013)."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_H_PROJECT_REVIEW):
        return
    await project_finding_reviewed(event, uow)
    await guard.mark_processed(event.event_id, handler_name=_H_PROJECT_REVIEW)


async def record_ioc_match(
    event: EventEnvelope,
    uow: InvestigationUnitOfWork,
    *,
    read_indicator: IndicatorReader | None = None,
    read_case_links: CaseLinkReader | None = None,
) -> None:
    """Record a matched indicator as a case-graph finding. §25.8's "correlation input", built.

    **What a match is, in this model, and what it therefore cannot be.** `threat_intel.ioc_matched`
    says "indicator I is present in evidence E". CEM §11 gives the graph one node type — `Entity` —
    and types its edges between *entities*; evidence appears as a MENTIONS edge, and
    api-design.md §6's response is `{ entities, relationships }` with nothing else in it. So a match
    produces a **node** — the indicator, as a `digital_asset` entity grounded by a MENTIONS row —
    and not an edge to the evidence: there is no evidence node for an edge to reach, and CEM §8's
    closed relationship vocabulary has no `ioc_matched` type to give one. An edge needs two
    entities, and a single match supplies one.

    **Where the edge does come from.** If the same evidence item mentions another entity, the two
    co-occur in it, which CEM §10 names as a relationship-inference target and CEM §8 types
    `associated_with`. That edge is grounded in the matched evidence — the evidence genuinely
    supports "both of these appear here" — so it satisfies CEM §13's ≥1 supporting-evidence rule
    honestly. Today the usual case is two indicators in one evidence item; when an extraction layer
    starts writing mentions, the same code relates an indicator to the people and accounts named
    beside it, with no change here.

    **Written from the consumer path, not through ``InvestigationService``.** The dispatcher hands a
    handler a session and a signed outbox — no KMS, no object storage — and every method on that
    service is an audited user action needing one. A match is not a user action:
    `platform.audit_log` records what principals did, and no principal did this. The durable record
    is the entity, the MENTIONS row, and the published finding, all attributable to the platform.

    **Idempotent on §25.8's business key.** `(ioc_id, matched_evidence_id)` lands here as
    `(entity_id, evidence_id)` — the indicator entity is resolved from the IOC's value, so the pair
    is the same fact — and it is checked before inserting *and* enforced by
    ``uq_entity_mention_pair``. Layer 2 of §12, which the inbox check cannot provide: a second,
    independently-published match for the same pair carries a new `event_id`.

    The readers are injectable so a test can drive this without a `threat_intel` or
    `case_management` schema; the defaults are the real §174 fetch paths.
    """
    payload = event.payload
    ioc_id = payload_uuid(payload.get("ioc_id"))
    evidence_id = payload_uuid(payload.get("matched_evidence_id"))
    confidence = payload_decimal(payload.get("confidence"))
    if ioc_id is None or evidence_id is None or confidence is None:
        log.info("ioc_match_skipped", reason="malformed payload", event_type=event.event_type)
        return

    fetch_indicator = read_indicator or (lambda ioc: read_ioc(uow.session, ioc))
    indicator = await fetch_indicator(ioc_id)
    if indicator is None:
        # Withdrawn between publish and consume. Nothing to name the entity after, and inventing a
        # placeholder would put an unattributable node in a legal record.
        log.info("ioc_match_skipped", reason="ioc not found", ioc_id=str(ioc_id))
        return

    entity = await uow.entities.find_by_type_and_name(INDICATOR_ENTITY_TYPE, indicator.value)
    if entity is None:
        entity = Entity(
            entity_type=INDICATOR_ENTITY_TYPE,
            canonical_name=indicator.value,
            aliases=None,
            # Machine output is never born confirmed (PRD FR-7.3, ADR-0011 §1).
            status=STATUS_PROPOSED,
            confidence=confidence,
            created_by_type=CREATED_BY_MACHINE,
            # The indicator that produced it — an app-ref, like every other cross-schema id (§5).
            # It is the one reference that answers "why is this node here" without a join.
            created_by_ref=ioc_id,
        )
        await uow.entities.add(entity)
    elif await uow.entity_mentions.exists_for_pair(
        entity_id=entity.entity_id, evidence_id=evidence_id
    ):
        # This sighting is already recorded — §25.8's key. Returning here is what makes a replay
        # cheap rather than merely correct.
        return

    await uow.entity_mentions.add(
        EntityEvidenceMention(entity_id=entity.entity_id, evidence_id=evidence_id)
    )
    findings = await _associate_co_mentioned(
        uow, entity=entity, evidence_id=evidence_id, ioc_id=ioc_id
    )

    fetch_cases = read_case_links or (lambda eid: read_cases_for_evidence(uow.session, eid))
    case_links = await fetch_cases(evidence_id)
    if not case_links:
        # The evidence belongs to no case yet, and `correlation_generated` has no case-less form:
        # `case_id` is required in its payload (§25.8). The entity and its grounding are recorded
        # regardless — they are facts about evidence, not about a case — and they reach a case's
        # graph when the evidence is linked, via `project_evidence_linked`.
        log.info(
            "ioc_match_recorded_unlinked",
            ioc_id=str(ioc_id),
            evidence_id=str(evidence_id),
            entity_id=str(entity.entity_id),
        )
        return

    generated_by = f"threat_intel.ioc_match:{ioc_id}"
    for link in case_links:
        await _publish_finding(
            uow,
            event,
            case_id=link.case_id,
            recipient_user_id=link.owning_user_id,
            confidence=confidence,
            generated_by=generated_by,
            kind="entity",
            finding_id=entity.entity_id,
        )
        for relationship in findings:
            await _publish_finding(
                uow,
                event,
                case_id=link.case_id,
                recipient_user_id=link.owning_user_id,
                confidence=CO_OCCURRENCE_CONFIDENCE,
                generated_by=generated_by,
                kind="relationship",
                finding_id=relationship.relationship_id,
            )

    log.info(
        "ioc_match_projected",
        ioc_id=str(ioc_id),
        evidence_id=str(evidence_id),
        entity_id=str(entity.entity_id),
        cases=len(case_links),
        relationships=len(findings),
    )


async def _associate_co_mentioned(
    uow: InvestigationUnitOfWork, *, entity: Entity, evidence_id: UUID, ioc_id: UUID
) -> list[Relationship]:
    """Relate the indicator to every other entity the same evidence mentions. Returns the new ones.

    Reads this module's own MENTIONS rows, so no cross-module call is needed to find what the
    indicator co-occurs with — and the answer grows on its own as other producers record mentions.

    Ordered by entity id so the cap below is deterministic rather than planner-dependent.
    """
    co_mentioned = sorted(
        (
            other
            for other in await uow.entities.list_by_evidence_ids([evidence_id])
            if other.entity_id != entity.entity_id
        ),
        key=lambda other: other.entity_id,
    )
    if len(co_mentioned) > _MAX_CO_OCCURRENCE:
        log.warning(
            "ioc_match_co_occurrence_capped",
            evidence_id=str(evidence_id),
            entity_id=str(entity.entity_id),
            found=len(co_mentioned),
            cap=_MAX_CO_OCCURRENCE,
        )
        co_mentioned = co_mentioned[:_MAX_CO_OCCURRENCE]

    created: list[Relationship] = []
    for other in co_mentioned:
        existing = await uow.relationships.find_between(
            rel_type=CO_OCCURRENCE_REL_TYPE,
            first_entity_id=entity.entity_id,
            second_entity_id=other.entity_id,
        )
        if existing is not None:
            continue
        supporting = (evidence_id,)
        # ADR-0011 §1: the aggregate owns CEM §13's ≥1 rule, checked where the row is built.
        Relationship.assert_supporting_evidence(len(supporting))
        relationship = Relationship(
            type=CO_OCCURRENCE_REL_TYPE,
            from_entity_id=entity.entity_id,
            to_entity_id=other.entity_id,
            # `associated_with` is symmetric (§8's "Any ↔ Any"), which is also why
            # `find_between` matches either order.
            directional=False,
            confidence=CO_OCCURRENCE_CONFIDENCE,
            status=STATUS_PROPOSED,
            created_by_type=CREATED_BY_MACHINE,
            created_by_ref=ioc_id,
        )
        await uow.relationships.add(relationship)
        for supporting_evidence_id in supporting:
            await uow.relationship_evidence.add(
                RelationshipEvidence(
                    relationship_id=relationship.relationship_id,
                    evidence_id=supporting_evidence_id,
                )
            )
        created.append(relationship)
    return created


async def _publish_finding(
    uow: InvestigationUnitOfWork,
    event: EventEnvelope,
    *,
    case_id: UUID,
    recipient_user_id: UUID,
    confidence: Decimal,
    generated_by: str,
    kind: FindingKind,
    finding_id: UUID,
) -> None:
    """Announce one new proposed finding for one case (§25.8's ``correlation_generated``).

    Published from the same transaction as the write it describes (§16), and threaded onto the
    workflow that caused it: the same ``correlation_id``, and ``causation_id`` pointing one hop back
    at the ``ioc_matched`` event. §11's worked example is this exact chain —
    ``evidence.ingested → threat_intel.ioc_matched → investigation.correlation_generated`` — so the
    causal path an auditor walks is the one the document draws.

    ``kind`` selects which of §25.8's mutually-exclusive id fields carries the finding: the payload
    specifies "one of ``relationship_id``/``entity_id``", and taking one id plus its kind is what
    makes "one of" true by construction rather than by two nullable parameters that could both be
    passed or both omitted.

    One event per (case, finding) pair. A finding relevant to two cases is announced twice because
    `notification`'s idempotency key is per recipient (§25.9) and each case has its own owner to
    tell; the projection is keyed per case for the same reason.
    """
    await uow.outbox.publish(
        event_type=EVENT_CORRELATION_GENERATED,
        aggregate_type=kind,
        aggregate_id=finding_id,
        payload={
            "case_id": str(case_id),
            "entity_id": str(finding_id) if kind == "entity" else None,
            "relationship_id": str(finding_id) if kind == "relationship" else None,
            "confidence": str(confidence),
            # §25's payload schema: "model/run reference, per CEM §10's `created_by`". A match has
            # no model, so it names what did produce it.
            "generated_by": generated_by,
            # §25.8's recipient — the case owner `notification` alerts.
            "recipient_user_id": str(recipient_user_id),
        },
        correlation_id=str(event.correlation_id),
        causation_id=str(event.event_id),
        # No `actor_ref`: the platform observed this, no principal did it.
        actor_type="system",
    )


def register_consumers(dispatcher: EventDispatcher) -> None:
    # ADR-0013's projectors first: they are the handlers here with a real side effect, and
    # registering them beside the deferred no-ops makes the difference visible in one place.
    dispatcher.register(
        EVENT_CORRELATION_GENERATED,
        on_correlation_generated,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_FINDING_REVIEWED,
        on_finding_reviewed,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_EVIDENCE_INGESTED,
        on_evidence_ingested,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_EVIDENCE_LINKED_TO_CASE,
        on_evidence_linked_to_case,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_EVIDENCE_UNLINKED_FROM_CASE,
        on_evidence_unlinked_from_case,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_IOC_MATCHED,
        on_ioc_matched,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
