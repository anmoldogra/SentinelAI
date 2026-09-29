"""The OSINT connector pipeline against a real Postgres — api-design.md §4.3, CEM §9.

What this covers that nothing else can: the pipeline crosses a **module boundary**. A finding
lands in `osint`, publishing normalizes it into `ingestion.evidence` via `ingestion.public`, and the
`osint.finding_captured` event lands in `osint`'s own outbox **signed** under `EVENT_ROOT`. Three
schemas, two modules, one transaction — a fake for any of them would prove only that the test author
knew the answer.

The signature is verified with the real `EventSigner`, not asserted non-null: ADR-0007's claim is
that
a consumer can prove the event came from this platform, and a test that only checked the column was
populated would pass against bytes that verify against nothing.

Skips cleanly when no Postgres is reachable; never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.modules.ingestion.models import (
    AttributeSchemaRegistry,
    Evidence,
    EvidenceCustodyEvent,
    IntakeRecord,
)
from sentinelai.modules.ingestion.repository import IngestionUnitOfWork
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.modules.osint.models import OsintConnectorState, OsintFinding, OsintSource
from sentinelai.modules.osint.repository import OsintUnitOfWork
from sentinelai.modules.osint.schemas import FindingCreate, SourceCreate, SourceUpdate
from sentinelai.modules.osint.service import (
    STATUS_CAPTURED,
    STATUS_PUBLISHED,
    OsintService,
    source_etag,
)
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.events.signing import EventSigner
from sentinelai.shared.cem import PUBLIC_SOURCE_AUTHORITY
from sentinelai.shared.exceptions import (
    ConflictError,
    PreconditionFailedError,
    ValidationFailedError,
)
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_ACTOR = CurrentUser(user_id=uuid4(), roles=("investigator",))
_SCHEMA_VERSION = "1.0.0"
_ARTIFACT_TYPE = "domain_whois"


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
    name = f"sentinelai_osint_{uuid.uuid4().hex[:8]}"
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
    """Three schemas, because the pipeline genuinely spans them.

    `osint` holds the finding and its outbox; `ingestion` holds the evidence and its custody ledger;
    `platform` holds the audit log every write here appends to.
    """
    from sentinelai.platform.events.outbox import get_outbox_table

    async with engine.begin() as conn:
        for schema in ("platform", "ingestion", "osint"):
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                OsintSource.__table__,
                OsintFinding.__table__,
                OsintConnectorState.__table__,
                Evidence.__table__,
                EvidenceCustodyEvent.__table__,
                AttributeSchemaRegistry.__table__,
                # `ingest_evidence` writes an intake record for every attempt, accepted or
                # rejected — that record is how a rejected ingest stays auditable (FR-1.3).
                IntakeRecord.__table__,
                AuditLog.__table__,
            ],
        )
        await conn.run_sync(get_outbox_table("osint").create, checkfirst=True)
        await conn.run_sync(get_outbox_table("ingestion").create, checkfirst=True)


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


def _service(session: AsyncSession) -> OsintService:
    """The real service, wired to the real `ingestion` service through the same session.

    One session for both modules is what production does (ADR-0005: the entrypoint owns one
    transaction), and it is what makes "the finding and its evidence commit together" testable.
    """
    kms = kms_for_tests()
    evidence = EvidenceService(
        IngestionUnitOfWork(session, kms=kms), storage=FakeObjectStorage(), kms=kms
    )
    return OsintService(OsintUnitOfWork(session, kms=kms), evidence=evidence, kms=kms)


async def _register_schema(session: AsyncSession) -> None:
    """Register the (schema_version, category, artifact_type) triple publishing requires.

    `ingest_evidence` refuses an unregistered triple (CEM §13), so without this every publish would
    fail for the right reason in the wrong test.
    """
    session.add(
        AttributeSchemaRegistry(
            schema_version=_SCHEMA_VERSION,
            category="osint",
            artifact_type=_ARTIFACT_TYPE,
        )
    )
    await session.flush()


def _raw(**overrides: Any) -> dict[str, Any]:
    """A connector payload carrying the CEM envelope `publish` maps from.

    Modelled on CEM §5's own OSINT WHOIS example, so the test exercises the documented shape rather
    than one invented for convenience.
    """
    body: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "artifact_type": _ARTIFACT_TYPE,
        "title": "WHOIS for suspect-domain.example",
        "confidence": "0.9",
        "attributes": {"domain": "suspect-domain.example", "registrar": "Example Registrar Inc."},
    }
    body.update(overrides)
    return body


async def _seed_source(session: AsyncSession, *, name: str = "whois-connector") -> OsintSource:
    return await _service(session).register_source(
        SourceCreate(name=name, connector_type="api_pull", reliability_baseline="B2"),
        _ACTOR,
        str(uuid4()),
    )


async def _outbox_rows(session: AsyncSession, schema: str) -> list[Any]:
    result = await session.execute(
        text(f"SELECT * FROM {schema}.outbox_events ORDER BY occurred_at")
    )
    return list(result.mappings().all())


# --- source registration ----------------------------------------------------
async def test_registering_a_source_persists_it_and_announces_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.3: a newly-registered source is newly active, so it publishes `source_activated` —
    otherwise a consumer tracking live feeds would miss every source never toggled after
    creation."""
    async with db() as session:
        source = await _seed_source(session)
        await session.commit()

    async with db() as session:
        stored = (await session.execute(select(OsintSource))).scalars().one()
        rows = await _outbox_rows(session, "osint")

    assert stored.source_id == source.source_id
    assert stored.is_active is True
    assert stored.reliability_baseline == "B2"
    assert [r["event_type"] for r in rows] == ["osint.source_activated"]


