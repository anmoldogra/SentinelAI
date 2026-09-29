"""The correlation run, end to end against real Postgres — api-design.md §6, §25.8, ADR-0013.

One chain, no fakes in the middle of it: the HTTP trigger creates a `queued` row and enqueues, the
**real job function** claims it, reads the case's linked evidence through `case_management.public`
and its content through `ingestion.public`, extracts with the real adapter, writes `proposed`
entities and relationships, publishes signed `investigation.correlation_generated` per finding and
`correlation_run_completed` at the end; the **real dispatcher** verifies and relays those into the
projector, and `GET /api/v1/cases/{case_id}/graph` returns the result over HTTP.

What this file exists to prove, beyond "it runs once":

* the run drives `correlation_runs` through `queued -> running -> completed` with real clocks and a
  real count, which is what `GET /correlation-runs/{run_id}` is reading;
* every finding is born `proposed` (PRD FR-7.3, CEM §10) and grounded — a MENTIONS edge per entity,
  a `relationship_evidence` row per edge (CEM §1.6/§13);
* the findings **project** into the case graph, so an analyst can actually see them;
* a second run over unchanged evidence converges: no new rows, no new events, count zero;
* the failure path leaves the row `failed` and visible rather than stuck `running`, and cancellation
  stops at a batch boundary keeping what it found;
* `quarantined` evidence is never read.

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
from sqlalchemy import select, text, update
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
from sentinelai.modules.investigation.extraction import EvidenceRecord, Extraction
from sentinelai.modules.investigation.jobs import run_correlation
from sentinelai.modules.investigation.models import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    STATUS_PROPOSED,
    CorrelationRun,
    Entity,
    EntityEvidenceMention,
    Relationship,
    RelationshipEvidence,
)
from sentinelai.modules.investigation.read.models import CaseGraphEdge, CaseGraphNode
from sentinelai.modules.investigation.router import router as investigation_router
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
from sentinelai.platform.events.inbox import get_inbox_table
from sentinelai.platform.events.outbox import get_outbox_table
from sentinelai.platform.events.signing import EventSigner
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_SCHEMAS = ("platform", "ingestion", "case_management", "investigation")
_ALL_SCHEMAS = (*_SCHEMAS, "investigation_read")

_OWNER = uuid4()
_ACTOR = CurrentUser(user_id=uuid4(), roles=("investigator",))
_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_DOMAIN = "evil-c2.example"
_EMAIL = "suspect.01@mail.example"
_SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
# The outbox's `correlation_id` column is a real `uuid`, so a workflow id is a UUID string —
# not a readable label. Held as constants so the assertions can compare against the parsed form.
_CORRELATION = str(uuid4())
_CORRELATION_2 = str(uuid4())


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
    name = f"sentinelai_corr_{uuid.uuid4().hex[:8]}"
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
    """Every table the run actually touches, from the ORM metadata.

    Created from metadata rather than by running Alembic so a failure here is a missing table, not a
    migration ordering problem three modules away. `uq_entity_mention_pair` is declared on the model
    precisely so `create_all` reproduces the constraint the convergence assertions depend on.
    """
    async with engine.begin() as conn:
        for schema in _ALL_SCHEMAS:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                AuditLog.__table__,
                Evidence.__table__,
                Case.__table__,
                CaseEvidenceLink.__table__,
                Entity.__table__,
                Relationship.__table__,
                RelationshipEvidence.__table__,
                EntityEvidenceMention.__table__,
                CorrelationRun.__table__,
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
class _Queue:
    """A task queue that records rather than dispatches, so the HTTP trigger is testable.

    The job itself is invoked directly by the tests below — running a real arq worker would prove
    Redis works, not that the correlation engine does.
    """

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((function, args))


def _ctx(db: async_sessionmaker[AsyncSession], extractor: Any = None) -> dict[str, Any]:
    """The worker context `on_startup` builds, narrowed to what this job reads."""
    ctx: dict[str, Any] = {"session_factory": db, "kms": kms_for_tests()}
    if extractor is not None:
        ctx["evidence_extractor"] = extractor
    return ctx


def _dispatcher(db: async_sessionmaker[AsyncSession]) -> EventDispatcher:
    """The real relay, in strict signature mode, with investigation's real registrations.

    ``VERIFY_STRICT`` is the point of using the real thing: an event whose signature does not verify
    never reaches a handler (ADR-0007 §2). So every assertion that the projection happened is also
    an assertion that the event it happened on was verified, not merely present.
    """
    dispatcher = EventDispatcher(
        db,
        poll_schemas=("investigation",),
        lease_seconds=0,
        signer=EventSigner(kms_for_tests()),
        signature_mode=VERIFY_STRICT,
    )
    investigation_events.register_consumers(dispatcher)
    return dispatcher


async def _drain(dispatcher: EventDispatcher, *, passes: int = 4) -> None:
    for _ in range(passes):
        if await dispatcher._poll_once() == 0:
            return


def _app(db: async_sessionmaker[AsyncSession], queue: _Queue) -> FastAPI:
    """Just the investigation router, on the same throwaway database."""

    class _Checker:
        async def user_has_access(self, case_id: object, user_id: object) -> bool:
            return True

    async def _session() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            yield session

    application = FastAPI()
    register_middleware(application)
    register_exception_handlers(application)
    application.include_router(investigation_router)
    application.state.task_queue = queue
    application.dependency_overrides[get_current_user] = lambda: _ACTOR
    application.dependency_overrides[get_kms] = lambda: kms_for_tests()
    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_case_access_checker] = lambda: _Checker()
    return application


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- scenario fixtures ------------------------------------------------------
async def _seed_case(db: async_sessionmaker[AsyncSession]) -> UUID:
    case_id = uuid4()
    async with db() as session:
        session.add(
            Case(
                case_id=case_id,
                title="Operation Kingfisher",
                description=None,
                status=STATUS_OPEN,
                owning_user_id=_OWNER,
                created_at=_NOW,
            )
        )
        await session.commit()
    return case_id


async def _seed_evidence(
    db: async_sessionmaker[AsyncSession],
    *,
    title: str = "Intrusion report",
    attributes: dict[str, Any] | None = None,
    status: str = "validated",
) -> UUID:
    evidence_id = uuid4()
    async with db() as session:
        session.add(
            Evidence(
                evidence_id=evidence_id,
                schema_version="1.2.0",
                category="digital_forensics",
                artifact_type="chat_message",
                title=title,
                source={"system": "analyst-upload", "collector_id": "examiner:n.doe"},
                collected_at=_NOW,
                ingested_at=_NOW,
                attributes=attributes if attributes is not None else {},
                confidence=Decimal("1.000"),
                status=status,
                retention_policy_ref="standard-7y",
            )
        )
        await session.commit()
    return evidence_id


async def _link(db: async_sessionmaker[AsyncSession], case_id: UUID, evidence_id: UUID) -> None:
    async with db() as session:
        session.add(
            CaseEvidenceLink(
                case_id=case_id,
                evidence_id=evidence_id,
                linked_by_user_id=_ACTOR.user_id,
                linked_at=_NOW,
            )
        )
        await session.commit()


async def _queue_run(db: async_sessionmaker[AsyncSession], case_id: UUID) -> UUID:
    """A `queued` run row, exactly as `trigger_correlation_run` writes one."""
    run_id = uuid4()
    async with db() as session:
        session.add(
            CorrelationRun(
                run_id=run_id,
                case_id=case_id,
                status=RUN_QUEUED,
                started_at=None,
                completed_at=None,
                findings_generated_count=0,
                cancellation_requested=False,
            )
        )
        await session.commit()
    return run_id


async def _seed_correlatable_case(
    db: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID]:
    """A case with one evidence item naming a domain, an address and a hash, and a queued run."""
    case_id = await _seed_case(db)
    evidence_id = await _seed_evidence(
        db,
        title=f"Beacon to {_DOMAIN}",
        attributes={"sender": _EMAIL, "payload": {"sha256": _SHA256}},
    )
    await _link(db, case_id, evidence_id)
    return case_id, evidence_id, await _queue_run(db, case_id)


async def _rows(db: async_sessionmaker[AsyncSession], model: Any) -> list[Any]:
    async with db() as session:
        return list((await session.execute(select(model))).scalars().all())


async def _events(db: async_sessionmaker[AsyncSession], schema: str = "investigation") -> list[Any]:
    table = get_outbox_table(schema)
    async with db() as session:
        return list((await session.execute(select(table))).mappings().all())


# --- the run, end to end ----------------------------------------------------
async def test_a_run_walks_the_state_machine_and_records_its_count(db) -> None:
    """api-design.md §6's `queued -> running -> completed`, with the clocks a poller reads."""
    _case_id, _evidence_id, run_id = await _seed_correlatable_case(db)

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    [run] = await _rows(db, CorrelationRun)
    assert run.status == RUN_COMPLETED
    assert run.started_at is not None and run.completed_at is not None
    assert run.started_at <= run.completed_at
    # Three identifiers -> three entities, plus the complete pairwise co-occurrence (3 choose 2).
    assert run.findings_generated_count == 6


