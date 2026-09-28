"""Dispatcher claiming, deduplication and ordering — ADR-0006 §2/§3/§4, Wave 2.2.

Every property here is about what happens when **two dispatchers run at once**, so every test uses
real Postgres connections. `FOR UPDATE SKIP LOCKED` is a guarantee the database provides between
sessions; a fake or a single shared session cannot exhibit it, and a test that appeared to prove
deduplication without real row locks would be proving nothing at all.

The claim has three jobs and they are tested separately, because each fails differently:

* **No double-dispatch** — two dispatchers must never both handle one row.
* **Strict per-aggregate ordering** — events for one ``aggregate_id`` must be handled oldest-first,
  even across dispatchers, which means a later event must not be claimable while an earlier one is
  in flight.
* **Backoff** — a failed row must wait out its lease instead of hot-looping.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.config import settings
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.dispatcher import EventDispatcher
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.outbox import get_outbox_table
from sentinelai.platform.migrations._event_tables import outbox_dispatch_index

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_SCHEMA = "ingestion"
_EVENT_TYPE = "evidence.ingested"


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


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A throwaway database holding one module's outbox table, plus the ADR-0006 index.

    The index is created here as well as in the migration, so this suite exercises the same plan
    shape production will use rather than whatever a bare table happens to give.
    """
    if not await _reachable(_URL):
        pytest.skip(f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL")

    name = f"sentinelai_dispatchtest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    engine: AsyncEngine = create_async_engine(_URL.rsplit("/", 1)[0] + f"/{name}")
    try:
        table = get_outbox_table(_SCHEMA)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))
            await conn.run_sync(table.create)
            await conn.execute(
                text(
                    f"CREATE INDEX {outbox_dispatch_index(_SCHEMA)} "
                    f"ON {_SCHEMA}.outbox_events (dispatch_status, aggregate_id, occurred_at)"
                )
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


async def _insert_event(
    sessions: async_sessionmaker[AsyncSession],
    *,
    aggregate_id: uuid.UUID,
    occurred_at: datetime,
    marker: str,
    last_attempted_at: datetime | None = None,
    dispatch_status: str = "pending",
) -> uuid.UUID:
    table = get_outbox_table(_SCHEMA)
    event_id = uuid.uuid4()
    async with sessions() as session:
        await session.execute(
            insert(table).values(
                event_id=event_id,
                event_type=_EVENT_TYPE,
                event_version="1.0.0",
                aggregate_type="evidence",
                aggregate_id=aggregate_id,
                payload={"marker": marker},
                correlation_id=uuid.uuid4(),
                causation_id=None,
                trace_id=None,
                actor_type="user",
                actor_ref=None,
                occurred_at=occurred_at,
                dispatch_status=dispatch_status,
                attempt_count=0,
                last_error=None,
                last_attempted_at=last_attempted_at,
            )
        )
        await session.commit()
    return event_id


class _Recorder:
    """Records every delivery, in the order handlers actually saw them."""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.seen: list[str] = []
        self._fail = fail
        self._delay = delay
        self._lock = asyncio.Lock()

    async def handle(self, event: EventEnvelope, _uow: UnitOfWork) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)
        async with self._lock:
            self.seen.append(str(event.payload["marker"]))
        if self._fail:
            raise RuntimeError("handler failed on purpose")


def _dispatcher(
    sessions: async_sessionmaker[AsyncSession],
    recorder: _Recorder,
    *,
    batch_size: int = 100,
    lease_seconds: int = 60,
) -> EventDispatcher:
    dispatcher = EventDispatcher(
        sessions,
        poll_schemas=(_SCHEMA,),
        batch_size=batch_size,
        lease_seconds=lease_seconds,
    )
    dispatcher.register(_EVENT_TYPE, recorder.handle, inbox_schema=_SCHEMA)
    return dispatcher


