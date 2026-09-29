"""The social_media capture and publish pipeline against real Postgres — api-design.md §4.6, CEM §9.

The real `SocialMediaService` wired to the real `EvidenceService` over one session, which is what
production does (ADR-0005: the entrypoint owns one transaction) and what makes "the capture and its
evidence commit together" testable rather than asserted.

What only a real database settles here:

* **account convergence** — registering a handle already monitored refreshes it instead of creating
  a second row, and `uq_social_account_platform_handle` holds when the check loses a race;
* **the provenance difference** between content from a monitored account and content from a handle
  nobody registered, which is a difference in evidential weight, not formatting;
* **rollback on a rejected publish**, so a capture missing its legal authority stays unpublished.

Runs in a throwaway database created and dropped here. Skips cleanly when no Postgres is reachable;
never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
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
from sentinelai.modules.social_media.models import CapturedContent, SocialAccountObserved
from sentinelai.modules.social_media.repository import SocialMediaUnitOfWork
from sentinelai.modules.social_media.schemas import AccountCreate, ContentCreate
from sentinelai.modules.social_media.service import (
    CATEGORY_SOCIAL_MEDIA,
    STATUS_CAPTURED,
    STATUS_PUBLISHED,
    SocialMediaService,
)
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.events.outbox import get_outbox_table
from sentinelai.shared.cem import PUBLIC_SOURCE_AUTHORITY
from sentinelai.shared.exceptions import ConflictError, ValidationFailedError
from sentinelai.shared.pagination import PageParams
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_ANALYST = CurrentUser(user_id=uuid4(), roles=("investigator",))
# In the past on purpose: §4.6 refuses a future `captured_at`, and CEM §13's clock-skew rule would
# refuse it again downstream — a future timestamp would fail these tests for the wrong reason.
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_SCHEMA_VERSION = "1.0.0"
_PLATFORM = "X"
_HANDLE = "@suspect_01"


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
    name = f"sentinelai_social_{uuid.uuid4().hex[:8]}"
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
    """Three schemas, because the pipeline genuinely spans them."""
    async with engine.begin() as conn:
        for schema in ("platform", "ingestion", "social_media"):
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                CapturedContent.__table__,
                SocialAccountObserved.__table__,
                Evidence.__table__,
                EvidenceCustodyEvent.__table__,
                AttributeSchemaRegistry.__table__,
                IntakeRecord.__table__,
                AuditLog.__table__,
            ],
        )
        await conn.run_sync(get_outbox_table("social_media").create, checkfirst=True)
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


def _service(session: AsyncSession) -> SocialMediaService:
    """The real service, wired to the real `ingestion` service through the same session."""
    kms = kms_for_tests()
    evidence = EvidenceService(
        IngestionUnitOfWork(session, kms=kms), storage=FakeObjectStorage(), kms=kms
    )
    return SocialMediaService(SocialMediaUnitOfWork(session, kms=kms), evidence=evidence, kms=kms)


async def _register_schema(session: AsyncSession, artifact_type: str = "post") -> None:
    """Register the triple publishing requires.

    Production gets these from `202609290003_ingest_seed_social`; this file builds its schema with
    `create_all`, so the row is inserted here for the same reason `test_forensics_db.py` does it.
    """
    session.add(
        AttributeSchemaRegistry(
            schema_version=_SCHEMA_VERSION,
            category=CATEGORY_SOCIAL_MEDIA,
            artifact_type=artifact_type,
        )
    )
    await session.flush()


def _envelope(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "title": f"Post by {_HANDLE}",
        "attributes": {"body": "meet at the usual spot"},
        "confidence": 0.9,
        "legal_authority_ref": "PRODUCTION-ORDER-2026-88",
    }
    raw.update(overrides)
    return raw


def _capture(**overrides: Any) -> ContentCreate:
    fields: dict[str, Any] = {
        "platform": _PLATFORM,
        "account_handle": _HANDLE,
        "content_kind": "post",
        "captured_at": _NOW,
        "raw_attributes": _envelope(),
    }
    fields.update(overrides)
    return ContentCreate(**fields)


async def _outbox(db: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with db() as session:
        result = await session.execute(select(get_outbox_table("social_media")))
        return [dict(row) for row in result.mappings().all()]


# --- account monitoring -----------------------------------------------------
async def test_registering_an_account_stores_and_announces_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.6's `account_registered`: "New account added for monitoring"."""
    async with db() as session:
        await _service(session).register_account(
            AccountCreate(platform=_PLATFORM, handle=_HANDLE), _ANALYST, str(uuid4())
        )
        await session.commit()

    async with db() as session:
        accounts = (await session.execute(select(SocialAccountObserved))).scalars().all()
    rows = await _outbox(db)

    assert len(accounts) == 1
    assert accounts[0].platform == _PLATFORM
    assert accounts[0].first_observed_at is not None
    assert [row["event_type"] for row in rows] == ["social_media.account_registered"]
    assert rows[0]["payload"]["platform"] == _PLATFORM
    assert rows[0]["signature"] is not None, "ADR-0007 §1: signed under EVENT_ROOT"