async def test_every_finding_is_born_proposed_and_grounded(db) -> None:
    """PRD FR-7.3 and CEM §10: never a confirmed fact. CEM §1.6/§13: never without evidence."""
    _case_id, evidence_id, run_id = await _seed_correlatable_case(db)

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    entities = await _rows(db, Entity)
    relationships = await _rows(db, Relationship)
    mentions = await _rows(db, EntityEvidenceMention)
    supporting = await _rows(db, RelationshipEvidence)

    assert {entity.status for entity in entities} == {STATUS_PROPOSED}
    assert {entity.created_by_type for entity in entities} == {"ai"}
    assert {entity.created_by_ref for entity in entities} == {run_id}
    assert {rel.status for rel in relationships} == {STATUS_PROPOSED}
    assert {mention.evidence_id for mention in mentions} == {evidence_id}
    assert len(mentions) == len(entities)
    assert {link.evidence_id for link in supporting} == {evidence_id}
    assert {link.relationship_id for link in supporting} == {
        rel.relationship_id for rel in relationships
    }


async def test_the_identifiers_land_on_their_cem_7_types(db) -> None:
    _case_id, _evidence_id, run_id = await _seed_correlatable_case(db)

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    by_name = {entity.canonical_name: entity.entity_type for entity in await _rows(db, Entity)}
    assert by_name == {
        _DOMAIN: "digital_asset",
        _EMAIL: "account",
        _SHA256: "digital_asset",
    }