async def _statuses(sessions: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """marker -> dispatch_status, for asserting what the relay did to each row."""
    table = get_outbox_table(_SCHEMA)
    async with sessions() as session:
        rows = (await session.execute(select(table.c.payload, table.c.dispatch_status))).all()
    return {str(row[0]["marker"]): str(row[1]) for row in rows}


# ---------------------------------------------------------------------------------------
# Deduplication under concurrency — ADR-0006 §2
# ---------------------------------------------------------------------------------------


async def test_two_concurrent_dispatchers_never_deliver_the_same_event_twice(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The defect ADR-0006 exists for, reproduced as a race and shown not to happen.

    Before Wave 2.2 the poll was a plain ``SELECT ... WHERE dispatch_status='pending'``, so two
    pollers read the same rows and both dispatched them. Inbox dedup masked the consequence
    downstream; it did not prevent the duplicate delivery, the wasted work, or the handler side
    effects that are not inbox-guarded.
    """
    markers = [f"m{i}" for i in range(12)]
    for index, marker in enumerate(markers):
        await _insert_event(
            sessions,
            aggregate_id=uuid.uuid4(),  # distinct aggregates: ordering is not what is under test
            occurred_at=datetime.now(UTC) - timedelta(seconds=100 - index),
            marker=marker,
        )

    shared = _Recorder(delay=0.01)
    left, right = _dispatcher(sessions, shared), _dispatcher(sessions, shared)
    claims: list[int] = []
    overlaps: list[set[uuid.UUID]] = []

    async def claim_and_run(dispatcher: EventDispatcher) -> list[uuid.UUID]:
        """One drain, reporting which rows this dispatcher claimed."""
        rows = await dispatcher._claim_batch(_SCHEMA)
        claims.append(len(rows))
        for row in rows:
            await dispatcher._process_row(_SCHEMA, EventEnvelope.from_row(row))
        return [row["event_id"] for row in rows]

    # Polled repeatedly rather than once: a single `gather` exercises one interleaving, and the
    # property has to hold for all of them. Each round also asserts the invariant directly -- the
    # two claims must be disjoint -- so a recurrence points at the claim rather than at a count.
    for _ in range(6):
        left_ids, right_ids = await asyncio.gather(claim_and_run(left), claim_and_run(right))
        overlaps.append(set(left_ids) & set(right_ids))
        if not left_ids and not right_ids:
            break

    assert all(not overlap for overlap in overlaps), f"two dispatchers claimed a row: {overlaps}"
    assert sorted(shared.seen) == sorted(markers), (
        f"every event must be delivered once: {shared.seen}"
    )
    assert len(shared.seen) == len(set(shared.seen)), f"duplicate delivery: {shared.seen}"
    assert set((await _statuses(sessions)).values()) == {"dispatched"}
    assert sum(claims) == len(markers), f"claims must sum to the row count, got {claims}"


async def test_a_claimed_row_is_invisible_to_a_peer_claim(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The lease, asserted directly on the claim rather than through a full dispatch.

    The row lock alone is released at commit, so a row left ``pending`` while its handlers run would
    be re-claimed by the next poll. The ``last_attempted_at`` stamp is what closes that window.
    """
    aggregate = uuid.uuid4()
    await _insert_event(
        sessions, aggregate_id=aggregate, occurred_at=datetime.now(UTC), marker="only"
    )

    first = _dispatcher(sessions, _Recorder())
    second = _dispatcher(sessions, _Recorder())

    claimed = await first._claim_batch(_SCHEMA)
    assert len(claimed) == 1

    # The lock is gone (the claim transaction committed) but the lease is not.
    again = await second._claim_batch(_SCHEMA)
    assert again == [], "a leased row must not be claimable by a peer"


async def test_an_expired_lease_makes_a_row_claimable_again(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A dispatcher killed mid-batch must not strand its rows forever.

    This is why the claim is a lease rather than a ``dispatching`` status: there is no reaper to
    write, and a crash needs no cleanup — the row simply becomes claimable when the lease lapses.
    """
    aggregate = uuid.uuid4()
    await _insert_event(
        sessions,
        aggregate_id=aggregate,
        occurred_at=datetime.now(UTC),
        marker="orphan",
        # Claimed two minutes ago by a dispatcher that never came back.
        last_attempted_at=datetime.now(UTC) - timedelta(minutes=2),
    )

    survivor = _dispatcher(sessions, _Recorder(), lease_seconds=60)
    claimed = await survivor._claim_batch(_SCHEMA)

    assert len(claimed) == 1
    assert str(claimed[0]["payload"]["marker"]) == "orphan"


# ---------------------------------------------------------------------------------------
# Strict per-aggregate ordering — ADR-0006 §3
# ---------------------------------------------------------------------------------------


async def test_only_the_oldest_pending_event_per_aggregate_is_claimed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The mechanism behind ordering: one row per aggregate, and it is the oldest.

    If a batch could contain two events for one aggregate, two dispatchers could take one each and
    run them in either order. Claiming at most the oldest makes that arrangement impossible.
    """
    aggregate = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(minutes=5)
    for index in range(3):
        await _insert_event(
            sessions,
            aggregate_id=aggregate,
            occurred_at=base + timedelta(seconds=index),
            marker=f"e{index}",
        )

    claimed = await _dispatcher(sessions, _Recorder())._claim_batch(_SCHEMA)

    assert len(claimed) == 1, "a batch must never hold two events for one aggregate"
    assert str(claimed[0]["payload"]["marker"]) == "e0"


async def test_events_for_one_aggregate_are_delivered_oldest_first(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Strict ordering end to end, with a handler slow enough to overlap if it could."""
    aggregate = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(minutes=5)
    for index in range(4):
        await _insert_event(
            sessions,
            aggregate_id=aggregate,
            occurred_at=base + timedelta(seconds=index),
            marker=f"e{index}",
        )

    recorder = _Recorder(delay=0.02)
    dispatcher = _dispatcher(sessions, recorder)
    # One drain per event, because only the oldest pending row for the aggregate is ever claimable.
    for _ in range(4):
        await dispatcher._poll_once()

    assert recorder.seen == ["e0", "e1", "e2", "e3"]


async def test_ordering_holds_when_two_dispatchers_compete_on_one_aggregate(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The real race: two dispatchers, one aggregate, overlapping polls.

    Both poll concurrently and repeatedly. Whichever wins each claim, the sequence the handler
    observes must still be strictly oldest-first — a later event is not claimable while an earlier
    one is in flight, because the earlier one is still the oldest *pending* row and still leased.
    """
    aggregate = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(minutes=5)
    markers = [f"e{i}" for i in range(5)]
    for index, marker in enumerate(markers):
        await _insert_event(
            sessions,
            aggregate_id=aggregate,
            occurred_at=base + timedelta(seconds=index),
            marker=marker,
        )

    shared = _Recorder(delay=0.01)
    left, right = _dispatcher(sessions, shared), _dispatcher(sessions, shared)

    for _ in range(len(markers)):
        await asyncio.gather(left._poll_once(), right._poll_once())

    assert shared.seen == markers, f"per-aggregate order broke: {shared.seen}"
    assert set((await _statuses(sessions)).values()) == {"dispatched"}


async def test_distinct_aggregates_are_dispatched_in_the_same_batch(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Ordering is per aggregate, not global — unrelated aggregates must not serialize.

    A claim that yielded one row overall would make throughput collapse to one event per poll, so
    this pins the "per" in per-aggregate.
    """
    base = datetime.now(UTC) - timedelta(minutes=5)
    for index in range(5):
        await _insert_event(
            sessions,
            aggregate_id=uuid.uuid4(),
            occurred_at=base + timedelta(seconds=index),
            marker=f"a{index}",
        )

    claimed = await _dispatcher(sessions, _Recorder())._claim_batch(_SCHEMA)

    assert len(claimed) == 5


# ---------------------------------------------------------------------------------------
# Retry backoff — ADR-0006 §4
# ---------------------------------------------------------------------------------------


async def test_a_failed_event_is_not_retried_until_its_backoff_elapses(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Without the gate, a permanently failing handler spins the dispatcher at poll speed.

    One poll should attempt the row once and then leave it alone: the failure re-stamps
    ``last_attempted_at``, so the row is outside the claim window until the lease passes.
    """
    await _insert_event(
        sessions, aggregate_id=uuid.uuid4(), occurred_at=datetime.now(UTC), marker="doomed"
    )

    recorder = _Recorder(fail=True)
    dispatcher = _dispatcher(sessions, recorder, lease_seconds=60)

    for _ in range(4):
        await dispatcher._poll_once()

    assert recorder.seen == ["doomed"], f"hot-looped: {recorder.seen}"
    assert (await _statuses(sessions))["doomed"] == "pending"


async def test_a_failed_event_is_retried_once_its_backoff_has_passed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The control for the test above: the row is retryable, the backoff was holding it."""
    await _insert_event(
        sessions, aggregate_id=uuid.uuid4(), occurred_at=datetime.now(UTC), marker="retryable"
    )

    recorder = _Recorder(fail=True)
    # A zero lease means "no backoff", which is exactly how the retry path is exercised without
    # sleeping in a test.
    dispatcher = _dispatcher(sessions, recorder, lease_seconds=0)

    for _ in range(3):
        await dispatcher._poll_once()

    assert len(recorder.seen) == 3
    assert (await _statuses(sessions))["retryable"] == "pending"


async def test_a_failing_event_dead_letters_at_its_attempt_ceiling(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Retries are bounded; the row must not stay pending forever."""
    await _insert_event(
        sessions, aggregate_id=uuid.uuid4(), occurred_at=datetime.now(UTC), marker="dead"
    )

    dispatcher = _dispatcher(sessions, _Recorder(fail=True), lease_seconds=0)
    for _ in range(6):  # RetryPolicy default max_attempts is 5
        await dispatcher._poll_once()

    assert (await _statuses(sessions))["dead"] == "dead_letter"


async def test_an_already_dispatched_row_is_never_reclaimed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal states are terminal — the claim filters on ``pending`` and must mean it."""
    await _insert_event(
        sessions,
        aggregate_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        marker="done",
        dispatch_status="dispatched",
    )
    await _insert_event(
        sessions,
        aggregate_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        marker="buried",
        dispatch_status="dead_letter",
    )

    recorder = _Recorder()
    await _dispatcher(sessions, recorder)._poll_once()

    assert recorder.seen == []
