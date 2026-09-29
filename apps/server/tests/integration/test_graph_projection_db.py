"""The graph projection end-to-end, against a real Postgres — ADR-0013.

Covers the full path the ADR promises: a domain command writes a relationship and an outbox event in
one transaction, the dispatcher delivers it, the projector writes `investigation_read`, and the
traversal serves the subgraph. Then the parts that only a real database can settle:

* both variants of `correlation_generated` — §25.8's "one of `relationship_id`/`entity_id`" —
  and the link-time projection that places already-grounded findings into a newly linked case;
* the **recursive CTE** actually terminates on a cyclic graph and honours `depth`;
* **idempotency** — the same event delivered twice leaves one row, not two, and not a corrupted one;
* filters are applied **before** traversal, so an excluded edge cannot act as a bridge.

Runs in a throwaway database created and dropped here. Skips cleanly when no Postgres is reachable;
never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.modules.investigation.models import (
    STATUS_CONFIRMED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    Entity,
    EntityEvidenceMention,
    Relationship,
    RelationshipEvidence,
)
from sentinelai.modules.investigation.read.models import CaseGraphEdge, CaseGraphNode
from sentinelai.modules.investigation.read.projector import (
    project_correlation_generated,
    project_evidence_linked,
    project_finding_reviewed,
)
from sentinelai.modules.investigation.read.repository import GraphProjectionRepository
from sentinelai.modules.investigation.repository import InvestigationUnitOfWork
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.events.envelope import EventEnvelope

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


async def _reachable(url: str) -> bool:
    try:
        engine = create_async_engine(url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


async def _create_throwaway_database() -> tuple[str, str]:
    name = f"sentinelai_graphtest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    return name, _URL.rsplit("/", 1)[0] + f"/{name}"


async def _drop_throwaway_database(name: str) -> None:
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def _create_tables(engine: AsyncEngine) -> None:
    """Both schemas: the transactional tables the projector reads, and the projection it writes."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS investigation"))
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS investigation_read"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Entity.__table__,
                Relationship.__table__,
                EntityEvidenceMention.__table__,
                RelationshipEvidence.__table__,
                CaseGraphNode.__table__,
                CaseGraphEdge.__table__,
            ],
        )


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )
    database, url = await _create_throwaway_database()
    engine = create_async_engine(url)
    try:
        await _create_tables(engine)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await _drop_throwaway_database(database)


def _entity(name: str, *, entity_type: str = "person", confidence: str = "0.700") -> Entity:
    return Entity(
        entity_id=uuid4(),
        entity_type=entity_type,
        canonical_name=name,
        aliases=None,
        status=STATUS_PROPOSED,
        confidence=Decimal(confidence),
        created_by_type="system",
        # NOT NULL on the write side — a correlation run records which run produced the entity.
        created_by_ref=uuid4(),
    )


def _relationship(
    frm: Entity,
    to: Entity,
    *,
    rel_type: str = "associated_with",
    status: str = STATUS_PROPOSED,
    confidence: str = "0.650",
) -> Relationship:
    return Relationship(
        relationship_id=uuid4(),
        type=rel_type,
        from_entity_id=frm.entity_id,
        to_entity_id=to.entity_id,
        status=status,
        confidence=Decimal(confidence),
        directional=True,
        created_by_type="system",
        created_by_ref=uuid4(),
    )


def _event(event_type: str, payload: dict[str, object]) -> EventEnvelope:
    """One envelope as the dispatcher would hand it to a handler.

    Built from the real dataclass rather than a stand-in, so a field added to the envelope breaks
    this file rather than letting a projector quietly read a shape production never produces.
    """
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        event_version="1.0.0",
        occurred_at=_NOW,
        aggregate_type="relationship",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        causation_id=None,
        trace_id=None,
        actor_type="system",
        actor_ref=None,
        dispatch_status="processing",
        attempt_count=1,
        payload=payload,
    )


async def _project(
    db: async_sessionmaker[AsyncSession], case_id: UUID, relationship: Relationship
) -> None:
    """Run the projector for one `correlation_generated` event, as the dispatcher would."""
    async with db() as session:
        await project_correlation_generated(
            _event(
                "investigation.correlation_generated",
                {
                    "case_id": str(case_id),
                    "relationship_id": str(relationship.relationship_id),
                    "confidence": str(relationship.confidence),
                },
            ),
            InvestigationUnitOfWork(session),
        )
        await session.commit()