async def test_the_run_publishes_a_finding_per_result_and_one_completion(db) -> None:
    """§25.8: `correlation_generated` per finding, `correlation_run_completed` when the job
    finishes."""
    case_id, _evidence_id, run_id = await _seed_correlatable_case(db)

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    events = await _events(db)
    findings = [e for e in events if e["event_type"] == "investigation.correlation_generated"]
    completions = [
        e for e in events if e["event_type"] == "investigation.correlation_run_completed"
    ]
    assert len(findings) == 6
    assert len(completions) == 1
    assert completions[0]["payload"] == {
        "run_id": str(run_id),
        "case_id": str(case_id),
        "findings_generated_count": 6,
    }
    assert {str(e["correlation_id"]) for e in events} == {_CORRELATION}
    # Every finding names the case owner `notification` alerts, and what produced it.
    for event in findings:
        assert event["payload"]["recipient_user_id"] == str(_OWNER)
        assert event["payload"]["generated_by"].startswith("heuristic-identifier-extraction/1 run:")


async def test_the_findings_reach_the_case_graph_over_http(db) -> None:
    """**The whole point of the increment.** A run an analyst cannot see is a run that did not
    happen: the write side records, the outbox announces, the projector builds `investigation_read`,
    and §6's endpoint serves it (ADR-0013)."""
    case_id, _evidence_id, run_id = await _seed_correlatable_case(db)
    await run_correlation(_ctx(db), run_id, _CORRELATION)

    await _drain(_dispatcher(db))

    async with await _client(_app(db, _Queue())) as client:
        response = await client.get(f"/api/v1/cases/{case_id}/graph")

    assert response.status_code == 200
    graph = response.json()["data"]
    assert {node["canonical_name"] for node in graph["entities"]} == {_DOMAIN, _EMAIL, _SHA256}
    assert {node["status"] for node in graph["entities"]} == {STATUS_PROPOSED}
    assert len(graph["relationships"]) == 3
    assert {edge["type"] for edge in graph["relationships"]} == {"associated_with"}
    # §6 guarantees the subgraph is self-contained.
    present = {node["entity_id"] for node in graph["entities"]}
    for edge in graph["relationships"]:
        assert {edge["from_entity_id"], edge["to_entity_id"]} <= present


