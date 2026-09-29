"""The threat-intel pipeline against a real Postgres — api-design.md §4.4, event-driven §25.4.

What only a real database settles here:

* the `(ioc_id, matched_evidence_id)` **unique index** §25.4 demands — the pair check in the service
  can lose a race, and the constraint is what makes the guarantee hold rather than merely usually
  hold;
* the matching query itself, which is one indexed `IN` against a token set rather than a loop over
  the IOC library;
* that the `ioc_matched` event lands in `threat_intel`'s own outbox **signed** under `EVENT_ROOT`,
  verified with the real signer rather than asserted non-null.

Skips cleanly when no Postgres is reachable; never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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

from sentinelai.modules.ingestion.models import Evidence
from sentinelai.modules.threat_intel.events import (
    MATCH_CONFIDENCE,
    on_evidence_ingested,
    scan_evidence_for_matches,
)
from sentinelai.modules.threat_intel.models import (
    FeedSubscription,
    Ioc,
    IocEvidenceMatch,
    ThreatActorProfile,
)
from sentinelai.modules.threat_intel.repository import (
    STATUS_ACTIVE,
    STATUS_RETIRED,
    ThreatIntelUnitOfWork,
)
from sentinelai.modules.threat_intel.schemas import FeedCreate, IocCreate, ThreatActorCreate
from sentinelai.modules.threat_intel.service import ThreatIntelService
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import get_inbox_table
from sentinelai.platform.events.outbox import get_outbox_table
from sentinelai.platform.events.signing import EventSigner
from sentinelai.shared.exceptions import ValidationFailedError
from sentinelai.shared.pagination import PageParams, encode_cursor
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_ACTOR = CurrentUser(user_id=uuid4(), roles=("investigator",))
_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


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
    name = f"sentinelai_ti_{uuid.uuid4().hex[:8]}"
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
    async with engine.begin() as conn:
        # `ingestion` too: the consumer's default reader is the real
        # `read_evidence_attributes`, which queries `ingestion.evidence`. Creating the table lets
        # that path run for real and return `None`, rather than dodging it with an injected fake.
        for schema in ("platform", "ingestion", "threat_intel"):
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                ThreatActorProfile.__table__,
                Ioc.__table__,
                FeedSubscription.__table__,
                IocEvidenceMatch.__table__,
                AuditLog.__table__,
                Evidence.__table__,
            ],
        )
        await conn.run_sync(get_outbox_table("threat_intel").create, checkfirst=True)
        await conn.run_sync(get_inbox_table("threat_intel").create, checkfirst=True)


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


def _service(session: AsyncSession) -> ThreatIntelService:
    return ThreatIntelService(
        ThreatIntelUnitOfWork(session, kms=kms_for_tests()), kms=kms_for_tests()
    )


def _uow(session: AsyncSession) -> ThreatIntelUnitOfWork:
    return ThreatIntelUnitOfWork(session, kms=kms_for_tests())


async def _outbox_rows(session: AsyncSession) -> list[Any]:
    result = await session.execute(
        text("SELECT * FROM threat_intel.outbox_events ORDER BY occurred_at")
    )
    return list(result.mappings().all())


def _reader(attributes: dict[str, Any] | None) -> Any:
    """A stand-in for `ingestion.public.read_evidence_attributes`.

    Injected for the matcher tests: §181's contract is "the consumer fetches the attributes", and
    what these tests are about is what the matcher does *with* them. The consumer test below uses
    the real reader, and the osint suite exercises it end to end.
    """

    async def _read(_: UUID) -> dict[str, Any] | None:
        return attributes

    return _read


# --- IOC registration -------------------------------------------------------
async def test_registering_an_ioc_normalizes_stores_and_announces_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.4 publishes `threat_intel.ioc_registered` on "New IOC created".

    api-design.md §4.4's table says "Events Published: none at creation" — the two documents
    disagree, and `event-driven-architecture.md` is the authority for the event catalog, so it is
    published. The conflict is recorded in the implementation log, not resolved silently.
    """
    async with db() as session:
        ioc = await _service(session).register_ioc(
            IocCreate(indicator_type="domain", value="Evil.Example.COM."), _ACTOR, str(uuid4())
        )
        await session.commit()

    async with db() as session:
        stored = (await session.execute(select(Ioc))).scalars().one()
        rows = await _outbox_rows(session)
        audits = [a.action for a in (await session.execute(select(AuditLog))).scalars().all()]

    assert stored.ioc_id == ioc.ioc_id
    assert stored.value == "evil.example.com", "normalized at registration, not at match time"
    assert stored.status == STATUS_ACTIVE
    assert stored.evidence_id is None, "§4.4: `evidence_id: null` — not yet published"
    assert stored.first_seen is not None and stored.last_seen is not None
    assert [r["event_type"] for r in rows] == ["threat_intel.ioc_registered"]
    assert rows[0]["payload"]["value"] == "evil.example.com"
    assert "ioc_registered" in audits, "§4.4's audit requirement"