async def test_registering_a_monitored_account_refreshes_it_rather_than_duplicating(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The table is a set of accounts *observed*: one handle on one platform is one account.

    Two rows would split its observation window, so a monitoring query could miss content depending
    on which row it found. The caller's intent — "monitor this account" — is already satisfied, so
    a `409` would make a client treat a no-op as an error.
    """
    async with db() as session:
        service = _service(session)
        first = await service.register_account(
            AccountCreate(platform=_PLATFORM, handle=_HANDLE), _ANALYST, str(uuid4())
        )
        await session.commit()
        first_id, first_seen = first.account_id, first.first_observed_at

    async with db() as session:
        again = await _service(session).register_account(
            AccountCreate(platform=_PLATFORM, handle=_HANDLE), _ANALYST, str(uuid4())
        )
        await session.commit()
        again_id, last_seen = again.account_id, again.last_observed_at

    async with db() as session:
        accounts = (await session.execute(select(SocialAccountObserved))).scalars().all()

    assert again_id == first_id
    assert len(accounts) == 1
    assert accounts[0].first_observed_at == first_seen, "the original observation is not rewritten"
    assert last_seen is not None and last_seen > first_seen


async def test_a_refresh_does_not_announce_a_new_registration(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.6's trigger is "New account added" — announcing a refresh would make a consumer counting
    monitored accounts wrong."""
    async with db() as session:
        service = _service(session)
        for _ in range(3):
            await service.register_account(
                AccountCreate(platform=_PLATFORM, handle=_HANDLE), _ANALYST, str(uuid4())
            )
        await session.commit()

    registered = [
        row for row in await _outbox(db) if row["event_type"] == "social_media.account_registered"
    ]
    assert len(registered) == 1


async def test_the_same_handle_on_another_platform_is_another_account(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """`@suspect_01` on X and on Telegram are two people until an analyst says otherwise — which is
    an entity-resolution judgement, not something a uniqueness rule should make for them."""
    async with db() as session:
        service = _service(session)
        await service.register_account(
            AccountCreate(platform=_PLATFORM, handle=_HANDLE), _ANALYST, str(uuid4())
        )
        await service.register_account(
            AccountCreate(platform="Telegram", handle=_HANDLE), _ANALYST, str(uuid4())
        )
        await session.commit()

    async with db() as session:
        accounts = (await session.execute(select(SocialAccountObserved))).scalars().all()

    assert {account.platform for account in accounts} == {_PLATFORM, "Telegram"}


async def test_the_account_pair_is_unique_in_the_database(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The half the service's check cannot provide: two registrations can both find nothing."""
    async with db() as session:
        await _service(session).register_account(
            AccountCreate(platform=_PLATFORM, handle=_HANDLE), _ANALYST, str(uuid4())
        )
        await session.commit()

    async with db() as session:
        session.add(
            SocialAccountObserved(
                platform=_PLATFORM, handle=_HANDLE, first_observed_at=_NOW, last_observed_at=None
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


# --- capture ----------------------------------------------------------------
async def test_capturing_content_stores_it_unpublished_and_announces_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.6's `content_captured` fires on **capture**, not publication.

    api-design.md §4.6 says "Events Published: none at this step" for the same endpoint;
    `CLAUDE.md` makes `event-driven-architecture.md` authoritative for the event catalog. The same
    conflict, resolved the same way, as `osint.finding_captured`, `threat_intel.ioc_registered` and
    `forensics.artifact_registered`.
    """
    async with db() as session:
        content = await _service(session).create_content(_capture(), _ANALYST, str(uuid4()))
        await session.commit()
        content_id = content.content_id

    async with db() as session:
        stored = await session.get(CapturedContent, content_id)
    captured = [
        row for row in await _outbox(db) if row["event_type"] == "social_media.content_captured"
    ]

    assert stored is not None
    assert stored.evidence_id is None
    assert stored.status == STATUS_CAPTURED
    assert len(captured) == 1
    assert captured[0]["payload"] == {
        "content_id": str(content_id),
        "platform": _PLATFORM,
        "account_handle": _HANDLE,
    }
    assert captured[0]["signature"] is not None


async def test_capturing_from_a_monitored_account_moves_its_observation_window(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """What `last_observed_at` is for: the account produced something."""
    async with db() as session:
        service = _service(session)
        account = await service.register_account(
            AccountCreate(platform=_PLATFORM, handle=_HANDLE), _ANALYST, str(uuid4())
        )
        await session.commit()
        before = account.last_observed_at

    async with db() as session:
        await _service(session).create_content(_capture(), _ANALYST, str(uuid4()))
        await session.commit()

    async with db() as session:
        account_after = (await session.execute(select(SocialAccountObserved))).scalars().one()

    assert before is not None and account_after.last_observed_at is not None
    assert account_after.last_observed_at > before


async def test_capturing_from_an_unmonitored_handle_does_not_enrol_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A connector watching a hashtag captures content from handles nobody registered.

    The capture is kept — refusing it would lose evidence to a bookkeeping gap — but the author is
    not silently added to a monitoring list an analyst curates.
    """
    async with db() as session:
        await _service(session).create_content(
            _capture(account_handle="@bystander"), _ANALYST, str(uuid4())
        )
        await session.commit()

    async with db() as session:
        accounts = (await session.execute(select(SocialAccountObserved))).scalars().all()
        content = (await session.execute(select(CapturedContent))).scalars().one()

    assert accounts == []
    assert content.account_handle == "@bystander"


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"content_kind": "tweet"}, "content_kind"),
        ({"captured_at": _NOW + timedelta(days=1)}, "captured_at"),
    ],
    ids=["unknown-kind", "future-capture"],
)
async def test_a_malformed_capture_is_refused_and_stores_nothing(
    db: async_sessionmaker[AsyncSession], override: dict[str, Any], field: str
) -> None:
    """§4.6's two validation rules."""
    async with db() as session:
        with pytest.raises(ValidationFailedError) as caught:
            await _service(session).create_content(_capture(**override), _ANALYST, str(uuid4()))
        await session.rollback()

    assert any(detail["field"] == field for detail in caught.value.details)
    async with db() as session:
        assert (await session.execute(select(CapturedContent))).scalars().all() == []


# --- publication ------------------------------------------------------------
async def _capture_and_publish(
    db: async_sessionmaker[AsyncSession], *, create: ContentCreate | None = None
) -> UUID:
    async with db() as session:
        await _register_schema(session)
        service = _service(session)
        await service.register_account(
            AccountCreate(platform=_PLATFORM, handle=_HANDLE), _ANALYST, str(uuid4())
        )
        content = await service.create_content(create or _capture(), _ANALYST, str(uuid4()))
        await session.commit()
        content_id = content.content_id

    async with db() as session:
        await _service(session).publish_content(content_id, _ANALYST, str(uuid4()))
        await session.commit()
    return content_id


async def test_publishing_normalizes_the_capture_into_canonical_evidence(
    db: async_sessionmaker[AsyncSession],
) -> None:
    content_id = await _capture_and_publish(db)

    async with db() as session:
        content = await session.get(CapturedContent, content_id)
        evidence = (await session.execute(select(Evidence))).scalars().one()
        account = (await session.execute(select(SocialAccountObserved))).scalars().one()

    assert content is not None
    assert content.status == STATUS_PUBLISHED
    assert content.evidence_id == evidence.evidence_id
    assert evidence.category == CATEGORY_SOCIAL_MEDIA
    assert evidence.artifact_type == "post"
    assert evidence.legal_authority_ref == "PRODUCTION-ORDER-2026-88"
    assert evidence.source["collector_id"] == str(account.account_id), (
        "a monitored account ties the evidence to the configuration that produced it"
    )


async def test_publishing_content_from_an_unmonitored_handle_records_weaker_provenance(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """CEM §13 requires *a* collector, and the handle is the only stable identifier available.

    Asserted because the difference is evidential, not cosmetic: one answer points at a monitoring
    configuration an analyst set up, the other at a string the platform observed.
    """
    async with db() as session:
        await _register_schema(session)
        content = await _service(session).create_content(
            _capture(account_handle="@bystander"), _ANALYST, str(uuid4())
        )
        await session.commit()
        content_id = content.content_id

    async with db() as session:
        await _service(session).publish_content(content_id, _ANALYST, str(uuid4()))
        await session.commit()

    async with db() as session:
        evidence = (await session.execute(select(Evidence))).scalars().one()

    assert evidence.source["collector_id"] == "@bystander"


async def test_publishing_writes_the_custody_genesis_entry(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """CEM §13: the first custody event must be `collected` or `ingested`, written by `ingestion`
    in the same transaction."""
    await _capture_and_publish(db)

    async with db() as session:
        events = (await session.execute(select(EvidenceCustodyEvent))).scalars().all()

    assert [event.event_type for event in events] == ["ingested"]


async def test_publishing_announces_nothing_new(db: async_sessionmaker[AsyncSession]) -> None:
    """§25.6 defines two events and publication triggers neither.

    The canonical fact is `evidence.ingested`, published by `ingestion` on its own — which is also
    how `investigation` learns about social content (§25.6's "reaches `investigation` via
    `evidence.ingested`"). A third event here would be an invention.
    """
    await _capture_and_publish(db)

    types = [row["event_type"] for row in await _outbox(db)]
    assert sorted(types) == ["social_media.account_registered", "social_media.content_captured"]

    async with db() as session:
        ingestion_rows = (
            (await session.execute(select(get_outbox_table("ingestion")))).mappings().all()
        )
    assert any(row["event_type"] == "evidence.ingested" for row in ingestion_rows)


async def test_republishing_is_refused_rather_than_duplicated(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Natural idempotency: a retry cannot produce a second evidence object for one post."""
    content_id = await _capture_and_publish(db)

    async with db() as session:
        with pytest.raises(ConflictError):
            await _service(session).publish_content(content_id, _ANALYST, str(uuid4()))
        await session.rollback()

    async with db() as session:
        assert len((await session.execute(select(Evidence))).scalars().all()) == 1


async def test_a_capture_without_legal_authority_stays_unpublished(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """CEM §13 requires it for this category, and the transaction rolls back rather than half-write.

    The capture itself is kept: the content was lawfully observed and the missing field is a
    *declaration*, so an analyst can supply it and publish later rather than losing the record.
    """
    raw = _envelope()
    del raw["legal_authority_ref"]

    async with db() as session:
        await _register_schema(session)
        content = await _service(session).create_content(
            _capture(raw_attributes=raw), _ANALYST, str(uuid4())
        )
        await session.commit()
        content_id = content.content_id

    async with db() as session:
        with pytest.raises(ValidationFailedError) as caught:
            await _service(session).publish_content(content_id, _ANALYST, str(uuid4()))
        await session.rollback()

    assert any(
        detail["field"] == "raw_attributes.legal_authority_ref" for detail in caught.value.details
    )
    async with db() as session:
        after = await session.get(CapturedContent, content_id)
        assert (await session.execute(select(Evidence))).scalars().all() == []
    assert after is not None and after.evidence_id is None
    assert after.status == STATUS_CAPTURED


async def test_the_public_source_sentinel_publishes(db: async_sessionmaker[AsyncSession]) -> None:
    """A connector capturing a public post states the sentinel, and that is a lawful publication."""
    await _capture_and_publish(
        db, create=_capture(raw_attributes=_envelope(legal_authority_ref=PUBLIC_SOURCE_AUTHORITY))
    )

    async with db() as session:
        evidence = (await session.execute(select(Evidence))).scalars().one()

    assert evidence.legal_authority_ref == PUBLIC_SOURCE_AUTHORITY


# --- audit and listing ------------------------------------------------------
async def test_every_documented_act_is_audited(db: async_sessionmaker[AsyncSession]) -> None:
    """§4.6 requires an audit entry on both POSTs; publication is audited for the same reason
    `osint` and `forensics` audit theirs — it is the act that turns a capture into evidence."""
    await _capture_and_publish(db)

    async with db() as session:
        actions = [row.action for row in (await session.execute(select(AuditLog))).scalars().all()]

    assert sorted(actions) == [
        "evidence_published_from_social_media",
        "social_account_registered",
        "social_content_captured",
    ]


async def test_listing_pages_forward_through_a_cursor(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A monitored account can burst, so the `(collected_at, content_id)` tie-break is load-bearing:
    three captures at the same instant must still page deterministically."""
    async with db() as session:
        service = _service(session)
        for _ in range(3):
            await service.create_content(_capture(), _ANALYST, str(uuid4()))
        await session.commit()

    async with db() as session:
        service = _service(session)
        first, cursor, has_more = await service.list_content(
            _ANALYST, PageParams(limit=2, cursor=None)
        )
        assert has_more is True and cursor is not None
        second, next_cursor, more_after = await service.list_content(
            _ANALYST, PageParams(limit=2, cursor=cursor)
        )

    assert len(first) == 2
    assert len(second) == 1
    assert {c.content_id for c in first}.isdisjoint({c.content_id for c in second})
    assert (next_cursor, more_after) == (None, False)


async def test_accounts_are_listed_in_a_stable_order(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Unpaginated by §4.6, so the only contract is that the console does not reshuffle."""
    async with db() as session:
        service = _service(session)
        for platform, handle in (("X", "@b"), ("Telegram", "@a"), ("X", "@a")):
            await service.register_account(
                AccountCreate(platform=platform, handle=handle), _ANALYST, str(uuid4())
            )
        await session.commit()

    async with db() as session:
        accounts = await _service(session).list_accounts(_ANALYST)

    assert [(a.platform, a.handle) for a in accounts] == [
        ("Telegram", "@a"),
        ("X", "@a"),
        ("X", "@b"),
    ]