async def test_deactivating_a_source_publishes_once_and_only_on_a_real_transition(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Publishing on every PATCH would tell consumers a source was toggled when the operator only
    edited its reliability baseline."""
    async with db() as session:
        service = _service(session)
        source = await _seed_source(session)
        await session.commit()

        # A baseline-only edit: no transition, so no event.
        await service.update_source(
            source.source_id,
            SourceUpdate(reliability_baseline="C3"),
            _ACTOR,
            source_etag(source),
            str(uuid4()),
        )
        await session.commit()

        await service.update_source(
            source.source_id,
            SourceUpdate(is_active=False),
            _ACTOR,
            source_etag(source),
            str(uuid4()),
        )
        await session.commit()

    async with db() as session:
        rows = await _outbox_rows(session, "osint")
    assert [r["event_type"] for r in rows] == [
        "osint.source_activated",
        "osint.source_deactivated",
    ]


async def test_a_stale_etag_is_refused(db: async_sessionmaker[AsyncSession]) -> None:
    """§2.6's optimistic concurrency. The ETag covers only the mutable fields, so it changes when
    one of them does — which is what makes the guard meaningful rather than decorative."""
    async with db() as session:
        service = _service(session)
        source = await _seed_source(session)
        stale = source_etag(source)
        await service.update_source(
            source.source_id, SourceUpdate(reliability_baseline="C3"), _ACTOR, stale, str(uuid4())
        )

        with pytest.raises(PreconditionFailedError):
            await service.update_source(
                source.source_id,
                SourceUpdate(reliability_baseline="D4"),
                _ACTOR,
                stale,
                str(uuid4()),
            )


# --- finding capture --------------------------------------------------------
async def test_a_connector_push_persists_a_finding_and_signs_its_event(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The core of the task: an external connector pushes a finding, it persists in the `osint`
    schema, and the integration event is published **signed** (ADR-0007 §1).

    The signature is verified rather than merely present — a populated column that verifies against
    nothing would satisfy a weaker test and fail the guarantee.
    """
    async with db() as session:
        source = await _seed_source(session)
        finding = await _service(session).create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes=_raw()), _ACTOR, str(uuid4())
        )
        await session.commit()

    async with db() as session:
        stored = (await session.execute(select(OsintFinding))).scalars().one()
        rows = await _outbox_rows(session, "osint")

    assert stored.finding_id == finding.finding_id
    assert stored.status == STATUS_CAPTURED
    assert stored.evidence_id is None, "§3.3: a finding is not evidence until it is published"
    assert stored.reliability_rating == "B2", "inherited from the source's baseline"
    assert stored.raw_attributes["attributes"]["domain"] == "suspect-domain.example"

    captured = [r for r in rows if r["event_type"] == "osint.finding_captured"]
    assert len(captured) == 1
    event = captured[0]
    assert event["payload"]["finding_id"] == str(finding.finding_id)
    assert event["payload"]["source_id"] == str(source.source_id)
    assert event["payload"]["reliability_rating"] == "B2"

    assert event["signature"] is not None, "ADR-0007 §1: outbox events are signed"
    signer = EventSigner(kms_for_tests())
    assert await signer.verify(
        schema="osint",
        envelope=event["signature"],
        event_id=event["event_id"],
        event_type=event["event_type"],
        event_version=event["event_version"],
        aggregate_type=event["aggregate_type"],
        aggregate_id=event["aggregate_id"],
        payload=event["payload"],
        correlation_id=event["correlation_id"],
        causation_id=event["causation_id"],
        trace_id=event["trace_id"],
        actor_type=event["actor_type"],
        actor_ref=event["actor_ref"],
        occurred_at=event["occurred_at"],
    ), "the stored signature must verify under EVENT_ROOT"


