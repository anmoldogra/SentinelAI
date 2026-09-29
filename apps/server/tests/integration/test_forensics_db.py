"""The forensics intake and publish pipeline against real Postgres — api-design.md §4.5, CEM §9.

The real `ForensicsService` wired to the real `EvidenceService` over one session, because that is
what production does (ADR-0005: the entrypoint owns one transaction) and it is what makes "the
artifact and its evidence commit together" testable rather than asserted.

What only a real database and a real `ingestion` can settle:

* **ADR-0008 §3's recompute.** The acquisition hash an examiner registers is a *claim*; publishing
  streams the stored object, recomputes the digest, and refuses on a mismatch. That refusal links an
  imaging tool's manifest to the bytes the platform actually holds, and it is the reason the hash is
  carried through rather than trusted.
* **The custody genesis entry**, written by `ingestion` inside the same transaction.
* **Rollback on a rejected publish** — the artifact must stay unpublished, not half-published.
* **The signed `artifact_processed` event** in `forensics`' own outbox.

Runs in a throwaway database created and dropped here. Skips cleanly when no Postgres is reachable;
never fakes a pass.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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

from sentinelai.modules.forensics.models import Artifact
from sentinelai.modules.forensics.repository import ForensicsUnitOfWork
from sentinelai.modules.forensics.schemas import ArtifactCreate
from sentinelai.modules.forensics.service import (
    STATUS_PUBLISHED,
    STATUS_REGISTERED,
    ForensicsService,
)
from sentinelai.modules.ingestion.models import (
    AttributeSchemaRegistry,
    Evidence,
    EvidenceCustodyEvent,
    IntakeRecord,
)
from sentinelai.modules.ingestion.repository import IngestionUnitOfWork
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.events.outbox import get_outbox_table
from sentinelai.shared.exceptions import ConflictError, ValidationFailedError
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_EXAMINER = CurrentUser(user_id=uuid4(), roles=("investigator",))
# Deliberately in the **past**: CEM §13 rejects `collected_at` after `ingested_at` beyond a
# clock-skew tolerance, and that check runs before the payload recompute — so a future
# acquisition time would short-circuit every integrity assertion in this file and the failures
# would point at the wrong rule.
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_SCHEMA_VERSION = "1.0.0"
# The two `(category, artifact_type)` triples `202608300002_ingestion_seed` registers for
# forensics. Publishing an unregistered triple is refused by CEM §13, so using a real one keeps
# these tests failing for the reason under test rather than for a missing registry row.
_DISK_KIND = "forensic_image"
_MOBILE_KIND = "oxygen_extraction"

_BUCKET = "sentinelai-evidence"
_KEY = "images/ws-4471.e01"
_IMAGE_BYTES = b"E01 acquisition container bytes"
_IMAGE_DIGEST = hashlib.sha256(_IMAGE_BYTES).hexdigest()
_UNRELATED_DIGEST = hashlib.sha256(b"a different image entirely").hexdigest()


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
    name = f"sentinelai_forensics_{uuid.uuid4().hex[:8]}"
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

    `forensics` holds the artifact and its outbox; `ingestion` holds the evidence and its custody
    ledger; `platform` holds the audit log every write here appends to.
    """
    async with engine.begin() as conn:
        for schema in ("platform", "ingestion", "forensics"):
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Artifact.__table__,
                Evidence.__table__,
                EvidenceCustodyEvent.__table__,
                AttributeSchemaRegistry.__table__,
                # `ingest_evidence` writes an intake record for every attempt, accepted or
                # rejected — that record is how a rejected ingest stays auditable (FR-1.3).
                IntakeRecord.__table__,
                AuditLog.__table__,
            ],
        )
        await conn.run_sync(get_outbox_table("forensics").create, checkfirst=True)
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


@pytest.fixture
def storage() -> FakeObjectStorage:
    """Object storage holding one acquisition image, so ADR-0008's recompute has real bytes."""
    return FakeObjectStorage()