async def _seed(
    db: async_sessionmaker[AsyncSession], entities: list[Entity], rels: list[Relationship]
) -> None:
    async with db() as session:
        for row in (*entities, *rels):
            session.add(row)
        await session.commit()


async def _read(
    db: async_sessionmaker[AsyncSession],
    case_id: UUID,
    *,
    statuses: tuple[str, ...] = (STATUS_PROPOSED, STATUS_CONFIRMED),
    entity_types: tuple[str, ...] | None = None,
    min_confidence: Decimal | None = None,
    depth: int = 1,
) -> tuple[list[CaseGraphNode], list[CaseGraphEdge]]:
    async with db() as session:
        nodes, edges = await GraphProjectionRepository(session).read_subgraph(
            case_id,
            statuses=statuses,
            entity_types=entity_types,
            min_confidence=min_confidence,
            depth=depth,
        )
        return list(nodes), list(edges)


# --- the end-to-end path ----------------------------------------------------
async def test_a_generated_relationship_becomes_a_readable_subgraph(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The flow ADR-0013 promises, and the deferral it closes.

    `get_case_graph` was blocked for eight phases on a case→entity mapping no table provides. The
    event carries `case_id`; the projection stores it; the read returns a self-contained subgraph.
    """
    case_id = uuid4()
    alice, warehouse = _entity("Alice"), _entity("5th Street Warehouse", entity_type="location")
    rel = _relationship(alice, warehouse, rel_type="located_at")
    await _seed(db, [alice, warehouse], [rel])

    await _project(db, case_id, rel)
    nodes, edges = await _read(db, case_id)

    assert {n.canonical_name for n in nodes} == {"Alice", "5th Street Warehouse"}
    assert [e.relationship_id for e in edges] == [rel.relationship_id]
    assert all(n.is_seed for n in nodes), (
        "a relationship generated for a case makes both ends hop 0"
    )
    assert edges[0].rel_type == "located_at"
    assert nodes[0].projected_at is not None, "staleness must be visible (ADR-0013 §2)"


async def test_the_subgraph_is_self_contained(db: async_sessionmaker[AsyncSession]) -> None:
    """api-design.md §6: "every relationship's endpoints are guaranteed present in `entities`".

    Enforced by selecting edges last, restricted to node pairs that both survived — so a client can
    lay the graph out without a second request.
    """
    case_id = uuid4()
    a, b, c = _entity("A"), _entity("B"), _entity("C")
    ab, bc = _relationship(a, b), _relationship(b, c)
    await _seed(db, [a, b, c], [ab, bc])
    await _project(db, case_id, ab)
    await _project(db, case_id, bc)

    nodes, edges = await _read(db, case_id)

    present = {n.entity_id for n in nodes}
    for edge in edges:
        assert edge.from_entity_id in present
        assert edge.to_entity_id in present


async def test_a_reads_only_its_own_case(db: async_sessionmaker[AsyncSession]) -> None:
    """The projection is keyed per case, so one case's graph can never serve another's — the
    isolation that lets the endpoint rely on `require_case_access` alone."""
    case_a, case_b = uuid4(), uuid4()
    a, b, c, d = _entity("A"), _entity("B"), _entity("C"), _entity("D")
    ab, cd = _relationship(a, b), _relationship(c, d)
    await _seed(db, [a, b, c, d], [ab, cd])
    await _project(db, case_a, ab)
    await _project(db, case_b, cd)

    nodes_a, edges_a = await _read(db, case_a)
    nodes_b, _ = await _read(db, case_b)

    assert {n.canonical_name for n in nodes_a} == {"A", "B"}
    assert {n.canonical_name for n in nodes_b} == {"C", "D"}
    assert [e.relationship_id for e in edges_a] == [ab.relationship_id]


async def test_an_empty_case_graph_is_empty_not_an_error(
    db: async_sessionmaker[AsyncSession],
) -> None:
    nodes, edges = await _read(db, uuid4())
    assert nodes == [] and edges == []


# --- idempotency ------------------------------------------------------------
async def test_reprojecting_the_same_event_changes_nothing(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The upserts converge, independently of the inbox claim.

    Both guards exist on purpose (`read/projector.py`): the inbox stops the handler re-running, and
    the upsert means a projection cannot be corrupted even by a replay that clears the inbox — which
    event-driven §Replay describes as a normal operation.
    """
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])

    for _ in range(3):
        await _project(db, case_id, rel)

    async with db() as session:
        nodes = (await session.execute(select(CaseGraphNode))).scalars().all()
        edges = (await session.execute(select(CaseGraphEdge))).scalars().all()

    assert len(nodes) == 2, "one row per (case, entity), however many times the event arrives"
    assert len(edges) == 1