async def test_a_finding_for_a_deactivated_source_is_refused(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of `is_active` is that it stops new data arriving under that source's name; a
    finding attributed to a withdrawn feed would claim a provenance the operator revoked."""
    async with db() as session:
        service = _service(session)
        source = await _seed_source(session)
        await service.update_source(
            source.source_id,
            SourceUpdate(is_active=False),
            _ACTOR,
            source_etag(source),
            str(uuid4()),
        )
        await session.commit()

        with pytest.raises(ValidationFailedError):
            await service.create_finding(
                FindingCreate(source_id=source.source_id, raw_attributes=_raw()),
                _ACTOR,
                str(uuid4()),
            )


async def test_a_finding_for_an_unknown_source_is_a_404(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        from sentinelai.modules.osint.exceptions import SourceNotFoundError

        with pytest.raises(SourceNotFoundError):
            await _service(session).create_finding(
                FindingCreate(source_id=uuid4(), raw_attributes=_raw()), _ACTOR, str(uuid4())
            )


async def test_raw_attributes_are_stored_unmodified(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """CEM §9 step 1 keeps the raw connector output for audit. Validating its CEM shape at capture
    would reject findings a future mapping profile could handle, and the raw record is what an
    examiner returns to when a mapping is later found wrong."""
    unmappable = {"whatever_the_connector_sent": [1, 2, 3], "nested": {"a": None}}
    async with db() as session:
        source = await _seed_source(session)
        finding = await _service(session).create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes=unmappable),
            _ACTOR,
            str(uuid4()),
        )
        await session.commit()

    async with db() as session:
        stored = await session.get(OsintFinding, finding.finding_id)

    assert stored is not None
    assert stored.raw_attributes == unmappable, "stored verbatim, not normalized"


# --- publishing into the CEM ------------------------------------------------
async def test_publishing_normalizes_the_finding_into_evidence(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The cross-module step: `osint` → `ingestion.public` → `ingestion.evidence`, plus the custody
    genesis entry and the audit entry §4.3 requires."""
    async with db() as session:
        await _register_schema(session)
        source = await _seed_source(session)
        service = _service(session)
        finding = await service.create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes=_raw()), _ACTOR, str(uuid4())
        )
        published = await service.publish_finding(finding.finding_id, _ACTOR, str(uuid4()))
        # Read inside the session: `rollback()` and a detached instance both expire attributes, and
        # a test that depends on when SQLAlchemy happens to expire is testing the ORM, not the code.
        status, evidence_id = published.status, published.evidence_id
        await session.commit()

    assert status == STATUS_PUBLISHED
    assert evidence_id is not None

    async with db() as session:
        evidence = (await session.execute(select(Evidence))).scalars().one()
        custody = (await session.execute(select(EvidenceCustodyEvent))).scalars().all()
        audits = [a.action for a in (await session.execute(select(AuditLog))).scalars().all()]

    assert evidence.evidence_id == evidence_id
    assert evidence.category == "osint"
    assert evidence.artifact_type == _ARTIFACT_TYPE
    assert evidence.title == "WHOIS for suspect-domain.example"
    assert evidence.confidence == Decimal("0.9")
    assert evidence.reliability_rating == "B2"
    # Provenance is derived from the registered source, never from the payload — a connector must
    # not be able to attribute its output to a different system.
    assert evidence.source == {
        "system": "whois-connector",
        # The registered source's own id, so the provenance resolves back to the exact feed
        # configuration rather than to a name that can be edited later.
        "collector_id": str(source.source_id),
        "collection_method": "api_pull",
    }
    # OSINT is lawfully collected from a public source; CEM §13's sentinel says exactly that.
    assert evidence.legal_authority_ref == PUBLIC_SOURCE_AUTHORITY
    assert len(custody) == 1, "CEM §9 step 5: a genesis custody entry"
    assert "evidence_published_from_osint" in audits, "§4.3's audit requirement"


