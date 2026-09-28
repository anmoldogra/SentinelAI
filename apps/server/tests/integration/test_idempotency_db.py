"""The idempotency store against a real Postgres — ADR-0012 §1/§2(d)/§3.

The API tests prove the decision logic over a fake store. These prove the parts a fake cannot:

* the **unique constraint is the concurrency control** — two simultaneous claims on one key
  serialize on the index rather than both proceeding (§2(d));
* a failed request's claim really is **rolled back**, because the claim shares the request's
  transaction — which is what makes a retry after a transient failure possible at all;
* the **TTL sweep** deletes what it should and spares what it should not (§3).

Runs in a throwaway database created and dropped here. Skips cleanly when no Postgres is reachable;
never fakes a pass.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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

from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.idempotency.jobs import purge_expired_idempotency_keys
from sentinelai.platform.idempotency.models import (
    STATE_CLAIMED,
    STATE_COMPLETED,
    IdempotencyKey,
)
from sentinelai.platform.idempotency.repository import IdempotencyRepository

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_TTL = 86_400
_FINGERPRINT = "a" * 64


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
    name = f"sentinelai_idemtest_{uuid.uuid4().hex[:8]}"
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
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await conn.run_sync(Base.metadata.create_all, tables=[IdempotencyKey.__table__])


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


async def _claim(
    session: AsyncSession,
    *,
    principal: UUID,
    key: str,
    path: str = "/api/v1/evidence",
    fingerprint: str = _FINGERPRINT,
    now: datetime | None = None,
    ttl: int = _TTL,
) -> IdempotencyKey:
    return await IdempotencyRepository(session).claim(
        principal_id=principal,
        key=key,
        method="POST",
        path=path,
        fingerprint=fingerprint,
        now=now or datetime.now(UTC),
        ttl_seconds=ttl,
    )


# --- the claim --------------------------------------------------------------
async def test_a_claim_round_trips(db: async_sessionmaker[AsyncSession]) -> None:
    principal = uuid4()
    async with db() as session:
        row = await _claim(session, principal=principal, key="key-round-trip-1")
        await IdempotencyRepository(session).complete(
            row, status_code=201, headers={"etag": 'W/"abc"'}, body=b'{"data":{"id":1}}'
        )
        await session.commit()

    async with db() as session:
        stored = (await session.execute(select(IdempotencyKey))).scalars().one()

    assert stored.state == STATE_COMPLETED
    assert stored.response_status == 201
    assert stored.response_headers == {"etag": 'W/"abc"'}
    assert stored.response_body == b'{"data":{"id":1}}', "bytes, verbatim"
    assert stored.expires_at - stored.created_at == timedelta(seconds=_TTL)


async def test_the_same_key_cannot_be_claimed_twice(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The constraint ADR-0012 §1 specifies, proven against Postgres rather than the ORM."""
    principal = uuid4()
    async with db() as session:
        await _claim(session, principal=principal, key="key-dup-1")
        await session.commit()

    async with db() as session:
        with pytest.raises(IntegrityError):
            await _claim(session, principal=principal, key="key-dup-1")