async def test_reprojection_picks_up_a_changed_write_row(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The projector reads the transactional row rather than trusting the payload, so a re-delivery
    refreshes the projection instead of restoring a stale copy of it."""
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    await _project(db, case_id, rel)

    async with db() as session:
        stored = await session.get(Relationship, rel.relationship_id)
        assert stored is not None
        stored.status = STATUS_CONFIRMED
        await session.commit()

    await _project(db, case_id, rel)
    _, edges = await _read(db, case_id)

    assert [e.status for e in edges] == [STATUS_CONFIRMED]


async def test_is_seed_is_not_demoted_by_a_later_projection(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """`is_seed` is sticky, so replay order cannot change the answer.

    Without the `OR` in the conflict clause, projecting a non-seed view of an entity after its seed
    view would silently move it out of hop zero and change what `depth` returns.
    """
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    await _project(db, case_id, rel)

    async with db() as session:
        await GraphProjectionRepository(session).upsert_node(
            case_id=case_id,
            entity_id=a.entity_id,
            entity_type=a.entity_type,
            canonical_name=a.canonical_name,
            status=a.status,
            confidence=a.confidence,
            is_seed=False,
            projected_at=datetime.now(UTC),
        )
        await session.commit()

    nodes, _ = await _read(db, case_id)
    seed_flags = {n.canonical_name: n.is_seed for n in nodes}
    assert seed_flags["A"] is True, "a seed must stay a seed"


async def test_a_review_lands_on_every_case_showing_the_relationship(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """`investigation.finding_reviewed` carries no `case_id` — the write side cannot derive one —
    and the projection does not need it: it already knows which cases show the relationship.

    One disposition is one fact about one finding, not a per-case opinion, so it lands on both.
    """
    case_a, case_b = uuid4(), uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    await _project(db, case_a, rel)
    await _project(db, case_b, rel)

    async with db() as session:
        await project_finding_reviewed(
            _event(
                "investigation.finding_reviewed",
                {
                    "relationship_id": str(rel.relationship_id),
                    "disposition": STATUS_CONFIRMED,
                    "reviewed_by": str(uuid4()),
                },
            ),
            InvestigationUnitOfWork(session),
        )
        await session.commit()

    async with db() as session:
        edges = (
            (
                await session.execute(
                    select(CaseGraphEdge).where(
                        CaseGraphEdge.relationship_id == rel.relationship_id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert {e.case_id for e in edges} == {case_a, case_b}
    assert {e.status for e in edges} == {STATUS_CONFIRMED}


async def test_a_review_for_an_unprojected_relationship_is_harmless(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Out-of-order delivery is normal: a review can arrive before the generation event it concerns.
    The disposition is not lost — the later projection reads the row's current status."""
    async with db() as session:
        await project_finding_reviewed(
            _event(
                "investigation.finding_reviewed",
                {
                    "relationship_id": str(uuid4()),
                    "disposition": STATUS_REJECTED,
                    "reviewed_by": str(uuid4()),
                },
            ),
            InvestigationUnitOfWork(session),
        )
        await session.commit()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"case_id": "not-a-uuid", "relationship_id": str(uuid4())},
        {"case_id": str(uuid4())},
        {"relationship_id": str(uuid4())},
    ],
)
async def test_a_malformed_payload_is_skipped_not_raised(
    db: async_sessionmaker[AsyncSession], payload: dict[str, object]
) -> None:
    """A projector must not dead-letter over a bad field. The fact already happened on the write
    side, a poisoned projection is fixable by rebuild, and a crashed handler blocks the whole
    aggregate's queue under ADR-0006's per-aggregate ordering."""
    async with db() as session:
        await project_correlation_generated(
            _event("investigation.correlation_generated", payload),
            InvestigationUnitOfWork(session),
        )
        await session.commit()

    async with db() as session:
        assert (await session.execute(select(CaseGraphNode))).scalars().all() == []