async def test_republishing_is_a_409_not_a_duplicate(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§4.3's "Idempotency: Natural" — the check is on the finding's own `evidence_id`, which is why
    this endpoint needs no idempotency key for a retry to be safe."""
    async with db() as session:
        await _register_schema(session)
        source = await _seed_source(session)
        service = _service(session)
        finding = await service.create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes=_raw()), _ACTOR, str(uuid4())
        )
        await service.publish_finding(finding.finding_id, _ACTOR, str(uuid4()))
        await session.commit()

        with pytest.raises(ConflictError):
            await service.publish_finding(finding.finding_id, _ACTOR, str(uuid4()))

    async with db() as session:
        evidence = (await session.execute(select(Evidence))).scalars().all()
    assert len(evidence) == 1, "a retry must not create a second evidence object"


@pytest.mark.parametrize(
    "missing", ["schema_version", "artifact_type", "title", "attributes", "confidence"]
)
async def test_publishing_without_a_required_field_fails_loudly(
    db: async_sessionmaker[AsyncSession], missing: str
) -> None:
    """CEM §9 step 4 / FR-1.3: validation failure "routes to rejection with a clear error, never
    silent partial ingestion". The error names the field, so a connector author can fix it."""
    raw = _raw()
    del raw[missing]
    async with db() as session:
        await _register_schema(session)
        source = await _seed_source(session)
        service = _service(session)
        finding = await service.create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes=raw), _ACTOR, str(uuid4())
        )

        with pytest.raises(ValidationFailedError) as excinfo:
            await service.publish_finding(finding.finding_id, _ACTOR, str(uuid4()))

    fields = [d.get("field") for d in excinfo.value.details]
    assert f"raw_attributes.{missing}" in fields


async def test_a_failed_publish_leaves_the_finding_unpublished(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§4.3: "the finding remains unpublished". The finding and its evidence share one transaction,
    so a validation failure rolls both back and the finding stays re-publishable."""
    async with db() as session:
        await _register_schema(session)
        source = await _seed_source(session)
        service = _service(session)
        finding = await service.create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes={"nothing": "useful"}),
            _ACTOR,
            str(uuid4()),
        )
        await session.commit()

        finding_id = finding.finding_id
        with pytest.raises(ValidationFailedError):
            await service.publish_finding(finding_id, _ACTOR, str(uuid4()))
        await session.rollback()

    async with db() as session:
        stored = await session.get(OsintFinding, finding_id)
        evidence = (await session.execute(select(Evidence))).scalars().all()

    assert stored is not None
    assert stored.evidence_id is None
    assert stored.status == STATUS_CAPTURED
    assert evidence == [], "no half-written evidence object"


async def test_an_unregistered_schema_triple_is_refused(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The validation belongs to `ingestion`, and this proves osint does not bypass it: CEM §13
    requires the (schema_version, category, artifact_type) triple be registered."""
    async with db() as session:
        source = await _seed_source(session)  # no _register_schema call
        service = _service(session)
        finding = await service.create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes=_raw()), _ACTOR, str(uuid4())
        )

        with pytest.raises(ValidationFailedError) as excinfo:
            await service.publish_finding(finding.finding_id, _ACTOR, str(uuid4()))

    assert any("schema_version" in str(d.get("field")) for d in excinfo.value.details)


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "not-a-number", None])
async def test_a_confidence_outside_zero_to_one_is_refused(
    db: async_sessionmaker[AsyncSession], bad: str | None
) -> None:
    """A confidence is a probability. It is also parsed through `str` rather than `Decimal(float)`,
    so `0.9` stays 0.9 instead of 0.9000000000000000222…"""
    async with db() as session:
        await _register_schema(session)
        source = await _seed_source(session)
        service = _service(session)
        finding = await service.create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes=_raw(confidence=bad)),
            _ACTOR,
            str(uuid4()),
        )

        with pytest.raises(ValidationFailedError):
            await service.publish_finding(finding.finding_id, _ACTOR, str(uuid4()))


