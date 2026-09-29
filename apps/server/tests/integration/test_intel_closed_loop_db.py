"""The closed intelligence loop, end to end against real Postgres — §25.4/§25.8, ADR-0013.

One chain, no fakes in the middle of it: a signed `evidence.ingested` row goes into `ingestion`'s
outbox, the **real dispatcher** claims and verifies it, `threat_intel` matches the evidence against
registered IOCs and publishes a signed `threat_intel.ioc_matched`, the dispatcher verifies *that*,
`investigation` turns it into graph findings and publishes `investigation.correlation_generated`,
the projector writes `investigation_read`, and `GET /api/v1/cases/{case_id}/graph` returns the
result over HTTP. Every hop is the production code path — the same `OutboxWriter`, the same
`EventSigner`, the same `register_consumers`, the same route.

What this file exists to prove, beyond "it works once":

* the loop closes **at all** — before this increment `on_ioc_matched` was a no-op, so a match was
  recorded and announced and then went nowhere;
* it is idempotent at both layers §12 requires — redelivery (inbox) and an independent second
  publication of the same fact (the `(entity_id, evidence_id)` business key), including after a
  replay that clears the inbox;
* it survives the **other ordering**, where the evidence is matched before anyone links it to a
  case — the case that would otherwise never see the match;
* a match produces a CEM-legal graph: a `digital_asset` entity per CEM §7, an `associated_with` edge
  per CEM §8, `proposed` status per PRD FR-7.3, and ≥1 supporting evidence per CEM §13.

Runs in a throwaway database created and dropped here. Skips cleanly when no Postgres is reachable;
never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.entrypoints.http.exception_handlers import register_exception_handlers
from sentinelai.entrypoints.http.middleware import register_middleware
from sentinelai.modules.case_management.models import STATUS_OPEN, Case, CaseEvidenceLink
from sentinelai.modules.ingestion.models import Evidence
from sentinelai.modules.investigation import events as investigation_events
from sentinelai.modules.investigation.models import (
    STATUS_PROPOSED,
    Entity,
    EntityEvidenceMention,
    Relationship,
    RelationshipEvidence,
)
from sentinelai.modules.investigation.read.models import CaseGraphEdge, CaseGraphNode
from sentinelai.modules.investigation.repository import InvestigationUnitOfWork
from sentinelai.modules.investigation.router import router as investigation_router
from sentinelai.modules.threat_intel import events as threat_intel_events
from sentinelai.modules.threat_intel.models import Ioc, IocEvidenceMatch, ThreatActorProfile
from sentinelai.modules.threat_intel.repository import ThreatIntelUnitOfWork
from sentinelai.modules.threat_intel.schemas import IocCreate
from sentinelai.modules.threat_intel.service import ThreatIntelService
from sentinelai.platform.auth.dependencies import (
    CurrentUser,
    get_case_access_checker,
    get_current_user,
)
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.db.base import Base
from sentinelai.platform.db.session import get_session
from sentinelai.platform.events.dispatcher import VERIFY_STRICT, EventDispatcher
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import get_inbox_table
from sentinelai.platform.events.outbox import OutboxWriter, get_outbox_table
from sentinelai.platform.events.signing import EventSigner
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_SCHEMAS = ("platform", "ingestion", "threat_intel", "case_management", "investigation")
_ALL_SCHEMAS = (*_SCHEMAS, "investigation_read")

_OWNER = uuid4()
_ACTOR = CurrentUser(user_id=uuid4(), roles=("investigator",))
_DOMAIN = "evil-c2.example"
_SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


# --- database plumbing ------------------------------------------------------
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
    name = f"sentinelai_loop_{uuid.uuid4().hex[:8]}"
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
    """Five module schemas plus the projection — every table the loop actually touches.

    Created from the ORM metadata rather than by running Alembic so the failure mode is a missing
    table here, not a migration ordering problem three modules away. The constraints that matter to
    this file (``uq_entity_mention_pair``, ``uq_ioc_evidence_match_pair``) are declared on the
    models precisely so ``create_all`` reproduces them.
    """
    async with engine.begin() as conn:
        for schema in _ALL_SCHEMAS:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                AuditLog.__table__,
                Evidence.__table__,
                ThreatActorProfile.__table__,
                Ioc.__table__,
                IocEvidenceMatch.__table__,
                Case.__table__,
                CaseEvidenceLink.__table__,
                Entity.__table__,
                Relationship.__table__,
                RelationshipEvidence.__table__,
                EntityEvidenceMention.__table__,
                CaseGraphNode.__table__,
                CaseGraphEdge.__table__,
            ],
        )
        for schema in _SCHEMAS[1:]:
            await conn.run_sync(get_outbox_table(schema).create, checkfirst=True)
            await conn.run_sync(get_inbox_table(schema).create, checkfirst=True)


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


# --- the production wiring, assembled --------------------------------------
def _dispatcher(db: async_sessionmaker[AsyncSession]) -> EventDispatcher:
    """The real relay, in strict signature mode, with both modules' real registrations.

    ``VERIFY_STRICT`` is the point of using the real thing: an event whose signature does not verify
    never reaches a handler and is quarantined (ADR-0007 §2). So every assertion below that a
    handler ran is also an assertion that the event it ran on was **verified**, not merely present.
    """
    dispatcher = EventDispatcher(
        db,
        poll_schemas=_SCHEMAS[1:],
        lease_seconds=0,
        signer=EventSigner(kms_for_tests()),
        signature_mode=VERIFY_STRICT,
    )
    threat_intel_events.register_consumers(dispatcher)
    investigation_events.register_consumers(dispatcher)
    return dispatcher


async def _drain(dispatcher: EventDispatcher, *, passes: int = 8) -> None:
    """Poll until the bus is quiet.

    Several passes because the loop is three hops long and each hop publishes into the *next*
    schema's outbox: one pass moves `evidence.ingested`, the next `ioc_matched`, the next
    `correlation_generated`. A loop that needed more passes than this would mean an unexpected hop.
    """
    for _ in range(passes):
        if await dispatcher._poll_once() == 0:
            return


def _app(db: async_sessionmaker[AsyncSession], *, allow_access: bool = True) -> FastAPI:
    """Just the investigation router, on the same throwaway database.

    The graph read is served by the real route, the real service and the real projection query. The
    only overrides are the two things a request cannot bring with it here: the authenticated
    principal, and the case-access checker whose real implementation lives in `case_management` and
    is proven in `test_case_access_db.py`.
    """

    class _Checker:
        async def user_has_access(self, case_id: object, user_id: object) -> bool:
            return allow_access

    application = FastAPI()
    register_middleware(application)
    register_exception_handlers(application)
    application.include_router(investigation_router)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            yield session

    application.dependency_overrides[get_current_user] = lambda: _ACTOR
    application.dependency_overrides[get_kms] = lambda: kms_for_tests()
    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_case_access_checker] = lambda: _Checker()
    return application


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- fixtures for the scenario ---------------------------------------------
def _service(session: AsyncSession) -> ThreatIntelService:
    return ThreatIntelService(
        ThreatIntelUnitOfWork(session, kms=kms_for_tests()), kms=kms_for_tests()
    )


async def _seed_case(db: async_sessionmaker[AsyncSession]) -> UUID:
    case_id = uuid4()
    async with db() as session:
        session.add(
            Case(
                case_id=case_id,
                title="Operation Nightjar",
                description=None,
                status=STATUS_OPEN,
                owning_user_id=_OWNER,
                created_at=_NOW,
            )
        )
        await session.commit()
    return case_id


async def _seed_evidence(db: async_sessionmaker[AsyncSession], attributes: dict[str, Any]) -> UUID:
    """One validated evidence row whose ``attributes`` are what the matcher will tokenize.

    A real row rather than an injected reader: the consumer's default path is
    `ingestion.public.read_evidence_attributes`, and the §181 fetch is part of what this file is
    proving works end to end.
    """
    evidence_id = uuid4()
    async with db() as session:
        session.add(
            Evidence(
                evidence_id=evidence_id,
                schema_version="1.2.0",
                category="threat_intelligence",
                artifact_type="malware_report",
                title="Intrusion report",
                source={"system": "analyst-upload", "collector_id": "examiner:n.doe"},
                collected_at=_NOW,
                ingested_at=_NOW,
                attributes=attributes,
                confidence=Decimal("1.000"),
                status="validated",
                retention_policy_ref="standard-7y",
            )
        )
        await session.commit()
    return evidence_id


async def _link_evidence(
    db: async_sessionmaker[AsyncSession], case_id: UUID, evidence_id: UUID
) -> None:
    """Link evidence to a case exactly as `CaseService.link_evidence` does: row + signed event."""
    async with db() as session:
        session.add(
            CaseEvidenceLink(
                case_id=case_id,
                evidence_id=evidence_id,
                linked_by_user_id=_ACTOR.user_id,
                linked_at=_NOW,
            )
        )
        await OutboxWriter(
            session, schema="case_management", signer=EventSigner(kms_for_tests())
        ).publish(
            event_type="evidence.linked_to_case",
            aggregate_type="case",
            aggregate_id=case_id,
            payload={"case_id": str(case_id), "evidence_id": str(evidence_id)},
            correlation_id=str(uuid4()),
            actor_type="user",
            actor_ref=_ACTOR.user_id,
        )
        await session.commit()


async def _register_iocs(db: async_sessionmaker[AsyncSession]) -> list[UUID]:
    """Two indicators through the real audited service — the analyst-facing half of §4.4."""
    async with db() as session:
        service = _service(session)
        domain = await service.register_ioc(
            IocCreate(indicator_type="domain", value=_DOMAIN), _ACTOR, str(uuid4())
        )
        digest = await service.register_ioc(
            IocCreate(indicator_type="hash_sha256", value=_SHA256), _ACTOR, str(uuid4())
        )
        await session.commit()
        return [domain.ioc_id, digest.ioc_id]


async def _announce_ingested(
    db: async_sessionmaker[AsyncSession], evidence_id: UUID
) -> tuple[UUID, UUID]:
    """Publish `evidence.ingested` the way `ingestion` publishes it: its outbox, signed.

    Returns ``(correlation_id, event_id)`` so the causal chain §11 draws can be asserted rather
    than assumed.
    """
    correlation_id = uuid4()
    async with db() as session:
        await OutboxWriter(
            session, schema="ingestion", signer=EventSigner(kms_for_tests())
        ).publish(
            event_type="evidence.ingested",
            aggregate_type="evidence",
            aggregate_id=evidence_id,
            payload={
                "evidence_id": str(evidence_id),
                "category": "threat_intelligence",
                "artifact_type": "malware_report",
                "collected_at": _NOW.isoformat(),
                "collector_user_id": str(_ACTOR.user_id),
            },
            correlation_id=str(correlation_id),
            actor_type="user",
            actor_ref=_ACTOR.user_id,
        )
        await session.commit()
        table = get_outbox_table("ingestion")
        row = (
            await session.execute(
                select(table.c.event_id).where(
                    table.c.event_type == "evidence.ingested",
                    table.c.aggregate_id == evidence_id,
                )
            )
        ).scalar_one()
    return correlation_id, UUID(str(row))


async def _outbox(db: async_sessionmaker[AsyncSession], schema: str) -> list[dict[str, Any]]:
    table = get_outbox_table(schema)
    async with db() as session:
        rows = (await session.execute(select(table))).mappings().all()
    return [dict(row) for row in rows]


async def _graph_rows(
    db: async_sessionmaker[AsyncSession], case_id: UUID
) -> tuple[list[CaseGraphNode], list[CaseGraphEdge]]:
    async with db() as session:
        nodes = (
            (await session.execute(select(CaseGraphNode).where(CaseGraphNode.case_id == case_id)))
            .scalars()
            .all()
        )
        edges = (
            (await session.execute(select(CaseGraphEdge).where(CaseGraphEdge.case_id == case_id)))
            .scalars()
            .all()
        )
    return list(nodes), list(edges)


async def _run_loop(
    db: async_sessionmaker[AsyncSession], *, link_first: bool = True
) -> tuple[UUID, UUID, UUID]:
    """The whole scenario: a case, two IOCs, one evidence item naming both, and a full drain.

    ``link_first`` chooses the ordering. Linking first is the tidy path the task describes; linking
    afterwards is the common real one, and the two must end in the same graph.
    """
    case_id = await _seed_case(db)
    await _register_iocs(db)
    evidence_id = await _seed_evidence(
        db,
        {
            "c2_domain": _DOMAIN,
            "dropper_sha256": _SHA256,
            "analyst_note": "beaconing observed",
        },
    )
    if link_first:
        await _link_evidence(db, case_id, evidence_id)
    _, ingested_event_id = await _announce_ingested(db, evidence_id)
    await _drain(_dispatcher(db))
    if not link_first:
        await _link_evidence(db, case_id, evidence_id)
        await _drain(_dispatcher(db))
    return case_id, evidence_id, ingested_event_id


# --- the loop ---------------------------------------------------------------
async def test_a_matched_indicator_becomes_a_graph_node_and_edge(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The whole cycle: ingest → match → finding → projection, all through the real dispatcher.

    The edge is between the two **indicators**, not between an indicator and the evidence. CEM §11
    gives the graph one node type (`Entity`) and api-design.md §6's response is
    `{ entities, relationships }`, so there is no evidence node for an edge to reach — what the
    evidence grounds is that these two indicators co-occur in it (CEM §10, typed `associated_with`
    by CEM §8).
    """
    case_id, evidence_id, _ = await _run_loop(db)

    async with db() as session:
        matches = (await session.execute(select(IocEvidenceMatch))).scalars().all()
        entities = (await session.execute(select(Entity))).scalars().all()
        mentions = (await session.execute(select(EntityEvidenceMention))).scalars().all()
        relationships = (await session.execute(select(Relationship))).scalars().all()
        supporting = (await session.execute(select(RelationshipEvidence))).scalars().all()

    assert len(matches) == 2, "both indicators are present in the evidence"
    assert {e.canonical_name for e in entities} == {_DOMAIN, _SHA256}
    assert {e.entity_type for e in entities} == {"digital_asset"}, "CEM §7's type for an indicator"
    assert {e.status for e in entities} == {STATUS_PROPOSED}, "machine output is never confirmed"
    assert {(m.evidence_id) for m in mentions} == {evidence_id}
    assert len(mentions) == 2, "one MENTIONS edge per indicator (CEM §11)"

    assert len(relationships) == 1, "two co-occurring indicators are one association, not two"
    edge = relationships[0]
    assert edge.type == "associated_with", "CEM §8's closed vocabulary has no `ioc_matched` type"
    assert edge.directional is False
    assert edge.status == STATUS_PROPOSED
    assert {e.entity_id for e in entities} == {edge.from_entity_id, edge.to_entity_id}
    assert [s.evidence_id for s in supporting] == [evidence_id], "CEM §13: ≥1 supporting evidence"

    nodes, edges = await _graph_rows(db, case_id)
    assert {n.canonical_name for n in nodes} == {_DOMAIN, _SHA256}
    assert all(n.is_seed for n in nodes), "a matched indicator is directly evidenced: hop zero"
    assert [e.relationship_id for e in edges] == [edge.relationship_id]