async def _seed_image(storage: FakeObjectStorage) -> None:
    await storage.ensure_bucket(_BUCKET)
    await storage.put_immutable(_BUCKET, _KEY, _IMAGE_BYTES, retain_until=_NOW + timedelta(days=1))


def _service(session: AsyncSession, storage: FakeObjectStorage) -> ForensicsService:
    """The real service, wired to the real `ingestion` service through the same session."""
    kms = kms_for_tests()
    evidence = EvidenceService(IngestionUnitOfWork(session, kms=kms), storage=storage, kms=kms)
    return ForensicsService(ForensicsUnitOfWork(session, kms=kms), evidence=evidence, kms=kms)


async def _register_schemas(session: AsyncSession) -> None:
    """Register the triples publishing requires (CEM §13 refuses an unregistered one)."""
    for category, artifact_type in (
        ("digital_forensics", _DISK_KIND),
        ("mobile_forensics", _MOBILE_KIND),
    ):
        session.add(
            AttributeSchemaRegistry(
                schema_version=_SCHEMA_VERSION, category=category, artifact_type=artifact_type
            )
        )
    await session.flush()


def _envelope(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "title": "Disk image of workstation WS-4471",
        "attributes": {"image_format": "E01"},
        "legal_authority_ref": "WARRANT-2026-0417",
    }
    info.update(overrides)
    return info


def _create(**overrides: Any) -> ArtifactCreate:
    fields: dict[str, Any] = {
        "artifact_kind": _DISK_KIND,
        "acquisition_tool": "EnCase 8",
        "acquisition_hash": f"SHA-256:{_IMAGE_DIGEST}",
        "collected_at": _NOW,
        "device_info": _envelope(),
    }
    fields.update(overrides)
    return ArtifactCreate(**fields)