async def test_publishing_also_emits_ingestions_own_event(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§4.3: `evidence.ingested` is published "indirectly ... by `ingestion` once the evidence row
    commits" — on **ingestion's** outbox, not osint's.

    That separation is the module boundary made visible: each module publishes only its own facts to
    its own schema, and neither writes to the other's outbox.
    """
    async with db() as session:
        await _register_schema(session)
        source = await _seed_source(session)
        service = _service(session)
        finding = await service.create_finding(
            FindingCreate(source_id=source.source_id, raw_attributes=_raw()), _ACTOR, str(uuid4())
        )
        await service.publish_finding(finding.finding_id, _ACTOR, str(uuid4()))
        await session.commit()

    async with db() as session:
        osint_events = {r["event_type"] for r in await _outbox_rows(session, "osint")}
        ingestion_events = {r["event_type"] for r in await _outbox_rows(session, "ingestion")}

    assert "osint.finding_captured" in osint_events
    assert "evidence.ingested" in ingestion_events
    assert not any(e.startswith("evidence.") for e in osint_events), (
        "osint must not publish another module's events onto its own outbox"
    )


# --- listing ----------------------------------------------------------------
async def test_findings_page_forward_without_gaps(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Keyset pagination over `(collected_at, finding_id)`. The composite matters because a batch
    pull writes several findings with the same timestamp, and ordering by it alone would let a page
    boundary drop or duplicate one."""
    from sentinelai.shared.pagination import PageParams, encode_cursor

    async with db() as session:
        source = await _seed_source(session)
        service = _service(session)
        created = [
            await service.create_finding(
                FindingCreate(source_id=source.source_id, raw_attributes=_raw(title=f"f{i}")),
                _ACTOR,
                str(uuid4()),
            )
            for i in range(5)
        ]
        await session.commit()

    collected: list[UUID] = []
    cursor: str | None = None
    async with db() as session:
        service = _service(session)
        for _ in range(10):  # bounded, so a broken cursor fails rather than loops
            page = await service.list_findings(_ACTOR, PageParams(limit=2, cursor=cursor))
            if not page:
                break
            collected.extend(f.finding_id for f in page)
            last = page[-1]
            cursor = encode_cursor(last.collected_at.isoformat(), last.finding_id)

    assert len(collected) == len(created)
    assert len(set(collected)) == len(created), "no finding returned twice"
    assert set(collected) == {f.finding_id for f in created}


async def test_sources_are_listed_by_name(db: async_sessionmaker[AsyncSession]) -> None:
    """Stable ordering, so the console's list does not reshuffle between reads."""
    async with db() as session:
        for name in ("zeta-feed", "alpha-feed", "mid-feed"):
            await _seed_source(session, name=name)
        await session.commit()

    async with db() as session:
        names = [s.name for s in await _service(session).list_sources(_ACTOR)]

    assert names == ["alpha-feed", "mid-feed", "zeta-feed"]


async def test_an_unknown_finding_is_a_404(db: async_sessionmaker[AsyncSession]) -> None:
    from sentinelai.modules.osint.exceptions import FindingNotFoundError

    async with db() as session:
        with pytest.raises(FindingNotFoundError):
            await _service(session).get_finding(uuid4(), _ACTOR)


def test_collected_at_is_server_assigned() -> None:
    """A connector does not get to state when the platform received its finding.

    `collected_at` is set from the server clock at capture, so a connector cannot backdate a record
    into an already-anchored window — the same reasoning ADR-0003's anchor watermark rests on.
    """
    import inspect

    from sentinelai.modules.osint import service as osint_service

    source = inspect.getsource(osint_service.OsintService.create_finding)
    assert "collected_at=datetime.now(UTC)" in source
    assert "data.collected_at" not in source, "the payload must not supply it"