async def test_two_principals_do_not_collide_on_one_key(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§3 scopes keys per principal. Without that, a client picking a common key ("1") would
    either collide with, or replay, another client's response."""
    async with db() as session:
        await _claim(session, principal=uuid4(), key="shared-key-value")
        await _claim(session, principal=uuid4(), key="shared-key-value")
        await session.commit()

    async with db() as session:
        rows = (await session.execute(select(IdempotencyKey))).scalars().all()
    assert len(rows) == 2


async def test_one_key_on_two_paths_is_two_claims(
    db: async_sessionmaker[AsyncSession],
) -> None:
    principal = uuid4()
    async with db() as session:
        await _claim(session, principal=principal, key="key-paths-1", path="/api/v1/cases")
        await _claim(session, principal=principal, key="key-paths-1", path="/api/v1/evidence")
        await session.commit()

    async with db() as session:
        rows = (await session.execute(select(IdempotencyKey))).scalars().all()
    assert len(rows) == 2


# --- concurrency (§2(d)) ----------------------------------------------------
async def test_a_concurrent_duplicate_serializes_on_the_index(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-0012 §2(d), and the reason this design needs no in-flight state machine.

    Two transactions claim the same key at once. Postgres makes the second **wait** on the unique
    index entry until the first ends — it does not get a duplicate row, and it does not fail fast.
    Exactly one wins; the loser learns the outcome only once the winner has committed, by which
    time the response it wants is stored.

    The barrier is what makes this a real race rather than two sequential calls: without it the
    first transaction could commit before the second even opened.
    """
    principal = uuid4()
    key = "key-concurrent-1"
    started = asyncio.Event()
    outcomes: list[str] = []

    async def winner() -> None:
        async with db() as session:
            await _claim(session, principal=principal, key=key)
            started.set()
            # Hold the claim open long enough for the loser to block on the index.
            await asyncio.sleep(0.4)
            await session.commit()
            outcomes.append("winner-committed")

    async def loser() -> None:
        await started.wait()
        async with db() as session:
            try:
                await _claim(session, principal=principal, key=key)
                await session.commit()
                outcomes.append("loser-committed")
            except IntegrityError:
                outcomes.append("loser-blocked-then-rejected")

    await asyncio.gather(winner(), loser())

    assert "winner-committed" in outcomes
    assert "loser-blocked-then-rejected" in outcomes, (
        "the second claim must be refused, not duplicated"
    )
    async with db() as session:
        rows = (await session.execute(select(IdempotencyKey))).scalars().all()
    assert len(rows) == 1, "exactly one claim survives a race"


async def test_a_rolled_back_claim_frees_the_key(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The property that makes retry-after-failure work, and the reason the claim lives in the
    request's own transaction rather than a separate committed one.

    A claim committed independently would survive the business failure it accompanied and block
    every retry of that key until the TTL expired — turning one transient error into 24 hours of
    them, and requiring a cleanup job for abandoned claims that this design does not need.
    """
    principal = uuid4()
    async with db() as session:
        await _claim(session, principal=principal, key="key-rollback-1")
        await session.rollback()

    async with db() as session:
        assert (await session.execute(select(IdempotencyKey))).scalars().all() == []
        # And the key is immediately reusable.
        await _claim(session, principal=principal, key="key-rollback-1")
        await session.commit()

    async with db() as session:
        rows = (await session.execute(select(IdempotencyKey))).scalars().all()
    assert len(rows) == 1


async def test_a_claim_is_invisible_to_another_transaction_until_it_commits(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Why `state = 'claimed'` is never observed by a reader, and therefore why an in-flight row
    cannot leak: a concurrent reader sees nothing at all, not a half-finished record."""
    principal = uuid4()
    async with db() as holder:
        await _claim(holder, principal=principal, key="key-isolation-1")
        async with db() as observer:
            visible = await IdempotencyRepository(observer).get(
                principal_id=principal, key="key-isolation-1", path="/api/v1/evidence"
            )
            assert visible is None
        await holder.rollback()


# --- lookup and completion --------------------------------------------------
async def test_get_finds_an_expired_row_rather_than_hiding_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The repository does not filter on expiry; the guard decides. That split is what lets an
    expired row be *deleted* on the way past instead of colliding with the new claim."""
    principal = uuid4()
    past = datetime.now(UTC) - timedelta(days=2)
    async with db() as session:
        await _claim(session, principal=principal, key="key-expired-1", now=past)
        await session.commit()

    async with db() as session:
        found = await IdempotencyRepository(session).get(
            principal_id=principal, key="key-expired-1", path="/api/v1/evidence"
        )
        assert found is not None
        assert found.expires_at < datetime.now(UTC)


async def test_dropping_a_claim_frees_the_key_within_one_transaction(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The expired-row path: drop, then re-claim the same tuple without a constraint violation."""
    principal = uuid4()
    async with db() as session:
        repo = IdempotencyRepository(session)
        stale = await _claim(session, principal=principal, key="key-drop-1")
        await repo.drop(stale)
        await _claim(session, principal=principal, key="key-drop-1", fingerprint="b" * 64)
        await session.commit()

    async with db() as session:
        row = (await session.execute(select(IdempotencyKey))).scalars().one()
    assert row.request_fingerprint == "b" * 64


async def test_replays_are_counted(db: async_sessionmaker[AsyncSession]) -> None:
    principal = uuid4()
    async with db() as session:
        repo = IdempotencyRepository(session)
        row = await _claim(session, principal=principal, key="key-replay-count-1")
        await repo.complete(row, status_code=201, headers={}, body=b"{}")
        await session.commit()

    async with db() as session:
        repo = IdempotencyRepository(session)
        stored = await repo.get(
            principal_id=principal, key="key-replay-count-1", path="/api/v1/evidence"
        )
        assert stored is not None
        await repo.note_replay(stored)
        await repo.note_replay(stored)
        await session.commit()

    async with db() as session:
        row = (await session.execute(select(IdempotencyKey))).scalars().one()
    assert row.replay_count == 2


async def test_a_binary_response_body_survives_the_round_trip(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """`bytea`, not text. A body that is not valid UTF-8 would be corrupted by a text column, and
    §2.9 requires the replay be byte-identical."""
    principal = uuid4()
    payload = b"\x89PNG\r\n\x1a\n\xff\xfe binary"
    async with db() as session:
        row = await _claim(session, principal=principal, key="key-binary-1")
        await IdempotencyRepository(session).complete(
            row, status_code=200, headers={}, body=payload
        )
        await session.commit()

    async with db() as session:
        stored = (await session.execute(select(IdempotencyKey))).scalars().one()
    assert stored.response_body == payload


# --- the TTL sweep (§3) -----------------------------------------------------
async def test_the_purge_job_deletes_only_expired_rows(
    db: async_sessionmaker[AsyncSession],
) -> None:
    principal = uuid4()
    now = datetime.now(UTC)
    async with db() as session:
        await _claim(session, principal=principal, key="key-live-1", now=now)
        await _claim(
            session,
            principal=principal,
            key="key-stale-1",
            path="/api/v1/cases",
            now=now - timedelta(days=3),
        )
        await session.commit()

    deleted = await purge_expired_idempotency_keys({"session_factory": db})

    assert deleted == 1
    async with db() as session:
        remaining = (await session.execute(select(IdempotencyKey))).scalars().all()
    assert [r.idempotency_key for r in remaining] == ["key-live-1"]


async def test_the_purge_job_is_safe_on_an_empty_table(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A missed or repeated run costs nothing — the sweep is about disk, not correctness."""
    assert await purge_expired_idempotency_keys({"session_factory": db}) == 0


async def test_a_claimed_but_abandoned_row_is_eventually_swept(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The HTTP path cannot produce one (a claim that never completes is rolled back), but a
    future non-HTTP caller could. The sweep deletes by age regardless of state, so such a row is
    reclaimed rather than pinning its key forever — which is why the TTL index is not partial.
    """
    principal = uuid4()
    async with db() as session:
        row = await _claim(
            session,
            principal=principal,
            key="key-abandoned-1",
            now=datetime.now(UTC) - timedelta(days=2),
        )
        assert row.state == STATE_CLAIMED
        await session.commit()

    assert await purge_expired_idempotency_keys({"session_factory": db}) == 1