async def _outbox(db: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with db() as session:
        result = await session.execute(select(get_outbox_table("forensics")))
        return [dict(row) for row in result.mappings().all()]


# --- registration -----------------------------------------------------------
async def test_registering_an_artifact_stores_it_unpublished(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """§3.3's `evidence_id` is nullable until publication: a registered artifact is not yet
    evidence."""
    async with db() as session:
        artifact = await _service(session, storage).register_artifact(
            _create(), _EXAMINER, str(uuid4())
        )
        await session.commit()
        artifact_id = artifact.artifact_id

    async with db() as session:
        stored = await session.get(Artifact, artifact_id)

    assert stored is not None
    assert stored.evidence_id is None
    assert stored.status == STATUS_REGISTERED
    assert stored.artifact_kind == _DISK_KIND
    assert stored.acquisition_tool == "EnCase 8"


async def test_registration_publishes_artifact_registered_signed(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """§25.5's trigger is "`POST /forensics/artifacts` commits".

    api-design.md §4.5 says "Events Published: none at this step" for the same endpoint. They cannot
    both hold, and `CLAUDE.md` makes `event-driven-architecture.md` authoritative for the event
    catalog — the same conflict, resolved the same way, as `threat_intel.ioc_registered` in IC-042.
    """
    async with db() as session:
        await _service(session, storage).register_artifact(_create(), _EXAMINER, str(uuid4()))
        await session.commit()

    rows = await _outbox(db)
    assert [row["event_type"] for row in rows] == ["forensics.artifact_registered"]
    assert rows[0]["payload"]["artifact_kind"] == _DISK_KIND
    assert rows[0]["signature"] is not None, "ADR-0007 §1: signed under EVENT_ROOT"
    assert rows[0]["actor_ref"] is not None, "an examiner registered this; a principal did act"


async def test_registration_is_audited(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """§4.5: "artifact registration is itself a chain-of-custody-relevant act even before canonical
    publication"."""
    async with db() as session:
        await _service(session, storage).register_artifact(_create(), _EXAMINER, str(uuid4()))
        await session.commit()

    async with db() as session:
        actions = [row.action for row in (await session.execute(select(AuditLog))).scalars().all()]

    assert actions == ["forensic_artifact_registered"]


async def test_the_acquisition_hash_is_stored_in_one_canonical_spelling(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A tool's casing is accepted; the column holds one format, so the next reader cannot disagree
    with the last about what it says."""
    async with db() as session:
        artifact = await _service(session, storage).register_artifact(
            _create(acquisition_hash=f"sha-256:{_IMAGE_DIGEST}"), _EXAMINER, str(uuid4())
        )
        await session.commit()
        stored_hash = artifact.acquisition_hash

    assert stored_hash == f"SHA-256:{_IMAGE_DIGEST}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_kind", "disk-image"),
        ("artifact_kind", "post"),
        ("acquisition_hash", _IMAGE_DIGEST),
        ("acquisition_hash", f"MD5:{'a' * 32}"),
        ("acquisition_hash", f"SHA-512:{_IMAGE_DIGEST}"),
    ],
    ids=["unknown-kind", "other-category", "no-algorithm", "forbidden-algorithm", "mislabelled"],
)
async def test_a_malformed_definition_is_refused_at_registration(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage, field: str, value: str
) -> None:
    """§4.5's validation rules, and nothing is written when they fail."""
    async with db() as session:
        with pytest.raises(ValidationFailedError) as caught:
            await _service(session, storage).register_artifact(
                _create(**{field: value}), _EXAMINER, str(uuid4())
            )
        await session.rollback()

    assert any(detail["field"] == field for detail in caught.value.details)
    async with db() as session:
        assert (await session.execute(select(Artifact))).scalars().all() == []


# --- publication ------------------------------------------------------------
async def _register_and_publish(
    db: async_sessionmaker[AsyncSession],
    storage: FakeObjectStorage,
    *,
    create: ArtifactCreate | None = None,
) -> UUID:
    async with db() as session:
        await _register_schemas(session)
        service = _service(session, storage)
        artifact = await service.register_artifact(create or _create(), _EXAMINER, str(uuid4()))
        await session.commit()
        artifact_id = artifact.artifact_id

    async with db() as session:
        await _service(session, storage).publish_artifact(artifact_id, _EXAMINER, str(uuid4()))
        await session.commit()
    return artifact_id


async def test_publishing_normalizes_the_artifact_into_canonical_evidence(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """CEM §9's Commit step, performed by `ingestion` and recorded on the artifact."""
    artifact_id = await _register_and_publish(db, storage)

    async with db() as session:
        artifact = await session.get(Artifact, artifact_id)
        evidence = (await session.execute(select(Evidence))).scalars().one()

    assert artifact is not None
    assert artifact.status == STATUS_PUBLISHED
    assert artifact.evidence_id == evidence.evidence_id
    assert evidence.category == "digital_forensics"
    assert evidence.artifact_type == _DISK_KIND
    assert evidence.legal_authority_ref == "WARRANT-2026-0417"
    assert evidence.source["collector_id"] == f"examiner:{_EXAMINER.user_id}"


async def test_a_mobile_extraction_is_filed_under_mobile_forensics(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """One module, two of CEM §5's categories — the artifact kind decides which, not the caller."""
    await _register_and_publish(db, storage, create=_create(artifact_kind=_MOBILE_KIND))

    async with db() as session:
        evidence = (await session.execute(select(Evidence))).scalars().one()

    assert evidence.category == "mobile_forensics"


async def test_publishing_writes_the_custody_genesis_entry(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """CEM §13: the first custody event must be `collected` or `ingested`. `ingestion` writes it in
    the same transaction — which is why publication goes through its service, not the outbox."""
    await _register_and_publish(db, storage)

    async with db() as session:
        events = (await session.execute(select(EvidenceCustodyEvent))).scalars().all()

    assert [event.event_type for event in events] == ["ingested"]


async def test_publishing_publishes_artifact_processed_signed(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """§25.5's payload: `artifact_id` plus the `evidence_id` publication produced."""
    artifact_id = await _register_and_publish(db, storage)

    processed = [
        row for row in await _outbox(db) if row["event_type"] == "forensics.artifact_processed"
    ]
    async with db() as session:
        evidence = (await session.execute(select(Evidence))).scalars().one()

    assert len(processed) == 1
    assert processed[0]["payload"] == {
        "artifact_id": str(artifact_id),
        "evidence_id": str(evidence.evidence_id),
    }
    assert processed[0]["signature"] is not None


async def test_republishing_is_refused_rather_than_duplicated(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """Natural idempotency (§4.5 marks publish idempotent with no key): the artifact's own
    `evidence_id` is the guard, so a retry cannot produce a second evidence object for one
    acquisition — which would be two records of one seizure in a legal file."""
    artifact_id = await _register_and_publish(db, storage)

    async with db() as session:
        with pytest.raises(ConflictError):
            await _service(session, storage).publish_artifact(artifact_id, _EXAMINER, str(uuid4()))
        await session.rollback()

    async with db() as session:
        assert len((await session.execute(select(Evidence))).scalars().all()) == 1


# --- ADR-0008's recompute ---------------------------------------------------
async def test_a_payload_bearing_artifact_verifies_against_the_stored_bytes(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The examiner's manifest hash and the server's own digest, checked against each other.

    This is the link the whole carry-through exists for: `ingestion` streams the object at
    `payload_ref`, recomputes the digest, and only then admits the evidence (ADR-0008 §3).
    """
    await _seed_image(storage)
    create = _create(device_info=_envelope(payload_ref=f"s3://{_BUCKET}/{_KEY}"))

    await _register_and_publish(db, storage, create=create)

    async with db() as session:
        evidence = (await session.execute(select(Evidence))).scalars().one()
        genesis = (await session.execute(select(EvidenceCustodyEvent))).scalars().one()

    assert evidence.integrity_hash == _IMAGE_DIGEST
    assert evidence.integrity_verification_status == "verified"
    assert genesis.integrity_hash_at_event == _IMAGE_DIGEST, (
        "the ledger attests to bytes the server observed, not to the examiner's claim"
    )


async def test_an_acquisition_hash_that_does_not_match_the_stored_image_is_refused(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A tool manifest disagreeing with the bytes held is exactly what must not become evidence.

    The declared hash is well-formed and the object exists — the only thing wrong is that they are
    not the same image. Nothing but a server-side recompute catches that, and admitting it would put
    an artifact in a case file under a digest that proves nothing about it.
    """
    await _seed_image(storage)
    create = _create(
        acquisition_hash=f"SHA-256:{_UNRELATED_DIGEST}",
        device_info=_envelope(payload_ref=f"s3://{_BUCKET}/{_KEY}"),
    )

    async with db() as session:
        await _register_schemas(session)
        artifact = await _service(session, storage).register_artifact(
            create, _EXAMINER, str(uuid4())
        )
        await session.commit()
        artifact_id = artifact.artifact_id

    async with db() as session:
        with pytest.raises(ValidationFailedError) as caught:
            await _service(session, storage).publish_artifact(artifact_id, _EXAMINER, str(uuid4()))
        await session.rollback()

    assert any(detail["field"] == "integrity_hash" for detail in caught.value.details)
    async with db() as session:
        artifact_after = await session.get(Artifact, artifact_id)
        assert (await session.execute(select(Evidence))).scalars().all() == []
    assert artifact_after is not None
    assert artifact_after.evidence_id is None, "a rejected publish leaves the artifact unpublished"
    assert artifact_after.status == STATUS_REGISTERED


async def test_a_missing_stored_object_is_refused(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """ "We could not check" is not "it matched" — `payload_ref` naming nothing is a rejection."""
    create = _create(device_info=_envelope(payload_ref=f"s3://{_BUCKET}/absent.e01"))

    async with db() as session:
        await _register_schemas(session)
        artifact = await _service(session, storage).register_artifact(
            create, _EXAMINER, str(uuid4())
        )
        await session.commit()
        artifact_id = artifact.artifact_id

    async with db() as session:
        with pytest.raises(ValidationFailedError) as caught:
            await _service(session, storage).publish_artifact(artifact_id, _EXAMINER, str(uuid4()))
        await session.rollback()

    assert any(detail["field"] == "payload_ref" for detail in caught.value.details)


# --- publication refusals ---------------------------------------------------
async def test_an_incomplete_envelope_leaves_the_artifact_unpublished(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """FR-1.3's "never a silent partial ingestion": the rejection rolls back the whole request."""
    info = _envelope()
    del info["legal_authority_ref"]

    async with db() as session:
        await _register_schemas(session)
        artifact = await _service(session, storage).register_artifact(
            _create(device_info=info), _EXAMINER, str(uuid4())
        )
        await session.commit()
        artifact_id = artifact.artifact_id

    async with db() as session:
        with pytest.raises(ValidationFailedError) as caught:
            await _service(session, storage).publish_artifact(artifact_id, _EXAMINER, str(uuid4()))
        await session.rollback()

    assert any(
        detail["field"] == "device_info.legal_authority_ref" for detail in caught.value.details
    )
    async with db() as session:
        artifact_after = await session.get(Artifact, artifact_id)
        processed = [
            row for row in await _outbox(db) if row["event_type"] == "forensics.artifact_processed"
        ]
    assert artifact_after is not None and artifact_after.evidence_id is None
    assert processed == [], "no event announces a publication that did not happen"


async def test_an_unregistered_triple_is_refused_by_ingestion(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """Most of CEM §6's forensic kinds have no registry entry yet (only two are seeded).

    They register fine and cannot publish until their attributes schema exists — the registry is
    additive by §12, so this is a state to grow out of, not a bug. Asserted so the gap between
    "registerable" and "publishable" is visible rather than surprising.
    """
    async with db() as session:
        await _register_schemas(session)
        artifact = await _service(session, storage).register_artifact(
            _create(artifact_kind="memory_dump"), _EXAMINER, str(uuid4())
        )
        await session.commit()
        artifact_id = artifact.artifact_id

    async with db() as session:
        with pytest.raises(ValidationFailedError):
            await _service(session, storage).publish_artifact(artifact_id, _EXAMINER, str(uuid4()))
        await session.rollback()


async def test_publishing_an_unknown_artifact_is_a_404(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    from sentinelai.modules.forensics.exceptions import ArtifactNotFoundError

    async with db() as session:
        with pytest.raises(ArtifactNotFoundError):
            await _service(session, storage).publish_artifact(uuid4(), _EXAMINER, str(uuid4()))


# --- listing ----------------------------------------------------------------
async def test_listing_pages_forward_through_a_cursor(
    db: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """api-design.md §2.5's cursor pagination, actually advancing.

    A list endpoint that always answers `next_cursor: null` is one a client cannot page, whatever
    the repository underneath supports — so the cursor is asserted to move, not merely to exist.
    """
    from sentinelai.shared.pagination import PageParams

    async with db() as session:
        service = _service(session, storage)
        for index in range(3):
            await service.register_artifact(
                _create(collected_at=_NOW + timedelta(minutes=index)), _EXAMINER, str(uuid4())
            )
        await session.commit()

    async with db() as session:
        service = _service(session, storage)
        first, cursor, has_more = await service.list_artifacts(
            _EXAMINER, PageParams(limit=2, cursor=None)
        )
        assert has_more is True and cursor is not None
        second, next_cursor, more_after = await service.list_artifacts(
            _EXAMINER, PageParams(limit=2, cursor=cursor)
        )

    assert [a.collected_at for a in first] == [_NOW, _NOW + timedelta(minutes=1)]
    assert [a.collected_at for a in second] == [_NOW + timedelta(minutes=2)]
    assert (next_cursor, more_after) == (None, False)
