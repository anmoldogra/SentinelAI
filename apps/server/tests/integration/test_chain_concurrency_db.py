"""Concurrent appends cannot fork a hash chain — ADR-0003, ADR-0004.

Appending is a read-modify-write with no atomicity of its own: read the head, build an entry naming
it, insert. Two writers reading the same head both produce valid entries, and the ledger becomes a
tree — two contradictory histories of the same evidence, each internally consistent, with nothing
in the data to say which is real.

These tests use **two real database sessions racing on one Postgres**, because that is the only
place the property exists. A single-threaded test, or one against a fake, would pass against the
broken code.

Both mechanisms are covered, and they are covered separately on purpose:

* The **unique index** is the correctness guarantee. It is tested by deliberately bypassing the
  advisory lock — writing raw SQL the way an attacker or a buggy code path would — and asserting
  the second insert is rejected.
* The **advisory lock** is the liveness optimisation. It is tested by racing two writers through
  the real service path and asserting both succeed, in order, with no wasted work.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.db.chain_lock import AUDIT_CHAIN, CUSTODY_CHAIN, advisory_lock_key
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_GENESIS = "0" * 64


async def _reachable(url: str) -> bool:
    try:
        # Explicit short timeout: this probe decides skip-vs-run and must never stall the suite.
        engine = create_async_engine(url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _reachable(_URL):
        pytest.skip(f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL")

    name = f"sentinelai_racetest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    engine: AsyncEngine = create_async_engine(_URL.rsplit("/", 1)[0] + f"/{name}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
            # `AuditLog.__table_args__` carries the unique index, so `create_all` installs the
            # same constraint the migration does — which is what makes these tests meaningful
            # rather than a test of an index that only exists in production.
            await conn.run_sync(Base.metadata.create_all, tables=[AuditLog.__table__])
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


async def _append(sessions: async_sessionmaker[AsyncSession], action: str) -> None:
    async with sessions() as session:
        await record_audit_event(
            session,
            kms=kms_for_tests(),
            actor_user_id=uuid.uuid4(),
            actor_role="investigator",
            action=action,
            module="ingestion",
        )
        await session.commit()


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[AuditLog]:
    async with sessions() as session:
        return list(
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
        )


# --------------------------------------------------------------------------------------
# The unique index — the correctness guarantee
# --------------------------------------------------------------------------------------


async def test_the_unique_index_exists_on_the_chain_link(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Guards every other test here: without the index they would all pass on broken code."""
    async with sessions() as session:
        result = await session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'platform' AND tablename = 'audit_log' "
                "AND indexname = 'uq_audit_log_prev_entry_hash'"
            )
        )
        definition = result.scalar_one_or_none()
    assert definition is not None, "the chain-link unique index is missing"
    assert "UNIQUE" in definition


async def test_two_entries_cannot_name_the_same_predecessor(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The fork, attempted directly in SQL — bypassing the advisory lock entirely.

    This is the case that matters: the lock is cooperative and protects nothing against a writer
    that does not take it. A buggy code path, a migration script, or an attacker with table access
    all fall into this category. The constraint has to hold on its own.
    """
    await _append(sessions, "genesis")
    [first] = await _rows(sessions)

    async def insert_naming(prev: str, audit_id: uuid.UUID) -> None:
        async with sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO platform.audit_log "
                    "(audit_id, occurred_at, actor_role, action, module, "
                    " prev_entry_hash, entry_hash) "
                    "VALUES (:id, :ts, 'system', 'forged', 'platform', :prev, :entry)"
                ),
                {
                    "id": audit_id,
                    "ts": datetime.now(UTC),
                    "prev": prev,
                    "entry": uuid.uuid4().hex * 2,
                },
            )
            await session.commit()

    # The first successor is fine.
    await insert_naming(first.entry_hash, uuid.uuid4())
    # A second entry claiming the same predecessor is a fork, and is refused.
    with pytest.raises(IntegrityError):
        await insert_naming(first.entry_hash, uuid.uuid4())

    rows = await _rows(sessions)
    assert len(rows) == 2, "the forked entry must not have been written"


async def test_two_entries_cannot_both_be_genesis(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The sentinel is covered by the same rule: exactly one entry may be first.

    Without this, an attacker could start a second, parallel history from scratch and there would
    be two equally-valid chains in one table.
    """
    await _append(sessions, "first")
    async with sessions() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO platform.audit_log "
                    "(audit_id, occurred_at, actor_role, action, module, "
                    " prev_entry_hash, entry_hash) "
                    "VALUES (:id, :ts, 'system', 'second-genesis', 'platform', :prev, :entry)"
                ),
                {
                    "id": uuid.uuid4(),
                    "ts": datetime.now(UTC),
                    "prev": _GENESIS,
                    "entry": "f" * 64,
                },
            )
            await session.commit()


