"""Unit tests for the platform event plumbing (event-driven-architecture.md §16-17, guide Part 6).

The dispatcher, outbox writer, inbox guard, and the generic UoW are exercised against an
in-memory fake session — the real SQL statements are built and inspected, but nothing touches a
database. The DB-backed behaviour (real savepoints, real unique-constraint violations) belongs to
the integration suite; what is proved here is the control flow: at-least-once delivery, retry,
dead-lettering, graceful shutdown, and the transaction boundary.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.dispatcher import EventDispatcher, RetryPolicy
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import InboxGuard, get_inbox_table
from sentinelai.platform.events.outbox import OutboxWriter, get_outbox_table

_SCHEMA = "ingestion"


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": uuid4(),
        "event_type": "evidence.ingested",
        "event_version": "1.0.0",
        "aggregate_type": "evidence",
        "aggregate_id": uuid4(),
        "payload": {"k": "v"},
        "correlation_id": uuid4(),
        "causation_id": None,
        "trace_id": None,
        "actor_type": "user",
        "actor_ref": None,
        "occurred_at": datetime.now(UTC),
        "dispatch_status": "pending",
        "attempt_count": 0,
    }
    row.update(overrides)
    return row


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeSession:
    """Records executed statements; hands back queued rows for SELECTs."""

    def __init__(self, rows_per_select: list[list[dict[str, Any]]] | None = None) -> None:
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self._queue = rows_per_select or []
        self.raise_integrity_error = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if self.raise_integrity_error:
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        rows = self._queue.pop(0) if self._queue else []
        return _Result(rows)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Savepoint:
    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True


def _factory(session: _FakeSession):  # type: ignore[no-untyped-def]
    """A session factory that always hands back the same fake session."""

    def _make() -> _FakeSession:
        return session

    return _make


# --- envelope ---------------------------------------------------------------


def test_envelope_is_built_from_an_outbox_row() -> None:
    row = _row()
    envelope = EventEnvelope.from_row(row)  # type: ignore[arg-type]
    assert envelope.event_id == row["event_id"]
    assert envelope.event_type == "evidence.ingested"
    assert envelope.payload == {"k": "v"}
    assert envelope.attempt_count == 0


# --- outbox -----------------------------------------------------------------


def test_outbox_tables_are_memoised_per_schema() -> None:
    assert get_outbox_table(_SCHEMA) is get_outbox_table(_SCHEMA)
    assert get_outbox_table(_SCHEMA) is not get_outbox_table("osint")


def test_the_outbox_table_carries_the_full_envelope_column_set() -> None:
    columns = set(get_outbox_table(_SCHEMA).c.keys())
    assert {"correlation_id", "causation_id", "trace_id"} <= columns  # §11 triad
    assert {"dispatch_status", "attempt_count", "last_attempted_at"} <= columns


async def test_publish_inserts_one_pending_row_on_the_callers_session() -> None:
    """The outbox insert must ride the caller's transaction — it never commits on its own."""
    session = _FakeSession()
    writer = OutboxWriter(session, schema=_SCHEMA)  # type: ignore[arg-type]
    await writer.publish(
        event_type="evidence.ingested",
        aggregate_type="evidence",
        aggregate_id=uuid4(),
        payload={"evidence_id": "x"},
        correlation_id=str(uuid4()),
        actor_type="user",
    )
    assert len(session.statements) == 1
    assert session.commits == 0  # the service's UoW owns the commit


# --- inbox ------------------------------------------------------------------


def test_inbox_tables_are_memoised_per_schema() -> None:
    assert get_inbox_table(_SCHEMA) is get_inbox_table(_SCHEMA)


def test_the_inbox_key_is_event_id_plus_handler_name() -> None:
    """Two handlers must each get their own claim on the same event."""
    primary = {c.name for c in get_inbox_table(_SCHEMA).primary_key}
    assert primary == {"event_id", "handler_name"}