async def test_the_graph_endpoint_returns_the_match(db: async_sessionmaker[AsyncSession]) -> None:
    """`GET /api/v1/cases/{case_id}/graph` over the real route, against the real projection.

    §6's self-containment guarantee is the assertion that matters: "every relationship's endpoints
    are guaranteed present in `entities`". An edge whose endpoints the caller cannot resolve is
    unusable in the console the endpoint exists to serve.
    """
    case_id, _, _ = await _run_loop(db)

    async with await _client(_app(db)) as client:
        response = await client.get(f"/api/v1/cases/{case_id}/graph")

    assert response.status_code == 200
    data = response.json()["data"]
    assert {e["canonical_name"] for e in data["entities"]} == {_DOMAIN, _SHA256}
    assert {e["entity_type"] for e in data["entities"]} == {"digital_asset"}
    assert len(data["relationships"]) == 1
    relationship = data["relationships"][0]
    assert relationship["type"] == "associated_with"
    endpoints = {relationship["from_entity_id"], relationship["to_entity_id"]}
    assert endpoints <= {e["entity_id"] for e in data["entities"]}


async def test_the_graph_endpoint_still_enforces_case_access(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A closed loop must not become a way around ADR-0017: the ABAC check still gates the read."""
    case_id, _, _ = await _run_loop(db)

    async with await _client(_app(db, allow_access=False)) as client:
        response = await client.get(f"/api/v1/cases/{case_id}/graph")

    assert response.status_code == 403


# --- the event chain --------------------------------------------------------
async def test_the_published_chain_is_signed_verified_and_causally_threaded(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Every hop verified under `EVENT_ROOT`, and §11's causal chain reconstructable.

    The dispatcher runs in ``VERIFY_STRICT``, so a `dispatched` row is a row whose signature
    verified — a forged one would be `dead_letter` and its handler never invoked. §11's worked
    example is this exact chain, and ``causation_id`` pointing one hop back is what makes it
    walkable.
    """
    case_id, _, ingested_event_id = await _run_loop(db)

    matched = [r for r in await _outbox(db, "threat_intel") if r["event_type"].endswith("matched")]
    generated = [
        r
        for r in await _outbox(db, "investigation")
        if r["event_type"] == "investigation.correlation_generated"
    ]

    assert len(matched) == 2
    assert all(row["signature"] is not None for row in matched)
    assert {row["dispatch_status"] for row in matched} == {"dispatched"}
    assert all(row["payload"]["indicator_type"] for row in matched), "§25's required field"
    assert all(row["payload"]["matched_at"] for row in matched), "§25's required field"
    assert {str(row["causation_id"]) for row in matched} == {str(ingested_event_id)}

    # Two entity findings and one relationship finding, one event each for the single linked case.
    assert len(generated) == 3
    assert all(row["signature"] is not None for row in generated)
    assert {row["dispatch_status"] for row in generated} == {"dispatched"}
    assert {row["payload"]["case_id"] for row in generated} == {str(case_id)}
    assert {row["payload"]["recipient_user_id"] for row in generated} == {str(_OWNER)}, (
        "§25.8's recipient is the case owner, who notification alerts"
    )
    assert all(
        row["payload"]["generated_by"].startswith("threat_intel.ioc_match:") for row in generated
    )
    assert {row["actor_type"] for row in generated} == {"system"}
    assert {row["actor_ref"] for row in generated} == {None}, "no principal made this finding"
    kinds = sorted(row["aggregate_type"] for row in generated)
    assert kinds == ["entity", "entity", "relationship"]
    # One `correlation_id` threads the whole workflow (§11); `causation_id` moves one hop per event.
    assert len({str(row["correlation_id"]) for row in (*matched, *generated)}) == 1
    assert {str(row["causation_id"]) for row in generated} == {
        str(row["event_id"]) for row in matched
    }


async def test_a_match_is_not_audited(db: async_sessionmaker[AsyncSession]) -> None:
    """`platform.audit_log` records what principals did, and no principal made this finding.

    The two IOC registrations *are* audited — those were user actions. The match, the entity, the
    association and the projection are the platform's own observations, and attributing them to
    whoever happened to upload the evidence would misreport who decided what in a legal record.
    """
    await _run_loop(db)

    async with db() as session:
        actions = [row.action for row in (await session.execute(select(AuditLog))).scalars().all()]

    assert sorted(actions) == ["ioc_registered", "ioc_registered"]


# --- idempotency ------------------------------------------------------------
async def test_redelivery_of_the_same_match_changes_nothing(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Layer 1 of §12: the Inbox claim absorbs a redelivered `event_id`."""
    case_id, _, _ = await _run_loop(db)
    before = await _graph_rows(db, case_id)

    async with db() as session:
        rows = (await session.execute(select(get_outbox_table("threat_intel")))).mappings().all()
        # Built with the dispatcher's own `from_row`, so the handler sees exactly the envelope
        # production hands it — including the `event_id` the inbox claim turns on.
        for row in (r for r in rows if r["event_type"] == "threat_intel.ioc_matched"):
            await investigation_events.on_ioc_matched(
                EventEnvelope.from_row(row),
                InvestigationUnitOfWork(session, kms=kms_for_tests()),
            )
        await session.commit()

    async with db() as session:
        entities = (await session.execute(select(Entity))).scalars().all()
        mentions = (await session.execute(select(EntityEvidenceMention))).scalars().all()
        relationships = (await session.execute(select(Relationship))).scalars().all()
    nodes, edges = await _graph_rows(db, case_id)

    assert len(entities) == 2
    assert len(mentions) == 2
    assert len(relationships) == 1
    assert (len(nodes), len(edges)) == (len(before[0]), len(before[1]))


async def test_a_replay_that_clears_the_inbox_still_does_not_duplicate(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Layer 2 of §12, which is the layer that matters here.

    Clearing the inbox and re-delivering is a documented operation (§ Replay) — it is how an
    operator re-runs a fixed handler over history. With the inbox empty the claim cannot help, so
    the `(entity_id, evidence_id)` business key is the only thing standing between a replay and a
    second copy of every indicator in the graph.
    """
    case_id, _, _ = await _run_loop(db)

    async with db() as session:
        await session.execute(delete(get_inbox_table("investigation")))
        await session.execute(delete(get_inbox_table("threat_intel")))
        await session.execute(
            get_outbox_table("threat_intel")
            .update()
            .values(dispatch_status="pending", attempt_count=0, last_attempted_at=None)
        )
        await session.commit()

    await _drain(_dispatcher(db))

    async with db() as session:
        entities = (await session.execute(select(Entity))).scalars().all()
        mentions = (await session.execute(select(EntityEvidenceMention))).scalars().all()
        relationships = (await session.execute(select(Relationship))).scalars().all()
    nodes, edges = await _graph_rows(db, case_id)

    assert len(entities) == 2, "a replay must not invent a second node per indicator"
    assert len(mentions) == 2
    assert len(relationships) == 1
    assert (len(nodes), len(edges)) == (2, 1)


async def test_the_mention_pair_is_unique_in_the_database(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The half the handler's pre-check cannot provide: two dispatchers can both pass it.

    A duplicate MENTIONS row would double-count the evidence grounding a finding under CEM §13, so
    the guarantee is enforced by the database and not only by the code that usually gets there
    first.
    """
    await _run_loop(db)

    async with db() as session:
        mention = (await session.execute(select(EntityEvidenceMention))).scalars().first()
        assert mention is not None
        session.add(
            EntityEvidenceMention(entity_id=mention.entity_id, evidence_id=mention.evidence_id)
        )
        with pytest.raises(IntegrityError):
            await session.flush()


# --- the other ordering -----------------------------------------------------
async def test_a_match_before_the_link_still_reaches_the_case_graph(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The ordering that matters in practice, and the reason `on_evidence_linked_to_case` is real.

    Evidence is ingested and scanned within seconds; an analyst links it to a case later. At match
    time there is no case to project into and `correlation_generated` has no case-less form, so
    without the link-time projection every one of those matches would be invisible in the case
    forever — recorded on the write side, absent from the read model, with no event left to replay.
    """
    case_id, _, _ = await _run_loop(db, link_first=False)

    nodes, edges = await _graph_rows(db, case_id)

    assert {n.canonical_name for n in nodes} == {_DOMAIN, _SHA256}
    assert len(edges) == 1
    assert all(n.is_seed for n in nodes)


async def test_a_match_on_unlinked_evidence_is_recorded_without_a_finding(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """No case, no `correlation_generated` — but the entity and its grounding are still facts.

    `case_id` is required in §25.8's payload, so a finding cannot be announced for evidence that
    belongs to no case. What the platform knows is nonetheless recorded, which is what lets the
    link-time projection place it later.
    """
    await _register_iocs(db)
    evidence_id = await _seed_evidence(db, {"c2_domain": _DOMAIN})
    await _announce_ingested(db, evidence_id)
    await _drain(_dispatcher(db))

    async with db() as session:
        entities = (await session.execute(select(Entity))).scalars().all()
        mentions = (await session.execute(select(EntityEvidenceMention))).scalars().all()
        nodes = (await session.execute(select(CaseGraphNode))).scalars().all()
    generated = [
        r
        for r in await _outbox(db, "investigation")
        if r["event_type"] == "investigation.correlation_generated"
    ]

    assert [e.canonical_name for e in entities] == [_DOMAIN]
    assert len(mentions) == 1
    assert generated == [], "nothing to announce without a case to announce it for"
    assert nodes == [], "and nothing projected, because a projection row is per case"


async def test_a_second_evidence_item_is_a_second_sighting_of_one_entity(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Entity resolution: one indicator is one node however many evidence items name it.

    Two nodes for one domain would split an investigator's view of the same threat in half, and
    would make the graph's answer to "where has this appeared" depend on how many times it appeared.
    """
    case_id, first_evidence, _ = await _run_loop(db)
    second_evidence = await _seed_evidence(db, {"c2_domain": _DOMAIN})
    await _link_evidence(db, case_id, second_evidence)
    await _announce_ingested(db, second_evidence)
    await _drain(_dispatcher(db))

    async with db() as session:
        domains = (
            (await session.execute(select(Entity).where(Entity.canonical_name == _DOMAIN)))
            .scalars()
            .all()
        )
        mentions = (
            (
                await session.execute(
                    select(EntityEvidenceMention).where(
                        EntityEvidenceMention.entity_id == domains[0].entity_id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(domains) == 1, "one indicator, one node"
    assert {m.evidence_id for m in mentions} == {first_evidence, second_evidence}


async def test_a_withdrawn_indicator_projects_nothing_rather_than_failing(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The IOC is gone between publish and consume: skip, log, do not dead-letter.

    The match already happened and is recorded in `threat_intel`. Raising here would dead-letter an
    event describing something true and then block its aggregate's queue under ADR-0006's
    per-aggregate ordering — and naming the node after an id nobody can resolve would put an
    unattributable entity in a legal record.
    """
    case_id = await _seed_case(db)
    evidence_id = await _seed_evidence(db, {"c2_domain": _DOMAIN})
    await _link_evidence(db, case_id, evidence_id)
    event = EventEnvelope(
        event_id=uuid4(),
        event_type="threat_intel.ioc_matched",
        event_version="1.0.0",
        occurred_at=_NOW,
        aggregate_type="ioc",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        causation_id=None,
        trace_id=None,
        actor_type="system",
        actor_ref=None,
        dispatch_status="processing",
        attempt_count=1,
        payload={
            "ioc_id": str(uuid4()),
            "matched_evidence_id": str(evidence_id),
            "indicator_type": "domain",
            "confidence": "1.000",
            "matched_at": _NOW.isoformat(),
        },
    )

    async with db() as session:
        await investigation_events.on_ioc_matched(
            event, InvestigationUnitOfWork(session, kms=kms_for_tests())
        )
        await session.commit()

    async with db() as session:
        assert (await session.execute(select(Entity))).scalars().all() == []
    assert await _graph_rows(db, case_id) == ([], [])