# --------------------------------------------------------------------------------------
# The advisory lock — the liveness optimisation
# --------------------------------------------------------------------------------------


def test_lock_keys_are_stable_and_distinct() -> None:
    """Derived by blake2b, not `hash()`, which is salted per process.

    If two worker processes computed different keys for the same chain they would take different
    locks and append concurrently — the lock would look present and do nothing.
    """
    assert advisory_lock_key(AUDIT_CHAIN) == advisory_lock_key(AUDIT_CHAIN)
    assert advisory_lock_key(AUDIT_CHAIN) != advisory_lock_key(CUSTODY_CHAIN)
    # Per-evidence custody chains must not collide with each other.
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert advisory_lock_key(CUSTODY_CHAIN, a) != advisory_lock_key(CUSTODY_CHAIN, b)
    # Postgres takes a signed 64-bit key.
    for key in (advisory_lock_key(AUDIT_CHAIN), advisory_lock_key(CUSTODY_CHAIN, a)):
        assert -(2**63) <= key < 2**63


async def test_concurrent_appends_serialize_into_one_unbroken_chain(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Eight writers racing through the real service path. All succeed; none forks.

    Without the lock these would read the same head and all but one would die on the unique index
    — correct, but only after each had paid for a KMS signature it had to discard. With it they
    queue, and every write lands.
    """
    await asyncio.gather(*(_append(sessions, f"concurrent-{i}") for i in range(8)))

    rows = await _rows(sessions)
    assert len(rows) == 8, "every concurrent append should have succeeded"

    # One unbroken chain: each entry names its predecessor, starting from the sentinel.
    by_prev = {row.prev_entry_hash: row for row in rows}
    assert len(by_prev) == 8, "two entries named the same predecessor — the chain forked"
    walked = []
    cursor = _GENESIS
    while cursor in by_prev:
        row = by_prev[cursor]
        walked.append(row)
        cursor = row.entry_hash
    assert len(walked) == 8, "the chain does not reach every row from genesis"


async def test_the_lock_actually_blocks_a_second_writer(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Proves the lock is doing something, rather than the test merely being fast enough.

    One transaction takes the chain lock and holds it; a second attempt to take it must not
    succeed until the first commits. Asserting on the ordering of two events is what distinguishes
    a real lock from a no-op.
    """
    order: list[str] = []
    released = asyncio.Event()

    async def holder() -> None:
        async with sessions() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_lock_key(AUDIT_CHAIN)}
            )
            order.append("holder-acquired")
            released.set()
            # Hold it across a real await, the way a KMS round-trip does.
            await asyncio.sleep(0.25)
            order.append("holder-committing")
            await session.commit()

    async def contender() -> None:
        await released.wait()
        async with sessions() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_lock_key(AUDIT_CHAIN)}
            )
            order.append("contender-acquired")
            await session.commit()

    await asyncio.gather(holder(), contender())
    assert order == ["holder-acquired", "holder-committing", "contender-acquired"]


async def test_a_rolled_back_transaction_releases_the_lock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`pg_advisory_xact_lock` releases on rollback as well as commit.

    This is why a transaction-scoped lock is used rather than a session-scoped one: a failed
    signing attempt must not wedge the ledger until the connection is recycled.
    """
    async with sessions() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_lock_key(AUDIT_CHAIN)}
        )
        await session.rollback()

    # If the lock had leaked, this would block until the test timed out.
    await asyncio.wait_for(_append(sessions, "after-rollback"), timeout=10)
    assert len(await _rows(sessions)) == 1