async def test_a_first_delivery_claims_the_event() -> None:
    session = _FakeSession()
    guard = InboxGuard(session, _SCHEMA)  # type: ignore[arg-type]
    assert await guard.try_claim(uuid4(), "handler") is True


async def test_a_redelivery_is_refused_so_no_side_effect_reruns() -> None:
    """At-least-once delivery is assumed: the duplicate insert is the dedup mechanism."""
    session = _FakeSession()
    session.raise_integrity_error = True
    guard = InboxGuard(session, _SCHEMA)  # type: ignore[arg-type]
    assert await guard.try_claim(uuid4(), "handler") is False


async def test_mark_processed_updates_the_claimed_row() -> None:
    session = _FakeSession()
    guard = InboxGuard(session, _SCHEMA)  # type: ignore[arg-type]
    await guard.mark_processed(uuid4(), "handler")
    assert len(session.statements) == 1


# --- generic unit of work ---------------------------------------------------


async def test_the_uow_commits_explicitly() -> None:
    session = _FakeSession()
    uow = UnitOfWork(session)  # type: ignore[arg-type]
    await uow.commit()
    assert (session.commits, session.rollbacks) == (1, 0)


async def test_the_uow_rolls_back_on_an_exception_and_never_partially_commits() -> None:
    session = _FakeSession()
    with pytest.raises(RuntimeError):
        async with UnitOfWork(session) as uow:  # type: ignore[arg-type]
            assert uow.session is session
            raise RuntimeError("boom")
    assert (session.commits, session.rollbacks) == (0, 1)


async def test_a_clean_uow_block_does_not_roll_back() -> None:
    session = _FakeSession()
    async with UnitOfWork(session):  # type: ignore[arg-type]
        pass
    assert session.rollbacks == 0


# --- dispatcher -------------------------------------------------------------


def _dispatcher(session: _FakeSession, **overrides: Any) -> EventDispatcher:
    kwargs: dict[str, Any] = {
        "poll_schemas": (_SCHEMA,),
        "poll_interval_seconds": 0.01,
        "batch_size": 10,
    }
    kwargs.update(overrides)
    return EventDispatcher(_factory(session), **kwargs)  # type: ignore[arg-type]


async def test_a_registered_handler_receives_its_event() -> None:
    row = _row()
    session = _FakeSession(rows_per_select=[[row]])
    dispatcher = _dispatcher(session)
    seen: list[EventEnvelope] = []

    async def _handler(event: EventEnvelope, _uow: UnitOfWork) -> None:
        seen.append(event)

    dispatcher.register("evidence.ingested", _handler, inbox_schema=_SCHEMA)
    assert await dispatcher._poll_once() == 1
    assert [e.event_id for e in seen] == [row["event_id"]]


async def test_an_event_with_no_handler_is_marked_dispatched_not_retried() -> None:
    session = _FakeSession(rows_per_select=[[_row(event_type="nobody.listens")]])
    dispatcher = _dispatcher(session)
    assert await dispatcher._poll_once() == 1
    assert session.commits == 1  # the mark-dispatched update committed


async def test_each_handler_runs_in_its_own_transaction() -> None:
    session = _FakeSession(rows_per_select=[[_row()]])
    dispatcher = _dispatcher(session)

    async def _one(_e: EventEnvelope, _u: UnitOfWork) -> None:
        return None

    async def _two(_e: EventEnvelope, _u: UnitOfWork) -> None:
        return None

    dispatcher.register("evidence.ingested", _one, inbox_schema=_SCHEMA)
    dispatcher.register("evidence.ingested", _two, inbox_schema=_SCHEMA)
    await dispatcher._poll_once()
    assert session.commits == 3  # two handler commits + the outbox status update


