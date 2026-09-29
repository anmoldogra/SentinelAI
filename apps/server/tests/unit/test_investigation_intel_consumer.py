"""Unit tests for `investigation`'s threat-intel consumer — §25.8, ADR-0013.

The full loop runs against real Postgres in `test_intel_closed_loop_db.py`. What this file covers is
the *decisions* that file cannot isolate: what the handler does with a payload it cannot use, an IOC
that has been withdrawn, evidence that belongs to no case, evidence that belongs to several, and an
evidence item mentioning more entities than one finding should fan out to.

In-memory fakes at the repository seam, driven through the real handler — the same shape as
`test_notification_consumers.py`. The two cross-module readers are injected, which is what their
injection points are for: this module's logic is under test here, not `threat_intel`'s tables.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinelai.modules.case_management.public import CaseEvidenceRef
from sentinelai.modules.investigation import events as investigation_events
from sentinelai.modules.investigation.events import (
    CO_OCCURRENCE_CONFIDENCE,
    CO_OCCURRENCE_REL_TYPE,
    CREATED_BY_MACHINE,
    INDICATOR_ENTITY_TYPE,
    record_ioc_match,
)
from sentinelai.modules.investigation.models import (
    STATUS_PROPOSED,
    Entity,
    EntityEvidenceMention,
    Relationship,
    RelationshipEvidence,
)
from sentinelai.modules.investigation.payloads import payload_decimal, payload_uuid
from sentinelai.modules.threat_intel.schemas import IocRead
from sentinelai.platform.events.envelope import EventEnvelope

_IOC_ID = uuid4()
_EVIDENCE_ID = uuid4()
_CASE_ID = uuid4()
_OWNER = uuid4()
_DOMAIN = "evil-c2.example"
_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


# --- fakes ------------------------------------------------------------------
class _FakeEntities:
    def __init__(self, existing: list[Entity] | None = None) -> None:
        self.items: list[Entity] = list(existing or [])
        self.mentioned: list[Entity] = []

    async def find_by_type_and_name(self, entity_type: str, canonical_name: str) -> Entity | None:
        for entity in self.items:
            if entity.entity_type == entity_type and entity.canonical_name == canonical_name:
                return entity
        return None

    async def add(self, entity: Entity) -> None:
        # The real repository flushes, which is what assigns the column default. A fake that did not
        # would hand the handler an entity with no id and fail for the wrong reason.
        if entity.entity_id is None:
            entity.entity_id = uuid4()
        self.items.append(entity)

    async def list_by_evidence_ids(self, evidence_ids: Sequence[UUID]) -> Sequence[Entity]:
        return [*self.mentioned, *self.items]


class _FakeMentions:
    def __init__(self, pairs: set[tuple[UUID, UUID]] | None = None) -> None:
        self.items: list[EntityEvidenceMention] = []
        self.pairs = pairs or set()

    async def exists_for_pair(self, *, entity_id: UUID, evidence_id: UUID) -> bool:
        return (entity_id, evidence_id) in self.pairs

    async def add(self, mention: EntityEvidenceMention) -> None:
        self.items.append(mention)
        self.pairs.add((mention.entity_id, mention.evidence_id))


class _FakeRelationships:
    def __init__(self) -> None:
        self.items: list[Relationship] = []

    async def find_between(
        self, *, rel_type: str, first_entity_id: UUID, second_entity_id: UUID
    ) -> Relationship | None:
        endpoints = {first_entity_id, second_entity_id}
        for relationship in self.items:
            if (
                relationship.type == rel_type
                and {
                    relationship.from_entity_id,
                    relationship.to_entity_id,
                }
                == endpoints
            ):
                return relationship
        return None

    async def add(self, relationship: Relationship) -> None:
        if relationship.relationship_id is None:
            relationship.relationship_id = uuid4()
        self.items.append(relationship)


class _FakeRelationshipEvidence:
    def __init__(self) -> None:
        self.items: list[RelationshipEvidence] = []

    async def add(self, link: RelationshipEvidence) -> None:
        self.items.append(link)


class _FakeOutbox:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


class _FakeUow:
    """Only what ``record_ioc_match`` touches — a UoW that grew a field would fail here loudly."""

    def __init__(self, *, entities: _FakeEntities | None = None) -> None:
        self.entities = entities or _FakeEntities()
        self.entity_mentions = _FakeMentions()
        self.relationships = _FakeRelationships()
        self.relationship_evidence = _FakeRelationshipEvidence()
        self.outbox = _FakeOutbox()
        self.session = object()


def _entity(name: str, *, entity_type: str = "person") -> Entity:
    return Entity(
        entity_id=uuid4(),
        entity_type=entity_type,
        canonical_name=name,
        aliases=None,
        status=STATUS_PROPOSED,
        confidence=Decimal("0.700"),
        created_by_type="analyst",
        created_by_ref=uuid4(),
    )


def _event(payload: dict[str, Any]) -> EventEnvelope:
    """One envelope as the dispatcher builds it, from the real dataclass."""
    return EventEnvelope(
        event_id=uuid4(),
        event_type="threat_intel.ioc_matched",
        event_version="1.0.0",
        occurred_at=_NOW,
        aggregate_type="ioc",
        aggregate_id=_IOC_ID,
        correlation_id=uuid4(),
        causation_id=uuid4(),
        trace_id=None,
        actor_type="system",
        actor_ref=None,
        dispatch_status="processing",
        attempt_count=1,
        payload=payload,
    )


def _matched(**overrides: Any) -> EventEnvelope:
    payload: dict[str, Any] = {
        "ioc_id": str(_IOC_ID),
        "matched_evidence_id": str(_EVIDENCE_ID),
        "indicator_type": "domain",
        "confidence": "1.000",
        "matched_at": _NOW.isoformat(),
    }
    payload.update(overrides)
    return _event(payload)


def _indicator(value: str = _DOMAIN) -> IocRead:
    return IocRead(
        ioc_id=_IOC_ID,
        evidence_id=None,
        status="active",
        indicator_type="domain",
        value=value,
        threat_actor_id=None,
        collected_at=_NOW,
        first_seen=None,
        last_seen=_NOW,
    )


_DEFAULT_INDICATOR = _indicator()
_DEFAULT_LINKS = (CaseEvidenceRef(case_id=_CASE_ID, owning_user_id=_OWNER),)


async def _run(
    uow: _FakeUow,
    *,
    event: EventEnvelope | None = None,
    indicator: IocRead | None = _DEFAULT_INDICATOR,
    links: Sequence[CaseEvidenceRef] = _DEFAULT_LINKS,
) -> None:
    async def _read_ioc(ioc_id: UUID) -> IocRead | None:
        return indicator

    async def _read_links(evidence_id: UUID) -> Sequence[CaseEvidenceRef]:
        return links

    await record_ioc_match(
        event or _matched(),
        uow,  # type: ignore[arg-type]
        read_indicator=_read_ioc,
        read_case_links=_read_links,
    )


# --- the indicator entity ---------------------------------------------------
async def test_a_match_creates_a_proposed_digital_asset_entity() -> None:
    """CEM §7 types an indicator `digital_asset`; PRD FR-7.3 says machine output is `proposed`."""
    uow = _FakeUow()

    await _run(uow)

    entity = uow.entities.items[0]
    assert entity.entity_type == INDICATOR_ENTITY_TYPE
    assert entity.canonical_name == _DOMAIN
    assert entity.status == STATUS_PROPOSED
    assert entity.confidence == Decimal("1.000")
    assert entity.created_by_type == CREATED_BY_MACHINE
    assert entity.created_by_ref == _IOC_ID, "the indicator that produced it, per §5's app-refs"


async def test_the_entity_is_grounded_by_a_mentions_row() -> None:
    """CEM §13: a non-analyst entity needs ≥1 MENTIONS edge from a valid evidence object."""
    uow = _FakeUow()

    await _run(uow)

    mention = uow.entity_mentions.items[0]
    assert mention.entity_id == uow.entities.items[0].entity_id
    assert mention.evidence_id == _EVIDENCE_ID


async def test_a_known_indicator_resolves_to_the_existing_entity() -> None:
    """One indicator is one node. A second node would split one threat across two graph nodes."""
    existing = _entity(_DOMAIN, entity_type=INDICATOR_ENTITY_TYPE)
    uow = _FakeUow(entities=_FakeEntities([existing]))

    await _run(uow)

    assert len(uow.entities.items) == 1
    assert uow.entity_mentions.items[0].entity_id == existing.entity_id


async def test_an_already_recorded_sighting_writes_nothing() -> None:
    """§25.8's business key, as it lands here: `(entity_id, evidence_id)`.

    This is the layer that survives a replay — clearing the inbox and re-delivering is a documented
    operation, so the inbox claim cannot be the only thing preventing a duplicate.
    """
    existing = _entity(_DOMAIN, entity_type=INDICATOR_ENTITY_TYPE)
    uow = _FakeUow(entities=_FakeEntities([existing]))
    uow.entity_mentions.pairs.add((existing.entity_id, _EVIDENCE_ID))

    await _run(uow)

    assert uow.entity_mentions.items == []
    assert uow.relationships.items == []
    assert uow.outbox.published == []


# --- the co-occurrence edge -------------------------------------------------
async def test_a_co_mentioned_entity_becomes_an_association() -> None:
    """CEM §10's "co-occurrence within the same evidence item", typed by CEM §8.

    The edge is indicator-to-entity, never indicator-to-*evidence*: api-design.md §6's response has
    no evidence node for an edge to reach, and §8's closed vocabulary has no `ioc_matched` type.
    """
    entities = _FakeEntities()
    entities.mentioned = [_entity("Alice")]
    uow = _FakeUow(entities=entities)

    await _run(uow)

    relationship = uow.relationships.items[0]
    assert relationship.type == CO_OCCURRENCE_REL_TYPE
    assert relationship.directional is False
    assert relationship.status == STATUS_PROPOSED
    assert relationship.confidence == CO_OCCURRENCE_CONFIDENCE
    assert relationship.created_by_type == CREATED_BY_MACHINE
    assert {relationship.from_entity_id, relationship.to_entity_id} == {
        uow.entities.items[0].entity_id,
        entities.mentioned[0].entity_id,
    }
    assert [link.evidence_id for link in uow.relationship_evidence.items] == [_EVIDENCE_ID], (
        "CEM §13: the association is grounded in the evidence that shows both"
    )


async def test_an_existing_association_is_not_duplicated() -> None:
    """Direction-insensitive, because `associated_with` is symmetric (§8's "Any to Any")."""
    entities = _FakeEntities()
    other = _entity("Alice")
    entities.mentioned = [other]
    uow = _FakeUow(entities=entities)

    await _run(uow)
    first = list(uow.relationships.items)
    uow.entity_mentions.pairs.clear()  # force the handler past its early return
    await _run(uow)

    assert uow.relationships.items == first, "one association, whichever endpoint arrived first"


async def test_the_fan_out_is_capped_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crowded evidence item must not turn one match into a review queue nobody can work through.

    Bounded because `attributes` are connector-supplied and a future extraction layer will write
    many mentions per evidence item. The cap is asserted through the real constant rather than a
    literal, so raising it is a deliberate edit in one place.
    """
    monkeypatch.setattr(investigation_events, "_MAX_CO_OCCURRENCE", 3)
    entities = _FakeEntities()
    entities.mentioned = [_entity(f"Person {i}") for i in range(10)]
    uow = _FakeUow(entities=entities)

    await _run(uow)

    assert len(uow.relationships.items) == 3


# --- the published findings -------------------------------------------------
async def test_each_finding_is_announced_once_per_case() -> None:
    """§25.8's `correlation_generated`: one of `entity_id`/`relationship_id`, plus the recipient."""
    entities = _FakeEntities()
    entities.mentioned = [_entity("Alice")]
    uow = _FakeUow(entities=entities)
    second_case, second_owner = uuid4(), uuid4()

    await _run(
        uow,
        links=(
            CaseEvidenceRef(case_id=_CASE_ID, owning_user_id=_OWNER),
            CaseEvidenceRef(case_id=second_case, owning_user_id=second_owner),
        ),
    )

    assert len(uow.outbox.published) == 4, "two findings, two cases"
    kinds = sorted(event["aggregate_type"] for event in uow.outbox.published)
    assert kinds == ["entity", "entity", "relationship", "relationship"]
    for event in uow.outbox.published:
        payload = event["payload"]
        assert event["event_type"] == "investigation.correlation_generated"
        assert (payload["entity_id"] is None) != (payload["relationship_id"] is None), (
            "§25.8's payload carries one of the two, never both and never neither"
        )
        assert payload["generated_by"] == f"threat_intel.ioc_match:{_IOC_ID}"
        assert event["actor_type"] == "system"
    recipients = {
        (event["payload"]["case_id"], event["payload"]["recipient_user_id"])
        for event in uow.outbox.published
    }
    assert recipients == {(str(_CASE_ID), str(_OWNER)), (str(second_case), str(second_owner))}


async def test_the_finding_is_threaded_onto_the_workflow_that_caused_it() -> None:
    """§11: one `correlation_id` for the workflow, `causation_id` one hop back."""
    event = _matched()
    uow = _FakeUow()

    await _run(uow, event=event)

    published = uow.outbox.published[0]
    assert published["correlation_id"] == str(event.correlation_id)
    assert published["causation_id"] == str(event.event_id)


async def test_evidence_in_no_case_is_recorded_but_not_announced() -> None:
    """`case_id` is required in §25.8's payload, so there is nothing to announce — yet.

    The entity and its grounding are facts about evidence, not about a case, so they are written
    anyway; `evidence.linked_to_case` is what later places them in a case's graph.
    """
    uow = _FakeUow()

    await _run(uow, links=())

    assert len(uow.entities.items) == 1
    assert len(uow.entity_mentions.items) == 1
    assert uow.outbox.published == []


# --- payloads it cannot use -------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        {"ioc_id": "not-a-uuid"},
        {"matched_evidence_id": None},
        {"confidence": "high"},
    ],
    ids=["bad-ioc-id", "missing-evidence-id", "unparseable-confidence"],
)
async def test_a_malformed_payload_writes_nothing_and_does_not_raise(
    overrides: dict[str, Any],
) -> None:
    """A handler that raised would dead-letter an event describing something that really happened,
    then block its aggregate's whole queue under ADR-0006's per-aggregate ordering."""
    uow = _FakeUow()

    await _run(uow, event=_matched(**overrides))

    assert uow.entities.items == []
    assert uow.outbox.published == []