async def test_a_second_run_over_unchanged_evidence_converges(db) -> None:
    """Re-running a case must not double its graph — and the honest report of that is a count of
    zero, not a silent repeat."""
    case_id, _evidence_id, first_run = await _seed_correlatable_case(db)
    await run_correlation(_ctx(db), first_run, _CORRELATION)
    entities_before = len(await _rows(db, Entity))
    relationships_before = len(await _rows(db, Relationship))
    findings_before = len(
        [e for e in await _events(db) if e["event_type"] == "investigation.correlation_generated"]
    )

    second_run = await _queue_run(db, case_id)
    await run_correlation(_ctx(db), second_run, _CORRELATION_2)

    assert len(await _rows(db, Entity)) == entities_before
    assert len(await _rows(db, Relationship)) == relationships_before
    assert (
        len(
            [
                e
                for e in await _events(db)
                if e["event_type"] == "investigation.correlation_generated"
            ]
        )
        == findings_before
    )
    runs = {run.run_id: run for run in await _rows(db, CorrelationRun)}
    assert runs[second_run].status == RUN_COMPLETED
    assert runs[second_run].findings_generated_count == 0


async def test_rerunning_the_same_run_id_is_a_no_op(db) -> None:
    """What an arq retry after a successful-but-unacknowledged run looks like. Re-walking it would
    re-announce every finding, and §25.9 keys `notification` on the finding, not the run."""
    _case_id, _evidence_id, run_id = await _seed_correlatable_case(db)
    await run_correlation(_ctx(db), run_id, _CORRELATION)
    events_before = len(await _events(db))

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    assert len(await _events(db)) == events_before
    assert len(await _rows(db, Entity)) == 3


async def test_quarantined_evidence_is_never_correlated(db) -> None:
    """`quarantined` is what a malware detection or a custody-chain gap sets. Findings grounded
    in it
    would reach an analyst with nothing saying the evidence is untrusted."""
    case_id = await _seed_case(db)
    clean = await _seed_evidence(db, title=f"Beacon to {_DOMAIN}")
    dirty = await _seed_evidence(db, title="Payload from other.example", status="quarantined")
    await _link(db, case_id, clean)
    await _link(db, case_id, dirty)
    run_id = await _queue_run(db, case_id)

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    assert {entity.canonical_name for entity in await _rows(db, Entity)} == {_DOMAIN}


async def test_a_scoped_run_reads_only_the_named_evidence(db) -> None:
    """api-design.md §6's `{ scope: { evidence_ids } }`, carried to the worker as a job argument."""
    case_id = await _seed_case(db)
    wanted = await _seed_evidence(db, title=f"Beacon to {_DOMAIN}")
    other = await _seed_evidence(db, title="Contact at other-c2.example")
    await _link(db, case_id, wanted)
    await _link(db, case_id, other)
    run_id = await _queue_run(db, case_id)

    await run_correlation(_ctx(db), run_id, _CORRELATION, [wanted])

    assert {entity.canonical_name for entity in await _rows(db, Entity)} == {_DOMAIN}