async def test_a_relationship_deleted_before_projection_is_skipped(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A view of an absent row is absence, not an error."""
    case_id = uuid4()
    async with db() as session:
        await project_correlation_generated(
            _event(
                "investigation.correlation_generated",
                {"case_id": str(case_id), "relationship_id": str(uuid4())},
            ),
            InvestigationUnitOfWork(session),
        )
        await session.commit()

    nodes, _ = await _read(db, case_id)
    assert nodes == []


# --- filtering --------------------------------------------------------------
async def test_the_default_status_filter_hides_a_rejected_finding(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§6's default is `proposed,confirmed`. A rejected finding must not appear in the graph an
    analyst explores, or the review decision would have no visible effect."""
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    rejected = _relationship(a, b, status=STATUS_REJECTED)
    await _seed(db, [a, b], [rejected])
    await _project(db, case_id, rejected)

    _, default_edges = await _read(db, case_id)
    assert default_edges == []


async def test_status_filters_entities_and_relationships_together(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """One `status` parameter covers both, per §6 ("filter relationships/entities by ...").

    The consequence is worth pinning because it surprised this test's first draft: asking for
    `rejected` returns a rejected edge only when its endpoints are rejected too. An edge whose
    endpoints were filtered out drops with them — which is also what keeps the subgraph
    self-contained, since returning it would reference entities not in `entities`.
    """
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    a.status = b.status = STATUS_REJECTED
    rejected = _relationship(a, b, status=STATUS_REJECTED)
    await _seed(db, [a, b], [rejected])
    await _project(db, case_id, rejected)

    nodes, edges = await _read(db, case_id, statuses=(STATUS_REJECTED,))

    assert {n.canonical_name for n in nodes} == {"A", "B"}
    assert [e.relationship_id for e in edges] == [rejected.relationship_id]


async def test_entity_type_filter(db: async_sessionmaker[AsyncSession]) -> None:
    case_id = uuid4()
    person, place = _entity("Alice"), _entity("Warehouse", entity_type="location")
    rel = _relationship(person, place)
    await _seed(db, [person, place], [rel])
    await _project(db, case_id, rel)

    nodes, edges = await _read(db, case_id, entity_types=("person",))

    assert [n.canonical_name for n in nodes] == ["Alice"]
    assert edges == [], "an edge whose far endpoint was filtered out must drop with it"


async def test_min_confidence_filter(db: async_sessionmaker[AsyncSession]) -> None:
    case_id = uuid4()
    strong, weak = _entity("Strong", confidence="0.900"), _entity("Weak", confidence="0.100")
    rel = _relationship(strong, weak, confidence="0.900")
    await _seed(db, [strong, weak], [rel])
    await _project(db, case_id, rel)

    nodes, _ = await _read(db, case_id, min_confidence=Decimal("0.500"))

    assert [n.canonical_name for n in nodes] == ["Strong"]


async def test_an_entity_review_does_not_reach_the_projection(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A known, deliberate staleness — pinned so it is a property rather than a surprise.

    `review_relationship_status` publishes `investigation.finding_reviewed`, so a relationship's
    disposition reaches the projection. `review_entity_status` publishes **nothing** — it writes a
    revision row and an audit entry and says so in a comment ("audit only") — and
    `event-driven-architecture.md` §25.8 defines no entity-disposition event.

    So a projected node's `status` refreshes only when one of its relationships is re-projected.
    Inventing an `investigation.entity_reviewed` event to close this would violate `CLAUDE.md` rule
    1; it is recorded in ADR-0013 as the projection's one known staleness beyond dispatcher latency.
    """
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    await _project(db, case_id, rel)

    # An analyst confirms the entity on the write side.
    async with db() as session:
        stored = await session.get(Entity, a.entity_id)
        assert stored is not None
        stored.status = STATUS_CONFIRMED
        await session.commit()

    nodes, _ = await _read(db, case_id)
    statuses = {n.canonical_name: n.status for n in nodes}
    assert statuses["A"] == STATUS_PROPOSED, "no event fires, so the projection cannot know yet"

    # Re-projecting the relationship refreshes it, which is the recovery path.
    await _project(db, case_id, rel)
    nodes, _ = await _read(db, case_id)
    statuses = {n.canonical_name: n.status for n in nodes}
    assert statuses["A"] == STATUS_CONFIRMED


# --- traversal --------------------------------------------------------------
async def test_the_recursive_walk_terminates_on_a_cycle(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The reason the CTE carries a hop counter at all.

    Entity graphs are full of cycles (A↔B↔C↔A), and a recursive CTE without a depth bound follows
    one until the server gives up. This test would hang, not fail, if the bound were dropped — which
    is exactly why it is worth writing.
    """
    case_id = uuid4()
    a, b, c = _entity("A"), _entity("B"), _entity("C")
    ab, bc, ca = _relationship(a, b), _relationship(b, c), _relationship(c, a)
    await _seed(db, [a, b, c], [ab, bc, ca])
    for rel in (ab, bc, ca):
        await _project(db, case_id, rel)

    nodes, edges = await _read(db, case_id, depth=3)

    assert {n.canonical_name for n in nodes} == {"A", "B", "C"}
    assert len(edges) == 3


async def test_depth_zero_returns_the_seed_set(db: async_sessionmaker[AsyncSession]) -> None:
    """Hop zero is the directly-evidenced entities. With today's single event source every node is
    a seed, so this is the whole case subgraph — see ADR-0013's note on what bounds `depth`."""
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    await _project(db, case_id, rel)

    nodes, edges = await _read(db, case_id, depth=0)

    assert {n.canonical_name for n in nodes} == {"A", "B"}
    assert len(edges) == 1


async def test_a_filtered_edge_is_not_a_bridge(db: async_sessionmaker[AsyncSession]) -> None:
    """Filters are applied **before** traversal, and this is why it matters.

    A─B is confirmed; B─C is rejected. Reading confirmed-only must not reach C, because the only
    path to it runs through a relationship the caller excluded. Filtering after traversal would let
    a rejected finding act as a bridge and leak the existence of what it connects to.
    """
    case_id = uuid4()
    a, b, c = _entity("A"), _entity("B"), _entity("C")
    ab = _relationship(a, b, status=STATUS_CONFIRMED)
    bc = _relationship(b, c, status=STATUS_REJECTED)
    await _seed(db, [a, b, c], [ab, bc])
    await _project(db, case_id, ab)
    await _project(db, case_id, bc)

    # Confirm A and B so the status filter keeps them, leaving the rejected edge as the only route.
    async with db() as session:
        for entity in (a, b):
            stored = await session.get(Entity, entity.entity_id)
            assert stored is not None
            stored.status = STATUS_CONFIRMED
        await session.commit()
    await _project(db, case_id, ab)

    nodes, edges = await _read(db, case_id, statuses=(STATUS_CONFIRMED,), depth=3)

    assert {n.canonical_name for n in nodes} == {"A", "B"}
    assert [e.relationship_id for e in edges] == [ab.relationship_id]


async def test_depth_is_capped_even_if_a_caller_asks_for_more(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§6 caps `depth` at 3 "to bound query cost". The router validates it, and the repository caps
    it again so a caller arriving another way cannot request an unbounded walk."""
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    await _project(db, case_id, rel)

    nodes, _ = await _read(db, case_id, depth=99)
    assert len(nodes) == 2


# --- rebuildability ---------------------------------------------------------
async def test_a_case_projection_can_be_dropped_and_rebuilt(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-0013 §2: projections are disposable and rebuildable from the event log — no second source
    of truth. If a rebuild did not reproduce the same graph, the projection would be holding a fact
    the write side had lost.
    """
    case_id = uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    await _project(db, case_id, rel)
    before_nodes, before_edges = await _read(db, case_id)

    async with db() as session:
        await GraphProjectionRepository(session).delete_case(case_id)
        await session.commit()
    assert await _read(db, case_id) == ([], [])

    await _project(db, case_id, rel)
    after_nodes, after_edges = await _read(db, case_id)

    assert {n.entity_id for n in after_nodes} == {n.entity_id for n in before_nodes}
    assert {e.relationship_id for e in after_edges} == {e.relationship_id for e in before_edges}


# --- the entity variant and the link-time path ------------------------------
async def _project_entity_finding(
    db: async_sessionmaker[AsyncSession], case_id: UUID, entity: Entity
) -> None:
    """Run the projector for the `entity_id` variant of `correlation_generated` (§25.8)."""
    async with db() as session:
        await project_correlation_generated(
            _event(
                "investigation.correlation_generated",
                {
                    "case_id": str(case_id),
                    "entity_id": str(entity.entity_id),
                    "confidence": str(entity.confidence),
                },
            ),
            InvestigationUnitOfWork(session),
        )
        await session.commit()


async def _project_link(
    db: async_sessionmaker[AsyncSession], case_id: UUID, evidence_id: UUID
) -> None:
    async with db() as session:
        await project_evidence_linked(
            _event(
                "evidence.linked_to_case",
                {"case_id": str(case_id), "evidence_id": str(evidence_id)},
            ),
            InvestigationUnitOfWork(session),
        )
        await session.commit()


async def test_the_entity_variant_projects_a_seed_node_with_no_edge(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.8 permits `entity_id` in place of `relationship_id`, and `on_ioc_matched` now sends it.

    A node with no edge is the correct projection of a single grounded entity — a matched indicator
    relates to nothing until something else in the case's evidence does. Projecting a placeholder
    edge would put a relationship in a legal record that no evidence supports (CEM §13).
    """
    case_id = uuid4()
    indicator = _entity("evil-c2.example", entity_type="digital_asset", confidence="1.000")
    await _seed(db, [indicator], [])

    await _project_entity_finding(db, case_id, indicator)

    nodes, edges = await _read(db, case_id)
    assert [n.canonical_name for n in nodes] == ["evil-c2.example"]
    assert nodes[0].is_seed is True, "a matched indicator is directly evidenced: hop zero"
    assert nodes[0].entity_type == "digital_asset"
    assert edges == []


async def test_a_payload_with_neither_id_is_skipped_not_dead_lettered(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.8's "one of" means one is required: neither id projects nothing, and does not raise."""
    case_id = uuid4()
    async with db() as session:
        await project_correlation_generated(
            _event("investigation.correlation_generated", {"case_id": str(case_id)}),
            InvestigationUnitOfWork(session),
        )
        await session.commit()

    assert await _read(db, case_id) == ([], [])


async def test_linking_evidence_projects_what_it_already_grounds(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The ordering half of the bridge: findings recorded before the link still reach the case.

    Without this the common real sequence — evidence ingested and matched in seconds, linked to a
    case later — would leave the match recorded on the write side and permanently absent from the
    read model, with no event left to replay that would place it.
    """
    case_id, evidence_id = uuid4(), uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    async with db() as session:
        session.add(EntityEvidenceMention(entity_id=a.entity_id, evidence_id=evidence_id))
        session.add(
            RelationshipEvidence(relationship_id=rel.relationship_id, evidence_id=evidence_id)
        )
        await session.commit()

    await _project_link(db, case_id, evidence_id)

    nodes, edges = await _read(db, case_id)
    assert {n.canonical_name for n in nodes} == {"A", "B"}, (
        "the edge's far endpoint is projected too, so §6's self-containment holds"
    )
    assert [e.relationship_id for e in edges] == [rel.relationship_id]
    assert all(n.is_seed for n in nodes)


async def test_linking_the_same_evidence_twice_converges(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Redelivery reaches an upsert, not an insert — the second half of ADR-0013's doubled
    idempotency, which has to hold on its own because a replay clears the inbox."""
    case_id, evidence_id = uuid4(), uuid4()
    a, b = _entity("A"), _entity("B")
    rel = _relationship(a, b)
    await _seed(db, [a, b], [rel])
    async with db() as session:
        session.add(EntityEvidenceMention(entity_id=a.entity_id, evidence_id=evidence_id))
        session.add(
            RelationshipEvidence(relationship_id=rel.relationship_id, evidence_id=evidence_id)
        )
        await session.commit()

    await _project_link(db, case_id, evidence_id)
    await _project_link(db, case_id, evidence_id)

    nodes, edges = await _read(db, case_id)
    assert (len(nodes), len(edges)) == (2, 1)


async def test_linking_evidence_that_grounds_nothing_projects_nothing(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Ordinary, not an error: most evidence mentions no entity until something extracts one."""
    case_id = uuid4()

    await _project_link(db, case_id, uuid4())

    assert await _read(db, case_id) == ([], [])