async def test_a_withdrawn_indicator_writes_nothing() -> None:
    """The IOC was deleted between publish and consume. Naming a node after an id nobody can
    resolve would put an unattributable entity in a legal record."""
    uow = _FakeUow()

    await _run(uow, indicator=None)

    assert uow.entities.items == []
    assert uow.outbox.published == []


# --- the parsers themselves -------------------------------------------------
def test_payload_uuid_accepts_only_a_uuid_string() -> None:
    value = uuid4()
    assert payload_uuid(str(value)) == value
    assert payload_uuid("nonsense") is None
    assert payload_uuid(None) is None
    assert payload_uuid(value) is None, "the bus carries ids as strings; anything else is a bug"


def test_payload_decimal_keeps_the_precision_the_publisher_sent() -> None:
    """A string on the wire and a ``Decimal`` in memory, because `min_confidence` compares exactly.

    Parsing through ``float`` would answer §6's boundary case differently from the `numeric` column
    the write side uses (ADR-0011 §2).
    """
    assert payload_decimal("0.500") == Decimal("0.500")
    assert str(payload_decimal("0.500")) == "0.500", "trailing precision survives"
    assert payload_decimal(1) == Decimal(1)
    assert payload_decimal(Decimal("0.25")) == Decimal("0.25")
    assert payload_decimal("high") is None
    assert payload_decimal(None) is None