async def test_a_failing_handler_does_not_prevent_the_others_from_running() -> None:
    session = _FakeSession(rows_per_select=[[_row()]])
    dispatcher = _dispatcher(session)
    ran = []

    async def _bad(_e: EventEnvelope, _u: UnitOfWork) -> None:
        raise RuntimeError("handler exploded")

    async def _good(_e: EventEnvelope, _u: UnitOfWork) -> None:
        ran.append("good")

    dispatcher.register("evidence.ingested", _bad, inbox_schema=_SCHEMA)
    dispatcher.register("evidence.ingested", _good, inbox_schema=_SCHEMA)
    await dispatcher._poll_once()
    assert ran == ["good"]


async def test_a_failed_delivery_is_requeued_for_another_attempt() -> None:
    session = _FakeSession(rows_per_select=[[_row(attempt_count=0)]])
    dispatcher = _dispatcher(session)

    async def _bad(_e: EventEnvelope, _u: UnitOfWork) -> None:
        raise RuntimeError("down")

    dispatcher.register(
        "evidence.ingested", _bad, inbox_schema=_SCHEMA, policy=RetryPolicy(max_attempts=5)
    )
    await dispatcher._poll_once()
    update = session.statements[-1]
    assert update.compile().params["dispatch_status"] == "pending"


async def test_an_exhausted_event_is_dead_lettered_never_silently_dropped() -> None:
    session = _FakeSession(rows_per_select=[[_row(attempt_count=4)]])
    dispatcher = _dispatcher(session)

    async def _bad(_e: EventEnvelope, _u: UnitOfWork) -> None:
        raise RuntimeError("down")

    dispatcher.register(
        "evidence.ingested", _bad, inbox_schema=_SCHEMA, policy=RetryPolicy(max_attempts=5)
    )
    await dispatcher._poll_once()
    update = session.statements[-1]
    assert update.compile().params["dispatch_status"] == "dead_letter"


async def test_a_module_can_register_its_own_unit_of_work_type() -> None:
    class _ModuleUow(UnitOfWork):
        pass

    session = _FakeSession(rows_per_select=[[_row()]])
    dispatcher = _dispatcher(session)
    received: list[UnitOfWork] = []

    async def _handler(_e: EventEnvelope, uow: _ModuleUow) -> None:
        received.append(uow)

    dispatcher.register("evidence.ingested", _handler, inbox_schema=_SCHEMA, uow_factory=_ModuleUow)
    await dispatcher._poll_once()
    assert isinstance(received[0], _ModuleUow)


async def test_polling_stops_immediately_once_shutdown_is_requested() -> None:
    session = _FakeSession(rows_per_select=[[_row()]])
    dispatcher = _dispatcher(session)
    dispatcher.request_shutdown()
    assert await dispatcher._poll_once() == 0


async def test_run_forever_returns_after_shutdown_is_signalled() -> None:
    session = _FakeSession()
    dispatcher = _dispatcher(session)
    task = asyncio.create_task(dispatcher.run_forever())
    await asyncio.sleep(0.05)
    dispatcher.request_shutdown()
    await asyncio.wait_for(task, timeout=2.0)  # graceful drain, no hang


async def test_one_bad_poll_cycle_never_kills_the_dispatcher() -> None:
    """A transient database error must not take the whole dispatcher down."""

    class _Exploding(_FakeSession):
        async def execute(self, statement: Any) -> _Result:
            raise RuntimeError("database went away")

    dispatcher = _dispatcher(_Exploding())
    task = asyncio.create_task(dispatcher.run_forever())
    await asyncio.sleep(0.05)
    assert not task.done()  # still polling despite the failure
    dispatcher.request_shutdown()
    await asyncio.wait_for(task, timeout=2.0)


async def test_a_batch_of_events_is_drained_in_one_cycle() -> None:
    rows = [_row() for _ in range(3)]
    session = _FakeSession(rows_per_select=[rows])
    dispatcher = _dispatcher(session)
    seen: list[UUID] = []

    async def _handler(event: EventEnvelope, _u: UnitOfWork) -> None:
        seen.append(event.event_id)

    dispatcher.register("evidence.ingested", _handler, inbox_schema=_SCHEMA)
    assert await dispatcher._poll_once() == 3
    assert seen == [r["event_id"] for r in rows]