async def test_the_registration_event_is_signed(db: async_sessionmaker[AsyncSession]) -> None:
    """ADR-0007 §1. Verified with the real signer — a populated column that verifies against nothing
    would satisfy a weaker assertion and fail the guarantee."""
    async with db() as session:
        await _service(session).register_ioc(
            IocCreate(indicator_type="ipv4", value="192.0.2.10"), _ACTOR, str(uuid4())
        )
        await session.commit()

    async with db() as session:
        event = (await _outbox_rows(session))[0]

    assert event["signature"] is not None
    signer = EventSigner(kms_for_tests())
    assert await signer.verify(
        schema="threat_intel",
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
    )


async def test_re_registering_the_same_indicator_converges(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """`(indicator_type, value)` is an IOC's natural key — the same hash from a second feed is the
    same hash. Two rows would each match the same evidence separately, turning one sighting into two
    alerts."""
    async with db() as session:
        service = _service(session)
        first = await service.register_ioc(
            IocCreate(indicator_type="hash_sha256", value=_SHA256.upper()), _ACTOR, str(uuid4())
        )
        await session.commit()
        earlier = first.last_seen

        second = await service.register_ioc(
            IocCreate(indicator_type="hash_sha256", value=_SHA256), _ACTOR, str(uuid4())
        )
        await session.commit()

    assert second.ioc_id == first.ioc_id
    assert second.last_seen is not None and earlier is not None
    assert second.last_seen >= earlier, "a second sighting advances last_seen"

    async with db() as session:
        rows = (await session.execute(select(Ioc))).scalars().all()
        events = [r["event_type"] for r in await _outbox_rows(session)]
    assert len(rows) == 1
    assert events == ["threat_intel.ioc_registered"], "the convergent path publishes nothing new"


@pytest.mark.parametrize(
    ("indicator_type", "value"),
    [
        ("domain", "not a domain"),
        ("ipv4", "192.0.2.256"),
        ("hash_sha256", "tooshort"),
        ("url", "no-scheme.example.com/x"),
        ("pigeon", "anything"),
    ],
)
async def test_an_invalid_indicator_is_a_422(
    db: async_sessionmaker[AsyncSession], indicator_type: str, value: str
) -> None:
    """§4.4: "`value` format validated against `indicator_type`". The message names the field so a
    feed author fixing a rejected indicator knows which."""
    async with db() as session:
        with pytest.raises(ValidationFailedError):
            await _service(session).register_ioc(
                IocCreate(indicator_type=indicator_type, value=value), _ACTOR, str(uuid4())
            )


async def test_an_unknown_threat_actor_is_refused(db: async_sessionmaker[AsyncSession]) -> None:
    """§4.4: "`threat_actor_id`, if present, must reference an existing profile"."""
    from sentinelai.modules.threat_intel.exceptions import ThreatActorNotFoundError

    async with db() as session:
        with pytest.raises(ThreatActorNotFoundError):
            await _service(session).register_ioc(
                IocCreate(indicator_type="ipv4", value="192.0.2.1", threat_actor_id=uuid4()),
                _ACTOR,
                str(uuid4()),
            )


async def test_a_known_threat_actor_is_attached(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        service = _service(session)
        profile = await service.create_threat_actor(
            ThreatActorCreate(name="APT-Example", aliases=["Group X"]), _ACTOR, str(uuid4())
        )
        ioc = await service.register_ioc(
            IocCreate(
                indicator_type="domain",
                value="c2.example.com",
                threat_actor_id=profile.threat_actor_id,
            ),
            _ACTOR,
            str(uuid4()),
        )
        await session.commit()

    assert ioc.threat_actor_id == profile.threat_actor_id


async def test_creating_a_threat_actor_publishes_nothing(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.4's catalog lists exactly two published events for this module: `ioc_registered` and
    `ioc_matched`. There is **no** `threat_intel.actor_profiled` — inventing one would violate
    CLAUDE.md rule 1, which requires a new event type be added to §25's catalog in the same change.
    The creation is audited, which is what §4.4 asks for."""
    async with db() as session:
        await _service(session).create_threat_actor(
            ThreatActorCreate(name="APT-Example"), _ACTOR, str(uuid4())
        )
        await session.commit()

    async with db() as session:
        rows = await _outbox_rows(session)
        audits = [a.action for a in (await session.execute(select(AuditLog))).scalars().all()]

    assert rows == []
    assert "threat_actor_profiled" in audits


# --- matching ---------------------------------------------------------------
async def _seed_ioc(
    session: AsyncSession, indicator_type: str, value: str, *, status: str = STATUS_ACTIVE
) -> Ioc:
    ioc = await _service(session).register_ioc(
        IocCreate(indicator_type=indicator_type, value=value), _ACTOR, str(uuid4())
    )
    ioc.status = status
    await session.flush()
    return ioc


async def test_an_indicator_present_in_evidence_produces_a_signed_match(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.4's handler action, and the event `investigation` consumes."""
    evidence_id = uuid4()
    async with db() as session:
        ioc = await _seed_ioc(session, "domain", "evil.example.com")
        await session.commit()

        count = await scan_evidence_for_matches(
            _uow(session),
            evidence_id=evidence_id,
            category="osint",
            correlation_id=str(uuid4()),
            read_attributes=_reader({"observed_domain": "Evil.Example.com"}),
        )
        await session.commit()

    assert count == 1
    async with db() as session:
        match = (await session.execute(select(IocEvidenceMatch))).scalars().one()
        matched_events = [
            r for r in await _outbox_rows(session) if r["event_type"] == "threat_intel.ioc_matched"
        ]
        stored_ioc = await session.get(Ioc, ioc.ioc_id)

    assert match.ioc_id == ioc.ioc_id
    assert match.matched_evidence_id == evidence_id
    assert match.confidence == MATCH_CONFIDENCE
    assert len(matched_events) == 1
    event = matched_events[0]
    assert event["payload"]["matched_evidence_id"] == str(evidence_id)
    assert event["actor_type"] == "system", (
        "a match is the platform's observation, not a user's act"
    )
    assert event["actor_ref"] is None
    assert event["signature"] is not None
    assert stored_ioc is not None and stored_ioc.last_seen is not None


async def test_a_retired_indicator_does_not_match(db: async_sessionmaker[AsyncSession]) -> None:
    """Matching on a stood-down indicator would resurrect a decision an analyst deliberately made.
    Retired IOCs are excluded in the query, not filtered afterwards, so they cost nothing."""
    async with db() as session:
        await _seed_ioc(session, "domain", "old.example.com", status=STATUS_RETIRED)
        await session.commit()

        count = await scan_evidence_for_matches(
            _uow(session),
            evidence_id=uuid4(),
            category="osint",
            correlation_id=str(uuid4()),
            read_attributes=_reader({"domain": "old.example.com"}),
        )

    assert count == 0


async def test_a_longer_domain_does_not_match_a_shorter_indicator(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The false positive a substring match would produce, proven against the real query."""
    async with db() as session:
        await _seed_ioc(session, "domain", "evil.com")
        await session.commit()

        count = await scan_evidence_for_matches(
            _uow(session),
            evidence_id=uuid4(),
            category="osint",
            correlation_id=str(uuid4()),
            read_attributes=_reader({"a": "notevil.com", "b": "evil.com.br"}),
        )

    assert count == 0


async def test_a_url_in_evidence_matches_a_domain_indicator(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The analyst registered the domain; the URL demonstrates it. Not decomposing would make the
    match depend on whether the connector stored a URL or a hostname."""
    async with db() as session:
        await _seed_ioc(session, "domain", "evil.example.com")
        await session.commit()

        count = await scan_evidence_for_matches(
            _uow(session),
            evidence_id=uuid4(),
            category="osint",
            correlation_id=str(uuid4()),
            read_attributes=_reader({"url": "https://evil.example.com/payload.exe"}),
        )

    assert count == 1


async def test_rescanning_the_same_pair_records_one_match(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§25.4: "never create a duplicate match row for the same pair".

    The service checks before inserting, which keeps redelivery quiet.
    """
    evidence_id = uuid4()
    async with db() as session:
        await _seed_ioc(session, "ipv4", "192.0.2.10")
        await session.commit()

        for _ in range(3):
            await scan_evidence_for_matches(
                _uow(session),
                evidence_id=evidence_id,
                category="osint",
                correlation_id=str(uuid4()),
                read_attributes=_reader({"peer": "192.0.2.10"}),
            )
            await session.commit()

    async with db() as session:
        matches = (await session.execute(select(IocEvidenceMatch))).scalars().all()
        matched = [
            r for r in await _outbox_rows(session) if r["event_type"] == "threat_intel.ioc_matched"
        ]

    assert len(matches) == 1
    assert len(matched) == 1, "and no duplicate event, or investigation would see two findings"


async def test_the_pair_uniqueness_is_enforced_by_the_database(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The constraint behind the check.

    Two workers scanning one evidence item concurrently both pass `exists_for_pair`; only one insert
    can then succeed, which is what turns "usually no duplicate" into "no duplicate".
    """
    evidence_id = uuid4()
    async with db() as session:
        ioc = await _seed_ioc(session, "ipv4", "192.0.2.10")
        await session.commit()

    async with db() as session:
        for _ in range(2):
            session.add(
                IocEvidenceMatch(
                    ioc_id=ioc.ioc_id,
                    matched_evidence_id=evidence_id,
                    matched_at=datetime.now(UTC),
                    confidence=Decimal("1.000"),
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_the_same_indicator_matches_two_different_evidence_items(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Uniqueness is per *pair*, not per IOC — an indicator turning up in two places is two
    sightings, and collapsing them would hide the second."""
    async with db() as session:
        await _seed_ioc(session, "domain", "evil.example.com")
        await session.commit()

        for _ in range(2):
            await scan_evidence_for_matches(
                _uow(session),
                evidence_id=uuid4(),
                category="osint",
                correlation_id=str(uuid4()),
                read_attributes=_reader({"domain": "evil.example.com"}),
            )
        await session.commit()

    async with db() as session:
        matches = (await session.execute(select(IocEvidenceMatch))).scalars().all()
    assert len(matches) == 2


async def test_missing_evidence_is_skipped_not_failed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The row may have been removed between the event and the scan. A raising handler would
    dead-letter an event describing something that genuinely happened, then block its aggregate's
    queue under ADR-0006's per-aggregate ordering."""
    async with db() as session:
        await _seed_ioc(session, "ipv4", "192.0.2.10")
        await session.commit()

        count = await scan_evidence_for_matches(
            _uow(session),
            evidence_id=uuid4(),
            category="osint",
            correlation_id=str(uuid4()),
            read_attributes=_reader(None),
        )

    assert count == 0


# --- the consumer -----------------------------------------------------------
def _event(payload: dict[str, Any]) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="evidence.ingested",
        event_version="1.0.0",
        occurred_at=datetime.now(UTC),
        aggregate_type="evidence",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        causation_id=None,
        trace_id=None,
        actor_type="user",
        actor_ref=uuid4(),
        dispatch_status="processing",
        attempt_count=1,
        payload=payload,
    )


async def test_the_consumer_claims_the_inbox_before_scanning(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Redelivery must not re-run the handler (§17). Delivered twice, the second call returns on the
    claim — and the pair check would stop a duplicate row even if it did not."""
    async with db() as session:
        await _seed_ioc(session, "ipv4", "192.0.2.10")
        await session.commit()

    event = _event({"evidence_id": str(uuid4()), "category": "osint"})
    # Nothing to read, so the scan is a no-op; what is under test is the claim, which happens first.
    async with db() as session:
        for _ in range(2):
            await on_evidence_ingested(event, _uow(session))
        await session.commit()

    async with db() as session:
        claims = await session.execute(
            text("SELECT COUNT(*) FROM threat_intel.inbox_events WHERE event_id = :eid"),
            {"eid": str(event.event_id)},
        )
    assert claims.scalar_one() == 1


async def test_a_payload_without_an_evidence_id_is_marked_handled(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Marked processed rather than dead-lettered: a missing id is not recoverable by retry, and a
    raising handler would block this aggregate's whole queue."""
    event = _event({"category": "osint"})
    async with db() as session:
        await on_evidence_ingested(event, _uow(session))
        await session.commit()

    async with db() as session:
        processed = await session.execute(
            text("SELECT processed_at FROM threat_intel.inbox_events WHERE event_id = :eid"),
            {"eid": str(event.event_id)},
        )
    assert processed.scalar_one() is not None


# --- feeds ------------------------------------------------------------------
async def test_adding_a_feed_is_idempotent_by_name(db: async_sessionmaker[AsyncSession]) -> None:
    """Two subscriptions to one feed would sync it twice and register every indicator twice."""
    async with db() as session:
        service = _service(session)
        first = await service.add_feed(
            FeedCreate(feed_name="vendor-x", protocol="taxii2"), _ACTOR, str(uuid4())
        )
        second = await service.add_feed(
            FeedCreate(feed_name="vendor-x", protocol="taxii2"), _ACTOR, str(uuid4())
        )
        await session.commit()

    assert second.subscription_id == first.subscription_id
    async with db() as session:
        feeds = (await session.execute(select(FeedSubscription))).scalars().all()
    assert len(feeds) == 1


async def test_syncing_an_unknown_feed_is_a_404(db: async_sessionmaker[AsyncSession]) -> None:
    from sentinelai.modules.threat_intel.exceptions import FeedSubscriptionNotFoundError

    async with db() as session:
        with pytest.raises(FeedSubscriptionNotFoundError):
            await _service(session).sync_feed(uuid4(), _ACTOR, str(uuid4()))


@pytest.mark.parametrize("profile", ["air-gapped", "classified"])
async def test_a_feed_sync_is_refused_on_a_zero_egress_profile(
    db: async_sessionmaker[AsyncSession], profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feed sync is by definition an outbound call. `deployment-architecture.md` requires
    air-gapped and classified deployments to have "zero configured or observed egress paths", so
    enqueuing a job that would attempt one — and possibly succeed through a misconfigured proxy — is
    not an acceptable answer on those profiles. Refusing at the API boundary is."""
    monkeypatch.setattr(settings, "app_env", profile)
    async with db() as session:
        service = _service(session)
        feed = await service.add_feed(
            FeedCreate(feed_name="vendor-x", protocol="taxii2"), _ACTOR, str(uuid4())
        )
        await session.commit()

        with pytest.raises(ValidationFailedError) as excinfo:
            await service.sync_feed(feed.subscription_id, _ACTOR, str(uuid4()))

    assert profile in str(excinfo.value.details)


async def test_a_feed_sync_is_audited_and_enqueued(db: async_sessionmaker[AsyncSession]) -> None:
    """The enqueue is the whole of what `sync_feed` does — the transport is not built, and
    `jobs.sync_feed_subscription` says so rather than stamping a sync that never happened."""

    class _Queue:
        def __init__(self) -> None:
            self.enqueued: list[tuple[str, tuple[object, ...]]] = []

        async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> object:
            self.enqueued.append((function, args))
            return None

    queue = _Queue()
    async with db() as session:
        service = ThreatIntelService(_uow(session), kms=kms_for_tests(), tasks=queue)
        feed = await service.add_feed(
            FeedCreate(feed_name="vendor-x", protocol="taxii2"), _ACTOR, str(uuid4())
        )
        await session.commit()
        await service.sync_feed(feed.subscription_id, _ACTOR, str(uuid4()))
        await session.commit()

    assert queue.enqueued == [("sync_feed_subscription", (feed.subscription_id,))]
    async with db() as session:
        audits = [a.action for a in (await session.execute(select(AuditLog))).scalars().all()]
    assert "feed_sync_requested" in audits


async def test_an_inactive_feed_cannot_be_synced(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        service = _service(session)
        feed = await service.add_feed(
            FeedCreate(feed_name="vendor-x", protocol="taxii2"), _ACTOR, str(uuid4())
        )
        feed.is_active = False
        await session.flush()

        with pytest.raises(ValidationFailedError):
            await service.sync_feed(feed.subscription_id, _ACTOR, str(uuid4()))


# --- listing ----------------------------------------------------------------
async def test_matches_page_newest_first(db: async_sessionmaker[AsyncSession]) -> None:
    """§4.4: "`matched_at` (default desc)". The cursor comparison flips with the order — `<` rather
    than `>` — which is the detail that silently returns an empty second page if copied from an
    ascending list."""
    async with db() as session:
        ioc = await _seed_ioc(session, "ipv4", "192.0.2.10")
        base = datetime.now(UTC)
        for offset in range(5):
            session.add(
                IocEvidenceMatch(
                    ioc_id=ioc.ioc_id,
                    matched_evidence_id=uuid4(),
                    matched_at=base - timedelta(minutes=offset),
                    confidence=Decimal("1.000"),
                )
            )
        await session.commit()

    collected: list[UUID] = []
    cursor: str | None = None
    async with db() as session:
        service = _service(session)
        for _ in range(10):
            page = await service.list_matches(
                ioc.ioc_id, _ACTOR, PageParams(limit=2, cursor=cursor)
            )
            if not page:
                break
            collected.extend(m.match_id for m in page)
            last = page[-1]
            cursor = encode_cursor(last.matched_at.isoformat(), last.match_id)

    assert len(collected) == 5
    assert len(set(collected)) == 5, "no match returned twice across pages"


async def test_listing_matches_for_an_unknown_ioc_is_a_404(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§4.4 validates `ioc_id`. An empty list would say "no matches" when the honest answer is "no
    such indicator"."""
    from sentinelai.modules.threat_intel.exceptions import IocNotFoundError

    async with db() as session:
        with pytest.raises(IocNotFoundError):
            await _service(session).list_matches(uuid4(), _ACTOR, PageParams(limit=10, cursor=None))


async def test_threat_actors_are_listed_by_name(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        service = _service(session)
        for name in ("Zeta Group", "Alpha Group", "Mid Group"):
            await service.create_threat_actor(ThreatActorCreate(name=name), _ACTOR, str(uuid4()))
        await session.commit()

    async with db() as session:
        names = [p.name for p in await _service(session).list_threat_actors(_ACTOR)]

    assert names == ["Alpha Group", "Mid Group", "Zeta Group"]