# --- the failure and cancellation paths -------------------------------------
class _BrokenExtractor:
    """An adapter that fails the way a real inference client would: mid-run, on one record."""

    name = "broken-extractor/test"

    async def extract(self, record: EvidenceRecord) -> Extraction:
        raise RuntimeError("inference endpoint unreachable")


async def test_a_failed_run_is_visible_to_a_poller_and_announced(db) -> None:
    """Marked `failed` in a **separate** transaction, so the outcome survives the rollback that
    discarded the work — a run stuck `running` forever is indistinguishable from a slow one."""
    _case_id, _evidence_id, run_id = await _seed_correlatable_case(db)

    with pytest.raises(RuntimeError):
        await run_correlation(_ctx(db, _BrokenExtractor()), run_id, _CORRELATION)

    [run] = await _rows(db, CorrelationRun)
    assert run.status == RUN_FAILED
    assert run.completed_at is not None
    assert run.started_at is not None  # the claim happened, and its clock survived
    failures = [
        e for e in await _events(db) if e["event_type"] == "investigation.correlation_run_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["payload"]["run_id"] == str(run_id)
    # Nothing half-written: the extraction never produced a finding, so none exist.
    assert await _rows(db, Entity) == []


async def test_a_retry_after_a_failure_completes_the_run(db) -> None:
    """A failure is usually transient, and arq's next attempt is the retry — so `failed` must be
    re-claimable or every blip becomes a case an analyst re-triggers by hand."""
    _case_id, _evidence_id, run_id = await _seed_correlatable_case(db)
    with pytest.raises(RuntimeError):
        await run_correlation(_ctx(db, _BrokenExtractor()), run_id, _CORRELATION)

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    [run] = await _rows(db, CorrelationRun)
    assert run.status == RUN_COMPLETED
    assert run.findings_generated_count == 6


async def test_a_cancelled_run_stops_and_keeps_what_it_found(db) -> None:
    """Guide Part 12's cooperative cancellation. It ends `failed` because api-design.md §6's status
    enum has no `cancelled` value, and reporting `completed` would tell a client the case had been
    correlated in full."""
    _case_id, _evidence_id, run_id = await _seed_correlatable_case(db)
    async with db() as session:
        await session.execute(
            update(CorrelationRun)
            .where(CorrelationRun.run_id == run_id)
            .values(cancellation_requested=True)
        )
        await session.commit()

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    [run] = await _rows(db, CorrelationRun)
    assert run.status == RUN_FAILED
    assert run.findings_generated_count == 0
    assert await _rows(db, Entity) == []
    assert [
        e["event_type"]
        for e in await _events(db)
        if e["event_type"].startswith("investigation.correlation_run")
    ] == ["investigation.correlation_run_failed"]


async def test_a_deleted_case_ends_the_run_without_failing_it(db) -> None:
    """Nothing to correlate, and nothing was wrong with the request that asked for it."""
    run_id = await _queue_run(db, uuid4())

    await run_correlation(_ctx(db), run_id, _CORRELATION)

    [run] = await _rows(db, CorrelationRun)
    assert run.status == RUN_COMPLETED
    assert run.findings_generated_count == 0


# --- the trigger endpoint (api-design.md §6) --------------------------------
async def test_the_trigger_returns_202_queued_with_a_location(db) -> None:
    """§6: `202` with `Location: /api/v1/correlation-runs/{run_id}`, body `{ run_id, status:
    "queued" }` — the async pattern of §2.12."""
    case_id = await _seed_case(db)
    evidence_id = await _seed_evidence(db, title=f"Beacon to {_DOMAIN}")
    await _link(db, case_id, evidence_id)
    queue = _Queue()

    async with await _client(_app(db, queue)) as client:
        response = await client.post(f"/api/v1/cases/{case_id}/correlation-runs", json={})

    assert response.status_code == 202
    body = response.json()["data"]
    assert body["status"] == RUN_QUEUED
    assert response.headers["Location"] == f"/api/v1/correlation-runs/{body['run_id']}"
    assert queue.jobs[0][0] == "run_correlation"
    assert queue.jobs[0][1][0] == UUID(body["run_id"])


async def test_the_trigger_refuses_a_case_with_no_evidence(db) -> None:
    """§6's "Case must have >= 1 linked evidence item". A run over an empty case would report a
    completed correlation pass over nothing."""
    case_id = await _seed_case(db)

    async with await _client(_app(db, _Queue())) as client:
        response = await client.post(f"/api/v1/cases/{case_id}/correlation-runs", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_a_second_trigger_while_a_run_is_in_progress_is_a_conflict(db) -> None:
    """§6's 409. Two runs walk the same evidence and each announces its own findings, so the case
    owner is notified twice for one fact."""
    case_id = await _seed_case(db)
    evidence_id = await _seed_evidence(db, title=f"Beacon to {_DOMAIN}")
    await _link(db, case_id, evidence_id)

    async with await _client(_app(db, _Queue())) as client:
        first = await client.post(f"/api/v1/cases/{case_id}/correlation-runs", json={})
        second = await client.post(f"/api/v1/cases/{case_id}/correlation-runs", json={})

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"


async def test_a_scope_naming_another_cases_evidence_is_refused(db) -> None:
    """Refused, not silently intersected: a run over the eight of ten items that happened to belong
    here would report success while announcing findings against the wrong case."""
    case_id = await _seed_case(db)
    linked = await _seed_evidence(db)
    foreign = await _seed_evidence(db)
    await _link(db, case_id, linked)

    async with await _client(_app(db, _Queue())) as client:
        response = await client.post(
            f"/api/v1/cases/{case_id}/correlation-runs",
            json={"scope": {"evidence_ids": [str(linked), str(foreign)]}},
        )

    assert response.status_code == 422
    assert str(foreign) in response.text


async def test_the_poll_endpoint_reports_the_run_as_the_job_left_it(db) -> None:
    """§2.12's async pattern: the row is the state, and a client polls it rather than the queue."""
    case_id, _evidence_id, run_id = await _seed_correlatable_case(db)
    await run_correlation(_ctx(db), run_id, _CORRELATION)

    async with await _client(_app(db, _Queue())) as client:
        response = await client.get(f"/api/v1/correlation-runs/{run_id}")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == RUN_COMPLETED
    assert body["findings_generated_count"] == 6
    assert body["case_id"] == str(case_id)
    assert body["started_at"] is not None and body["completed_at"] is not None


async def test_a_claimed_run_reports_running_before_it_finishes(db) -> None:
    """The claim commits on its own, so a poller sees `running` *while* the pass runs rather than
    `queued` until it ends. Observed from a separate session, which is the only way to prove the
    commit happened rather than the attribute merely being set."""
    case_id = await _seed_case(db)
    evidence_id = await _seed_evidence(db, title=f"Beacon to {_DOMAIN}")
    await _link(db, case_id, evidence_id)
    run_id = await _queue_run(db, case_id)
    seen: list[str] = []

    class _ObservingExtractor:
        name = "observing-extractor/test"

        async def extract(self, record: EvidenceRecord) -> Extraction:
            async with db() as session:
                row = await session.execute(
                    select(CorrelationRun.status).where(CorrelationRun.run_id == run_id)
                )
                seen.append(row.scalar_one())
            return Extraction()

    await run_correlation(_ctx(db, _ObservingExtractor()), run_id, _CORRELATION)

    assert seen == [RUN_RUNNING]
